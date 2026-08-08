# -*- coding: utf-8 -*-
"""Spider Service — FastAPI wrapper for Xianyu (闲鱼) scraping.

Provides REST API to trigger searches, get results, and manage tasks.
Results are stored in PostgreSQL and forwarded to Agent Pipeline for AI analysis.
"""
from __future__ import annotations
import asyncio, base64, json, logging, os, time, uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# 日志脱敏（详见 common/logfilter.py）。爬虫日志里会出现闲鱼 Cookie / token。
try:
    import sys as _sys
    from pathlib import Path as _P
    _r = str(_P(__file__).resolve().parents[2])
    if _r not in _sys.path:
        _sys.path.insert(0, _r)
    from common.logfilter import install as _install_logfilter
    _install_logfilter()
except Exception:
    pass

# 瑕疵定义词关键词展开（详见 common/keyword_expander.py）：「摔坏的手机」
# → 摔坏的手机/摔坏的iphone/屏幕碎的手机/坏的手机。不可用时退回单词行为。
try:
    from common.keyword_expander import (
        expand_keyword as _expand_keyword,
        classify_keyword as _classify_keyword,
        extract_residual as _extract_residual,
        max_variants as _expand_max_variants,
    )
    from common import keyword_lexicon_store as _lexicon_store
except Exception:
    def _expand_keyword(keyword, max_variants=None):  # type: ignore
        return [keyword] if (keyword or "").strip() else []

    def _classify_keyword(keyword):  # type: ignore
        return {"defect": None, "device": None}

    def _extract_residual(variant):  # type: ignore
        return ""

    def _expand_max_variants():  # type: ignore
        return 4

    _lexicon_store = None


async def _ai_defect_variants(keyword: str) -> List[str]:
    """静态词库未命中瑕疵词时的 AI 兜底理解（增量进化的「理解」层）。

    问 ai-router：该关键词是否描述瑕疵/损坏商品？是 → 给口语化损坏搜索词，
    变体落学习库（下次同词直接静态命中，不再烧 token）；否 → 负缓存 7 天。
    任何失败（AI 未配置/超时/解析失败）都静默返回 []，绝不阻塞搜索。
    """
    if _lexicon_store is None:
        return []
    norm = " ".join((keyword or "").lower().split())
    if not norm:
        return []
    cached = _lexicon_store.get_ai_variants(norm)
    if cached is not None:
        return cached
    try:
        content = (
            f"二手商品搜索词：{keyword}\n"
            "请判断该搜索词是否在寻找有瑕疵/损坏的二手商品。如果是，给出 3 个"
            "闲鱼上描述此类损坏的口语化搜索词变体（如：摔坏、进水、屏幕碎、"
            "键盘失灵、跑气）；如果不是在找瑕疵品，keywords 返回空列表。")
        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.post(f"{AI_ROUTER_URL}/api/search/keywords",
                                     json={"query": content})
        parsed = (resp.json() or {}).get("parsed") or {}
        variants = [str(v).strip() for v in (parsed.get("keywords") or [])
                    if str(v).strip()]
    except Exception as e:
        logger.debug("AI 瑕疵词兜底失败（忽略）: %r", e)
        return []
    variants = variants[: _expand_max_variants()]
    _lexicon_store.set_ai_variants(norm, variants)
    # 从 AI 变体反解瑕疵单词入学习库（剥设备词取残余）
    new_terms = [r for r in (_extract_residual(v) for v in variants) if r]
    added = _lexicon_store.add_ai_defect_terms(new_terms)
    if added:
        logger.info("词库 AI 学习: %s → 新增瑕疵词 %s", keyword, added)
    return variants


def _learn_from_results(keyword: str, results: List[Dict[str, Any]],
                        defect_context: bool) -> None:
    """采集结果反哺词库（增量进化的「记忆」层）：命中强化 + 候选挖掘转正。"""
    if _lexicon_store is None or not results:
        return
    try:
        from common.keyword_expander import DEFECT_FAMILIES
        known = {f for _c, forms in DEFECT_FAMILIES for f in forms}
        known |= _lexicon_store.all_known_defect_forms()
        titles = [str(i.get("title") or "") for i in results if i.get("title")]
        stats = _lexicon_store.learn_from_titles(titles, known, defect_context)
        if stats.get("promoted"):
            logger.info("词库挖掘: 本轮新转正 %d 个瑕疵表述（搜索词=%s）",
                        stats["promoted"], keyword)
    except Exception as e:
        logger.debug("词库学习失败（忽略）: %r", e)

logger = logging.getLogger("spider-service")

app = FastAPI(title="GoofishMasterDesktop · Spider Service", version="2.0.0")

# Configuration
AI_ROUTER_URL = os.environ.get("AI_ROUTER_URL", "http://ai-router:8902")
PIPELINE_URL = os.environ.get("PIPELINE_URL", "http://agent-pipeline:8903")
FEISHU_AGENT_URL = os.environ.get("FEISHU_AGENT_URL", "http://feishu-agent:8901")
# 兜底目录必须落在项目内：Docker 遗留的 "/app/data" 在 Windows 会解析成
# <当前盘符>:\app\data，直接在盘符根建垃圾目录（写到项目外）。
DATA_DIR = Path(os.environ.get("DATA_DIR")
                or Path(__file__).resolve().parents[2] / "data" / "spider")
DATA_DIR.mkdir(parents=True, exist_ok=True)

# State
_state = {
    "active_tasks": {},
    "results_cache": [],
    "start_time": time.time(),
}

# Global crawl serializer. Each search launches its own Chromium and hits
# Xianyu with rate-limited human-mimic pacing — two concurrent crawls would
# double memory AND double the per-IP request rate (account-ban risk), plus
# contend on the SQLite result store. Queue them instead: callers wait their
# turn (pipeline's 960s budget covers one queued crawl after the speedups).
_search_lock = asyncio.Lock()

# 后台任务强引用集——event loop 对 task 只持弱引用，裸 asyncio.create_task
# 可能被 GC 中途静默回收（2026-08-03 监控轮次蒸发事故的根因）：采集完成→
# 转发 pipeline 的 AI 分析链若被回收，结果抓到了却永不分析/通知
_bg_tasks: set = set()


def _spawn_bg(coro) -> asyncio.Task:
    """create_task + 强引用持有，防止后台协程被 GC 静默回收。"""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


# ============ Request/Response Models ============

class SearchRequest(BaseModel):
    keyword: str
    max_pages: int = 3
    personal_only: bool = False
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    free_shipping: bool = False
    region: Optional[str] = None
    new_publish_option: Optional[str] = None
    ai_analysis: bool = True
    notify: bool = True
    open_id: Optional[str] = None


class TaskCreateRequest(BaseModel):
    name: str
    keyword: str
    max_price: Optional[int] = None
    min_price: Optional[int] = None
    personal_only: bool = False
    interval_minutes: int = 30
    max_pages: int = 3
    notify_open_id: Optional[str] = None


class SpiderStatus(BaseModel):
    status: str
    active_tasks: int
    uptime_seconds: int
    results_count: int
    busy: bool = False  # 全局采集锁是否被持有（V2 一次性搜索的"正在采集"真值）


# ============ Spider Logic ============

def _normalize_v1_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a V1 raw result record (Chinese keys) to the V2 item format."""
    import re as _re
    info = record.get("商品信息", {}) or {}
    seller = record.get("卖家信息", {}) or {}
    title = info.get("商品标题") or record.get("title") or ""
    price_raw = str(info.get("当前售价") or record.get("price") or "0")
    price = _parse_price(price_raw)
    # 定金/预付款类商品的标价不是真实售价，打标供下游风控参考
    deposit_suspected = bool(_re.search(r"定金|预付款|订金|首付", title)) and price < 500
    # 实际字段是 商品图片列表（此前漏查导致 images 恒空、视觉分析拿不到图）
    images = (info.get("商品图片列表") or info.get("商品图片")
              or info.get("图片列表") or info.get("images") or [])
    if isinstance(images, str):
        images = [images]
    # 商品视频（scraper 提取的直链，可能为空）；写入记录供 AI 文本分析参考
    videos = info.get("商品视频链接") or []
    if isinstance(videos, str):
        videos = [videos]
    return {
        "item_id": str(info.get("商品ID") or record.get("item_id") or ""),
        "title": title,
        "description": info.get("商品描述") or info.get("描述") or "",
        "price": price,
        "deposit_suspected": deposit_suspected,
        "url": info.get("商品链接") or record.get("link") or "",
        "images": images,
        "videos": videos,
        "seller_name": (seller.get("卖家昵称") or info.get("卖家昵称")
                        or record.get("seller_nickname") or ""),
        "seller_id": str(seller.get("卖家ID") or ""),
        "location": info.get("发布地区") or info.get("商品所在地") or "",
        "publish_time": info.get("发布时间") or "",
    }


def _parse_price(price_raw: str) -> float:
    """Parse Chinese price text: handles 万 multiplier and noisy prefixes.

    Examples: '¥4600'→4600, '1.2万'→12000, '4600元'→4600, ''→0
    """
    import re as _re
    text = (price_raw or "").replace(",", "").strip()
    if not text:
        return 0.0
    m = _re.search(r"([\d]+(?:\.[\d]+)?)\s*(万)?", text)
    if not m:
        return 0.0
    value = float(m.group(1))
    if m.group(2) == "万":
        value *= 10000
    return value


async def _get_max_result_id(keyword: str) -> int:
    """Baseline max row id for a keyword (for incremental read-back)."""
    from src.infrastructure.persistence.storage_names import build_result_filename
    from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
    from src.infrastructure.persistence.sqlite_connection import sqlite_connection

    def _query() -> int:
        bootstrap_sqlite_storage()
        with sqlite_connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS max_id FROM result_items"
                " WHERE result_filename = ?",
                (build_result_filename(keyword),),
            ).fetchone()
            return int(row["max_id"] if row else 0)

    return await asyncio.to_thread(_query)


async def _load_keyword_results(keyword: str, since_id: int = 0,
                                 limit: Optional[int] = None,
                                 newest_first: bool = False) -> List[Dict[str, Any]]:
    """Read back records (id > since_id) for a keyword.

    Incremental read-back: avoids returning full history every run
    (memory/performance risk flagged in external review).
    limit + newest_first support the "retry returns recent cache" fallback.
    """
    from src.infrastructure.persistence.storage_names import build_result_filename
    from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
    from src.infrastructure.persistence.sqlite_connection import sqlite_connection

    order = "DESC" if newest_first else "ASC"
    limit_sql = f" LIMIT {int(limit)}" if limit else ""

    def _query() -> List[Dict[str, Any]]:
        bootstrap_sqlite_storage()
        with sqlite_connection() as conn:
            rows = conn.execute(
                "SELECT raw_json FROM result_items"
                f" WHERE result_filename = ? AND id > ? ORDER BY id {order}{limit_sql}",
                (build_result_filename(keyword), since_id),
            ).fetchall()
            records = []
            for row in rows:
                try:
                    records.append(json.loads(row["raw_json"]))
                except Exception:
                    continue
            return records

    records = await asyncio.to_thread(_query)
    items = []
    for rec in records or []:
        item = _normalize_v1_record(rec)
        if item["item_id"]:
            items.append(item)
    return items


# 全局采集锁整体超时兜底（2026-08-03 加固）：即便内部看门狗都未兜住，
# 超时取消也能释放 _search_lock，避免排队任务被永久拖死（此前需重启容器）。
SPIDER_SEARCH_TIMEOUT = float(os.environ.get("SPIDER_SEARCH_TIMEOUT", "1500"))


async def run_spider_search(request: SearchRequest) -> Dict[str, Any]:
    """Execute a spider search using the original scraper.

    Serialized via _search_lock: one crawl at a time (see lock declaration).
    """
    if _search_lock.locked():
        logger.info("Crawl busy, queued: keyword=%s", request.keyword)
    # 瑕疵词展开后单次要跑多个变体 query（每个约 8 分钟），超时按变体数放大；
    # 静态词库未命中瑕疵词时 AI 兜底可能补足到上限，超时按上限预留（只是上界，
    # 不影响正常返回速度）
    static_keywords = _expand_keyword(request.keyword)
    if _classify_keyword(request.keyword)["defect"] is None:
        n_variants = max(len(static_keywords), _expand_max_variants())
    else:
        n_variants = max(1, len(static_keywords))
    timeout = SPIDER_SEARCH_TIMEOUT * n_variants
    async with _search_lock:
        try:
            return await asyncio.wait_for(
                _run_spider_search_locked(request), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error("搜索 %s 超过 %ss 仍未返回，强制取消并释放采集锁",
                         request.keyword, timeout)
            return {
                "task_id": f"search_timeout_{request.keyword}",
                "status": "failed",
                "keyword": request.keyword,
                "error": f"采集超时（>{timeout:.0f}s），已取消",
                "results_count": 0,
                "results": [],
            }


async def _scrape_one_keyword(task_config: Dict[str, Any], keyword: str,
                              scraper_mod) -> tuple:
    """采集单个（变体）关键词并读回本轮新增记录。

    返回 (results, scrape_error, processed_count)。scrape_error 为
    login_expired / risk_control / None（业务级失败码透出）。
    """
    from src.scraper import scrape_xianyu

    task_config = dict(task_config, keyword=keyword)
    baseline_id = await _get_max_result_id(keyword)
    processed_count = await scrape_xianyu(task_config, debug_limit=0)

    scrape_error = scraper_mod.LAST_SCRAPE_ERROR
    if scrape_error == "login_expired":
        _write_login_health("expired", "采集时被重定向到 passport 登录页")
    elif not scrape_error:
        _write_login_health("ok")

    results = await _load_keyword_results(keyword, since_id=baseline_id)
    if not results and processed_count == 0:
        # 重试兜底：本轮无新发现（V1 链接去重）时返回近期库存记录
        results = await _load_keyword_results(
            keyword, since_id=0, limit=30, newest_first=True)
        if results:
            logger.info("无新增，回退返回 %d 条近期库存记录: %s",
                        len(results), keyword)
    return results, scrape_error, processed_count


async def _run_spider_search_locked(request: SearchRequest) -> Dict[str, Any]:
    task_id = f"search_{uuid.uuid4().hex[:8]}"
    keywords = _expand_keyword(request.keyword)
    # 静态词库（含学习库）未命中瑕疵词 → AI 兜底理解，变体补进本轮搜索
    ai_used = False
    if _classify_keyword(request.keyword)["defect"] is None:
        ai_variants = await _ai_defect_variants(request.keyword)
        if ai_variants:
            ai_used = True
            seen_kw = set(keywords)
            room = max(0, _expand_max_variants() - len(keywords))
            for v in ai_variants:
                if room <= 0:
                    break
                if v not in seen_kw:
                    seen_kw.add(v)
                    keywords.append(v)
                    room -= 1
    if len(keywords) > 1:
        logger.info("Starting search: %s (keyword=%s → 展开 %d 词%s: %s)",
                    task_id, request.keyword, len(keywords),
                    "（含 AI 兜底）" if ai_used else "", keywords)
    else:
        logger.info("Starting search: %s (keyword=%s)", task_id, request.keyword)

    task_config = {
        "task_name": task_id,
        "keyword": request.keyword,
        "max_pages": request.max_pages,
        "personal_only": request.personal_only,
        # V1 引擎用 Playwright fill() 填入价格筛选，必须是字符串
        "min_price": str(request.min_price) if request.min_price is not None else None,
        "max_price": str(request.max_price) if request.max_price is not None else None,
        "free_shipping": request.free_shipping,
        "region": request.region,
        "new_publish_option": request.new_publish_option,
        "enabled": True,
        "account_state_file": "",
    }

    # V2 架构下 AI 分析由 agent-pipeline 统一负责。
    # 仅当调用方显式要求 V1 内部分析（ai_analysis=True 的直连场景）时，
    # 才把 prompt 内容交给引擎；否则引擎内部按"未配置 prompt"跳过，
    # 避免与 pipeline 的三维分析重复烧 token。
    if request.ai_analysis:
        prompt_file = Path("prompts/base_prompt.txt")
        if prompt_file.exists():
            task_config["ai_prompt_text"] = prompt_file.read_text(encoding="utf-8")

    try:
        from src import scraper as scraper_mod

        all_results: List[Dict[str, Any]] = []
        seen_keys: set = set()
        scrape_error: Optional[str] = None
        processed_total = 0

        # 逐变体采集（同一锁内顺序执行，浏览器池自动复用实例）；
        # 登录失效/风控立即中止剩余变体——继续采只会加重风控。
        for kw in keywords:
            results, scrape_error, processed = await _scrape_one_keyword(
                task_config, kw, scraper_mod)
            processed_total += processed or 0
            for item in results:
                dedup_key = (item.get("item_id") or item.get("url")
                             or item.get("title") or "")
                if not dedup_key or dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                item["_matched_keyword"] = kw  # 记录命中变体，便于排查
                all_results.append(item)
            if scrape_error in ("login_expired", "risk_control"):
                logger.warning("Search %s: %s，中止剩余 %d 个变体",
                               task_id, scrape_error,
                               len(keywords) - keywords.index(kw) - 1)
                break

        # 采集结果反哺学习词库：命中强化 + （瑕疵语境下）候选挖掘转正
        _learn_from_results(
            request.keyword, all_results,
            defect_context=ai_used or
            _classify_keyword(request.keyword)["defect"] is not None)

        logger.info("Search %s completed: variants=%d, processed=%s, stored=%d",
                    task_id, len(keywords), processed_total, len(all_results))

        _state["results_cache"].extend(all_results)
        if len(_state["results_cache"]) > 1000:
            _state["results_cache"] = _state["results_cache"][-500:]

        if request.ai_analysis and all_results:
            _spawn_bg(_send_to_pipeline(all_results, request.keyword, request.open_id))

        return {
            "task_id": task_id,
            "status": "completed",
            "keyword": request.keyword,
            "expanded_keywords": keywords,
            "results_count": len(all_results),
            "results": all_results[:50],
            "login_expired": scrape_error == "login_expired",
            "risk_control": scrape_error == "risk_control",
        }

    except Exception as e:
        logger.error("Search %s failed: %s", task_id, e)
        return {
            "task_id": task_id,
            "status": "failed",
            "keyword": request.keyword,
            "error": str(e),
            "results_count": 0,
            "results": [],
        }


async def _send_to_pipeline(results: List[Dict], keyword: str,
                             open_id: Optional[str] = None) -> None:
    """Send spider results to agent pipeline for AI analysis."""
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            await client.post(f"{PIPELINE_URL}/api/analyze/batch", json={
                "items": results[:20],
                "keyword": keyword,
                "open_id": open_id,
            })
        logger.info("Sent %d results to pipeline for analysis", len(results[:20]))
    except Exception as e:
        logger.error("Failed to send to pipeline: %s", e)


# ============ API Endpoints ============

@app.post("/api/search")
async def search(request: SearchRequest, background_tasks: BackgroundTasks):
    """Trigger a Xianyu search."""
    if not request.keyword.strip():
        raise HTTPException(status_code=400, detail="关键词不能为空")
    background_tasks.add_task(run_spider_search, request)
    return {"status": "processing", "keyword": request.keyword,
            "message": "搜索已启动，结果将通过飞书推送"}


@app.post("/api/search/sync")
async def search_sync(request: SearchRequest):
    """Trigger a synchronous Xianyu search (waits for results)."""
    if not request.keyword.strip():
        raise HTTPException(status_code=400, detail="关键词不能为空")
    # 任务句柄登记：/api/search/stop 据此取消（CancelledError 沿
    # wait_for → scrape_xianyu 传播，采集循环已对该异常做优雅退出）。
    task = asyncio.create_task(run_spider_search(request))
    _state["current_search_task"] = task
    try:
        return await task
    except asyncio.CancelledError:
        logger.info("搜索已被手动停止: keyword=%s", request.keyword)
        return {
            "task_id": f"search_stopped_{request.keyword}",
            "status": "stopped",
            "keyword": request.keyword,
            "results_count": 0,
            "results": [],
        }
    finally:
        _state["current_search_task"] = None


@app.post("/api/search/stop")
async def search_stop():
    """手动停止当前进行中的同步搜索（pipeline「停止搜索」链路调用）。

    协作式取消：cancel 任务句柄，采集循环在下一个 await 点收到
    CancelledError 后优雅退出（浏览器上下文由 scraper 的 finally 回收）。
    """
    task = _state.get("current_search_task")
    if task is None or task.done():
        return {"success": False, "message": "当前没有进行中的搜索"}
    task.cancel()
    logger.info("收到停止搜索指令，已取消当前采集任务")
    return {"success": True, "message": "停止指令已发送"}


@app.post("/api/task/create")
async def create_task(request: TaskCreateRequest):
    """Create a monitoring task."""
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    task = {
        "task_id": task_id, "name": request.name, "keyword": request.keyword,
        "max_price": request.max_price, "min_price": request.min_price,
        "personal_only": request.personal_only,
        "interval_minutes": request.interval_minutes,
        "status": "running", "created_at": time.time(), "found_count": 0,
    }
    _state["active_tasks"][task_id] = task
    logger.info("Created task: %s (%s)", task_id, request.name)
    return {"task_id": task_id, "status": "running", "message": f"监控任务已创建: {request.name}"}


@app.get("/api/task/list")
async def list_tasks():
    """List all monitoring tasks."""
    return {"tasks": list(_state["active_tasks"].values())}


@app.post("/api/task/{task_id}/stop")
async def stop_task(task_id: str):
    """Stop a monitoring task."""
    task = _state["active_tasks"].get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    task["status"] = "stopped"
    return {"task_id": task_id, "status": "stopped"}


@app.get("/api/results")
async def get_results(limit: int = 50, keyword: Optional[str] = None):
    """Get recent search results."""
    results = _state["results_cache"]
    if keyword:
        results = [r for r in results if keyword.lower() in r.get("title", "").lower()]
    return {"total": len(results), "results": results[-limit:]}


# results_count 持久化缓存：V1 引擎结果实际落 /app/data/app.sqlite3 的
# result_items 表（实测 301 条；jsonl 映射目录在本链路不落盘）。
# 60s 缓存避免每次 status 都查库。
_results_count_cache = {"count": 0, "ts": 0.0}


def _count_persisted_results() -> int:
    now = time.time()
    if now - _results_count_cache["ts"] < 60:
        return _results_count_cache["count"]
    total = 0
    try:
        import sqlite3
        db_path = DATA_DIR / "app.sqlite3"
        if db_path.exists():
            conn = sqlite3.connect(str(db_path), timeout=5)
            try:
                total = conn.execute("SELECT COUNT(*) FROM result_items").fetchone()[0]
            finally:
                conn.close()
    except Exception as e:
        logger.debug("result_items count failed: %s", e)
    _results_count_cache.update({"count": total, "ts": now})
    return total


@app.get("/api/status", response_model=SpiderStatus)
async def get_status():
    """Get spider service status.

    results_count 改为 sqlite result_items 持久计数（原内存 results_cache
    容器重建即清零，WebUI「结果」恒为 0）；active_tasks 保持内存——
    「活动任务」本来就是瞬态语义，重建后确实没有在跑的任务。
    busy=采集锁持有状态：V2 交互搜索走一次性 /api/search/sync，不进
    active_tasks，WebUI「采集」长期恒 0 误导（2026-08-03 用户实锤），
    改用 busy 表达「此刻是否在采」。"""
    return SpiderStatus(
        status="running", active_tasks=len(_state["active_tasks"]),
        uptime_seconds=int(time.time() - _state["start_time"]),
        results_count=_count_persisted_results(),
        busy=_search_lock.locked(),
    )


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "spider-service", "version": "2.0.0"}


# ---- 三级健康模型：liveness / readiness（详见 common/health.py） ----
# 注：下面引用的 STATE_DIR / DEFAULT_STATE_FILE 定义在本段之后，但均在函数体内
# 延迟求值，调用时模块已完成加载，不存在 NameError。
def _load_health_mod():
    try:
        import sys as _sys
        _root = str(Path(__file__).resolve().parents[2])
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        from common import health as _h
        return _h
    except Exception:
        return None


@app.get("/api/health/live")
async def health_live():
    """存活探针：进程在跑即 200，不触碰任何依赖（供看门狗使用）。"""
    return {"status": "alive", "service": "spider-service"}


@app.get("/api/health/ready")
async def health_ready():
    """就绪探针：探测 Playwright 驱动、Chromium、状态目录可写、闲鱼登录态。

    这几项才是「采集到底能不能跑」的真实前提——只看进程活着毫无意义。
    """
    _h = _load_health_mod()
    if _h is None:
        return {"service": "spider-service", "status": "unknown",
                "reasons": ["common.health 不可用"]}

    def _chk_playwright():
        import importlib
        importlib.import_module("playwright.async_api")
        return True, "驱动已就绪"

    def _chk_chromium():
        bdir = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
        if bdir and Path(bdir).is_dir():
            hits = [p.name for p in Path(bdir).iterdir()
                    if p.is_dir() and p.name.startswith("chromium")]
            if hits:
                return True, "随包 Chromium: " + ",".join(sorted(hits)[:2])
        return False, "未找到随包 Chromium，将回退系统 Chrome/Edge"

    def _chk_state_dir():
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        probe = STATE_DIR / ".health_probe"
        probe.write_text("1", encoding="utf-8")
        probe.unlink()
        return True, str(STATE_DIR)

    def _chk_login():
        exists = DEFAULT_STATE_FILE.exists()
        return exists, ("已登录闲鱼" if exists
                        else "尚未登录闲鱼，采集会撞登录墙")

    def _chk_browser_pool():
        """扫码浏览器占用。满额说明有会话没释放，是内存泄漏的早期信号。"""
        n = len(_login_sessions)
        ok = n < _MAX_LOGIN_SESSIONS
        return ok, (f"扫码浏览器 {n}/{_MAX_LOGIN_SESSIONS}"
                    + ("" if ok else "（已满额，新扫码请求会被拒绝）"))

    specs = [
        ("playwright", _chk_playwright, True),
        ("state_dir_writable", _chk_state_dir, True),
        ("chromium", _chk_chromium, False),
        ("xianyu_login", _chk_login, False),
        ("browser_pool", _chk_browser_pool, False),
    ]
    report = await _h.gather_report("spider-service", specs, version="2.0.0")
    from fastapi.responses import JSONResponse as _JR
    return _JR(status_code=200 if report["ready"] else 503, content=report)


# ============ Xianyu Login Endpoints ============

STATE_DIR = Path(os.environ.get("ACCOUNT_STATE_DIR")
                 or Path(__file__).resolve().parents[2] / "data" / "spider-state")
STATE_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_STATE_FILE = STATE_DIR / "xianyu_state.json"

# 登录健康档案：cookies 文件存在 ≠ 会话有效（过期 cookies 照样在）。
# 运行时被动检测（采集撞 passport 重定向）才盖章「失效」，扫码/上传成功盖章「恢复」。
# ⚠️ 必须放 DATA_DIR 而非 STATE_DIR——账号轮换池按 *.json 扫 STATE_DIR，
#    健康档案会被当登录态加载（2026-08-03 投毒事故）；DATA_DIR 不被扫描。
_LOGIN_HEALTH_FILE = DATA_DIR / "login_health.json"


def _write_login_health(status: str, detail: str = "") -> None:
    """Persist runtime-detected login health ('ok' | 'expired')."""
    try:
        _LOGIN_HEALTH_FILE.write_text(json.dumps({
            "status": status,
            "since": time.strftime("%Y-%m-%d %H:%M:%S"),
            "detail": detail,
        }, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("write login health failed: %s", e)


def _read_login_health() -> Dict[str, Any]:
    try:
        if _LOGIN_HEALTH_FILE.exists():
            return json.loads(_LOGIN_HEALTH_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

_login_sessions: Dict[str, Dict[str, Any]] = {}
_LOGIN_SESSION_TTL = 600  # 扫码会话 10 分钟过期，防止浏览器实例泄漏

# 同时存活的扫码浏览器上限。每个 Chromium 常驻 150-300MB，桌面端跑在用户
# 自己的机器上，不能像服务器那样敞开。超限直接拒绝，让用户先用完/等过期。
_MAX_LOGIN_SESSIONS = int(os.environ.get("MAX_LOGIN_SESSIONS", "2"))

# 关浏览器的超时。Windows 上 Chromium 偶发关不掉（页面卡在 JS 死循环时
# CDP 不响应），不设超时会把整个 event loop 一起吊死。
_BROWSER_CLOSE_TIMEOUT = 15


async def _shutdown_browser(pw: Any, browser: Any, label: str = "") -> None:
    """带超时地关闭 browser + 停止 playwright driver。

    两段都必须做：只 close browser 而不 stop playwright，会残留 node.exe
    驱动进程（Playwright 的进程模型是「Python ←stdio→ node driver ←→ chromium」）。
    """
    if browser is not None:
        try:
            await asyncio.wait_for(browser.close(), timeout=_BROWSER_CLOSE_TIMEOUT)
        except Exception as e:
            logger.warning("关闭浏览器失败%s: %s", f"({label})" if label else "", e)
    if pw is not None:
        try:
            await asyncio.wait_for(pw.stop(), timeout=_BROWSER_CLOSE_TIMEOUT)
        except Exception as e:
            logger.warning("停止 playwright 驱动失败%s: %s", f"({label})" if label else "", e)


async def _close_login_session(session_id: str) -> None:
    """从表里摘掉会话并释放其浏览器资源（幂等）。"""
    session = _login_sessions.pop(session_id, None)
    if not session:
        return
    await _shutdown_browser(session.get("playwright"), session.get("browser"),
                            label=session_id)


async def _purge_stale_login_sessions() -> None:
    """Close and drop login sessions older than TTL (browser/process leak guard)."""
    now = time.time()
    stale = [sid for sid, s in _login_sessions.items()
             if now - s.get("created_at", now) > _LOGIN_SESSION_TTL]
    for sid in stale:
        logger.info("清理过期登录会话: %s", sid)
        await _close_login_session(sid)


async def _login_session_janitor() -> None:
    """周期性清扫过期扫码会话。

    原先只在 `/api/login/qrcode/start` 入口顺带清理一次——用户点开二维码后
    直接关掉页面再也不回来，那个 Chromium 就一直挂着，直到下次有人再点扫码。
    改为常驻清扫，60s 一轮。
    """
    while True:
        try:
            await asyncio.sleep(60)
            await _purge_stale_login_sessions()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("登录会话清扫异常: %s", e)


@app.on_event("startup")
async def _start_janitor() -> None:
    _spawn_bg(_login_session_janitor())


@app.on_event("shutdown")
async def _shutdown_login_sessions() -> None:
    """进程退出时兜底回收，避免留下孤儿 chrome.exe / node.exe。"""
    for sid in list(_login_sessions.keys()):
        await _close_login_session(sid)
    # 采集浏览器复用池：退出时回收池内全部实例（含 Playwright 驱动进程）
    try:
        from src.scraper import shutdown_scrape_browser_pool
        await shutdown_scrape_browser_pool()
    except Exception as e:
        logger.warning("回收采集浏览器池失败（忽略）: %s", e)



@app.get("/api/login/status")
async def login_status():
    """Get Xianyu login state status."""
    state_files = []
    if STATE_DIR.exists():
        for f in STATE_DIR.glob("*.json"):
            state_files.append({
                "name": f.name, "size": f.stat().st_size,
                "modified": f.stat().st_mtime,
            })

    has_default = DEFAULT_STATE_FILE.exists()
    has_cookies = False
    if has_default:
        try:
            data = json.loads(DEFAULT_STATE_FILE.read_text(encoding="utf-8"))
            has_cookies = len(data.get("cookies", [])) > 0
        except Exception:
            pass

    # cookies>0 只代表有文件；运行时检测盖章过 expired 才算真失效
    health = _read_login_health()
    expired = health.get("status") == "expired"
    logged_in = has_cookies and not expired

    return {
        "logged_in": logged_in,
        "login_health": health.get("status", "unknown"),
        "expired_since": health.get("since") if expired else None,
        "health_detail": health.get("detail", ""),
        "state_files": state_files,
        "default_state": DEFAULT_STATE_FILE.name if has_default else None,
        "state_dir": str(STATE_DIR),
    }


async def _launch_browser():
    """Launch Playwright browser with anti-detection."""
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled",
              "--no-sandbox", "--disable-dev-shm-usage"],
    )
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        locale="zh-CN", timezone_id="Asia/Shanghai",
    )
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
        window.chrome = {runtime: {}};
    """)
    return pw, browser, context


async def _find_login_frame(page):
    """Find the havana-login iframe (mini_login.htm) if present."""
    for frame in page.frames:
        url = frame.url.lower()
        if "mini_login" in url or "havana" in url:
            logger.info("Found login iframe: %s", frame.url[:100])
            return frame
    # Fallback: look for passport domain (but not the main page itself)
    for frame in page.frames:
        url = frame.url.lower()
        if "passport.goofish.com" in url and frame != page.main_frame:
            logger.info("Found passport iframe: %s", frame.url[:100])
            return frame
    logger.info("No login iframe found, using main page")
    return None


async def _click_qr_tab(target):
    """Try to click QR code login tab. Returns True if clicked."""
    selectors = [
        "text=扫码登录", "text=扫码", "text=二维码登录", "text=二维码",
        "[class*='qrcode']", "[class*='qr-code']", "[class*='qrCode']",
        "[class*='scan-login']", "[class*='scanLogin']",
        "[role='tab']:has-text('扫码')", "button:has-text('扫码')",
        "a:has-text('扫码')", "span:has-text('扫码登录')",
        "div:has-text('扫码登录')", "li:has-text('扫码登录')",
        "[class*='icon-qr']", "[class*='qr-icon']",
        "img[src*='qr']", "svg[class*='qr']",
    ]
    for sel in selectors:
        try:
            elem = target.locator(sel).first
            if await elem.is_visible(timeout=1500):
                await elem.click()
                logger.info("Clicked QR tab: %s", sel)
                return True
        except Exception:
            continue
    return False


async def _capture_qr(target, page):
    """Capture QR code screenshot. Returns bytes or None."""
    # Xianyu login page: QR code is in div.qrcode-login (right side)
    qr_selectors = [
        "div.qrcode-login",
        "div.qrcode-img",
        "div.extra-login-content",
        "canvas",
        "[class*='qrcode']",
    ]
    for sel in qr_selectors:
        try:
            elem = target.locator(sel).first
            if await elem.is_visible(timeout=2000):
                logger.info("Found QR element: %s", sel)
                return await elem.screenshot()
        except Exception:
            continue

    # Fallback: full page
    logger.info("Using full page screenshot as fallback")
    return await page.screenshot(full_page=False)


@app.post("/api/login/qrcode/start")
async def login_qrcode_start():
    """Start QR code login."""
    await _purge_stale_login_sessions()
    session_id = f"login_{uuid.uuid4().hex[:8]}"

    if len(_login_sessions) >= _MAX_LOGIN_SESSIONS:
        raise HTTPException(
            status_code=429,
            detail=(f"已有 {len(_login_sessions)} 个扫码会话在进行中"
                    f"（上限 {_MAX_LOGIN_SESSIONS}）。请先完成或等待其过期（10 分钟）。"))

    # 资源句柄提到 try 外面：异常分支必须能拿到它们做清理。
    # 原实现在 except 里直接 return，浏览器既没关、也没进 _login_sessions
    # （注册发生在截图成功之后），TTL 清理器压根看不见它 → 每次启动失败
    # 泄漏一个 Chromium + 一个 node 驱动进程。网络慢时用户反复点二维码，
    # 几分钟就能把内存吃光。
    pw = browser = None
    registered = False
    try:
        pw, browser, context = await _launch_browser()
        page = await context.new_page()

        # Navigate to goofish login (login form is JS-embedded via havana-login)
        logger.info("Navigating to goofish login page...")
        await page.goto("https://www.goofish.com/login",
                        wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(5000)

        logger.info("Page: url=%s title=%s frames=%d",
                    page.url, await page.title(), len(page.frames))

        # Check for login iframe (havana-login embedder)
        login_frame = await _find_login_frame(page)
        target = login_frame if login_frame else page

        # QR code is displayed alongside SMS login, no tab click needed
        # Just wait for it to render
        await page.wait_for_timeout(3000)

        # Capture QR
        qr_screenshot = await _capture_qr(target, page)
        qr_base64 = base64.b64encode(qr_screenshot).decode()

        _login_sessions[session_id] = {
            "playwright": pw, "browser": browser, "context": context,
            "page": page, "login_frame": login_frame,
            "status": "waiting", "created_at": time.time(),
        }
        registered = True

        return {
            "session_id": session_id, "status": "waiting",
            "qrcode_img": qr_base64,
            "message": "请用闲鱼 App 扫码登录",
        }

    except Exception as e:
        logger.error("QR login start failed: %s", e)
        return {"session_id": session_id, "status": "error",
                "error": str(e), "message": f"启动登录失败: {e}"}
    finally:
        # 只有「没成功交接给 _login_sessions」时才在这里回收；
        # 成功路径的浏览器要留着等用户扫码，由 TTL/成功回调关闭。
        if not registered:
            await _shutdown_browser(pw, browser, label=f"{session_id}/start-failed")


@app.get("/api/login/qrcode/status")
async def login_qrcode_status(session_id: str):
    """Check QR code login status."""
    session = _login_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")

    page = session["page"]
    status = session["status"]

    if status == "waiting":
        try:
            current_url = page.url
            content = await page.content()

            # Login success: no longer on passport/login page
            if "passport.goofish.com" not in current_url and "login" not in current_url.lower():
                storage_state = await session["context"].storage_state()
                DEFAULT_STATE_FILE.write_text(
                    json.dumps(storage_state, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                logger.info("Login state saved to %s", DEFAULT_STATE_FILE)
                _write_login_health("ok", "扫码登录成功")
                session["status"] = "success"

                # 登录态已落盘，浏览器没用了 —— 立刻释放，别等 TTL。
                await _close_login_session(session_id)

                return {"session_id": session_id, "status": "success",
                        "message": "登录成功！状态已保存"}

            if "扫码失败" in content or "登录失败" in content:
                session["status"] = "failed"
                # 失败态同样立即回收：原实现只标状态不关浏览器，
                # 那个 Chromium 要白挂到 TTL 到期（最长 10 分钟）。
                await _close_login_session(session_id)
                return {"session_id": session_id, "status": "failed",
                        "message": "登录失败，请重试"}

        except Exception as e:
            logger.error("QR status check error: %s", e)

        # Refresh screenshot
        try:
            login_frame = session.get("login_frame")
            target = login_frame if login_frame else page
            screenshot = await _capture_qr(target, page)
            img_b64 = base64.b64encode(screenshot).decode()
            return {"session_id": session_id, "status": "waiting",
                    "qrcode_img": img_b64, "message": "等待扫码..."}
        except Exception:
            return {"session_id": session_id, "status": "waiting",
                    "message": "等待扫码..."}

    return {"session_id": session_id, "status": status,
            "message": f"当前状态: {status}"}


# Debug endpoints launch a real browser and return page screenshots/HTML —
# keep them off unless explicitly enabled (no auth on this service).
_DEBUG_ENDPOINTS = os.environ.get("DEBUG_ENDPOINTS", "false").strip().lower() in (
    "1", "true", "yes", "on")


@app.get("/api/login/debug/page")
async def login_debug_page():
    """Debug: dump login page structure. Requires DEBUG_ENDPOINTS=true."""
    if not _DEBUG_ENDPOINTS:
        raise HTTPException(status_code=404, detail="Not found")
    pw = browser = None
    try:
        pw, browser, context = await _launch_browser()
        page = await context.new_page()
        await page.goto("https://www.goofish.com/login",
                        wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(5000)

        url = page.url
        title = await page.title()
        content = await page.content()

        # Get clickable elements
        clickable = await page.evaluate("""() => {
            const elements = [];
            const all = document.querySelectorAll('a, button, [role="tab"], [class*="tab"], [class*="switch"], img, svg, [class*="icon"], [class*="qr"], [class*="scan"], [class*="login"]');
            for (const el of all) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    elements.push({
                        tag: el.tagName, text: el.textContent?.trim()?.substring(0, 50) || '',
                        class: (el.className?.substring?.(0, 80) || ''),
                        id: el.id || '', visible: true,
                        x: Math.round(rect.x), y: Math.round(rect.y),
                        w: Math.round(rect.width), h: Math.round(rect.height),
                    });
                }
            }
            return elements;
        }""")

        iframes = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('iframe')).map(f => ({
                src: f.src, id: f.id, name: f.name
            }));
        }""")

        screenshot = await page.screenshot(full_page=False)
        screenshot_b64 = base64.b64encode(screenshot).decode()

        return {
            "url": url, "title": title,
            "content_length": len(content),
            "iframes": iframes,
            "clickable_elements": clickable[:50],
            "screenshot": screenshot_b64,
            "html_snippet": content[:5000],
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        # 原来 close 写在 return 之前的正常流里，任一步抛异常就漏一个浏览器。
        await _shutdown_browser(pw, browser, label="debug-page")


@app.post("/api/login/state/upload")
async def login_state_upload(body: dict):
    """Upload xianyu_state.json login state."""
    state_data = body.get("state")
    if not state_data:
        raise HTTPException(status_code=400, detail="缺少 state 数据")
    if not isinstance(state_data, dict) or "cookies" not in state_data:
        raise HTTPException(status_code=400, detail="无效的 state 格式")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_STATE_FILE.write_text(
        json.dumps(state_data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Login state uploaded: %d cookies", len(state_data.get("cookies", [])))
    _write_login_health("ok", "手动上传登录态")
    return {"success": True,
            "message": f"登录状态已保存 ({len(state_data.get('cookies', []))} cookies)",
            "file": DEFAULT_STATE_FILE.name}


@app.delete("/api/login/state")
async def login_state_delete():
    """Delete Xianyu login state."""
    deleted = []
    if DEFAULT_STATE_FILE.exists():
        DEFAULT_STATE_FILE.unlink()
        deleted.append(DEFAULT_STATE_FILE.name)
    for f in STATE_DIR.glob("*.json"):
        f.unlink()
        deleted.append(f.name)
    return {"success": True, "deleted": deleted,
            "message": f"已删除 {len(deleted)} 个登录状态文件"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8904))
    import logging
    class _HealthFilter(logging.Filter):
        def filter(self, record):
            return "/api/health" not in record.getMessage()
    logging.getLogger("uvicorn.access").addFilter(_HealthFilter())
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
