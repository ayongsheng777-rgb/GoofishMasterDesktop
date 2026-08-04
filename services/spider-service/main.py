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
    async with _search_lock:
        try:
            return await asyncio.wait_for(
                _run_spider_search_locked(request), timeout=SPIDER_SEARCH_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error("搜索 %s 超过 %ss 仍未返回，强制取消并释放采集锁",
                         request.keyword, SPIDER_SEARCH_TIMEOUT)
            return {
                "task_id": f"search_timeout_{request.keyword}",
                "status": "failed",
                "keyword": request.keyword,
                "error": f"采集超时（>{SPIDER_SEARCH_TIMEOUT:.0f}s），已取消",
                "results_count": 0,
                "results": [],
            }


async def _run_spider_search_locked(request: SearchRequest) -> Dict[str, Any]:
    task_id = f"search_{uuid.uuid4().hex[:8]}"
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
        from src.scraper import scrape_xianyu
        from src import scraper as scraper_mod
        # 记录基线：只读回本次新入库的记录（增量读回）
        baseline_id = await _get_max_result_id(request.keyword)

        # V1 engine returns a processed COUNT and persists records itself
        # (SQLite via result_storage_service); read the records back.
        processed_count = await scrape_xianyu(task_config, debug_limit=0)

        # 业务级失败码透出（登录失效/风控），让调用方分清「真0结果」与「登录过期」
        scrape_error = scraper_mod.LAST_SCRAPE_ERROR
        if scrape_error == "login_expired":
            _write_login_health("expired", "采集时被重定向到 passport 登录页")
        elif not scrape_error:
            # 采集全程无登录错误 = 会话有效（含合法的去重后 0 新增）
            _write_login_health("ok")

        results = await _load_keyword_results(request.keyword, since_id=baseline_id)

        # 重试兜底：本轮无新发现（V1 链接去重）时，返回最近的库存记录，
        # 避免用户失败后重试同一关键词反而拿到"未找到商品"
        if not results and processed_count == 0:
            results = await _load_keyword_results(
                request.keyword, since_id=0, limit=30, newest_first=True)
            if results:
                logger.info("Search %s: 无新增，回退返回 %d 条近期库存记录",
                            task_id, len(results))

        logger.info("Search %s completed: processed=%s, stored=%d",
                    task_id, processed_count, len(results))

        _state["results_cache"].extend(results)
        if len(_state["results_cache"]) > 1000:
            _state["results_cache"] = _state["results_cache"][-500:]

        if request.ai_analysis and results:
            _spawn_bg(_send_to_pipeline(results, request.keyword, request.open_id))

        return {
            "task_id": task_id,
            "status": "completed",
            "keyword": request.keyword,
            "results_count": len(results),
            "results": results[:50],
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
    return await run_spider_search(request)


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


async def _purge_stale_login_sessions() -> None:
    """Close and drop login sessions older than TTL (browser/process leak guard)."""
    now = time.time()
    stale = [sid for sid, s in _login_sessions.items()
             if now - s.get("created_at", now) > _LOGIN_SESSION_TTL]
    for sid in stale:
        session = _login_sessions.pop(sid, None)
        if not session:
            continue
        logger.info("清理过期登录会话: %s", sid)
        try:
            await session["browser"].close()
        except Exception:
            pass
        try:
            await session["playwright"].stop()
        except Exception:
            pass



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

        return {
            "session_id": session_id, "status": "waiting",
            "qrcode_img": qr_base64,
            "message": "请用闲鱼 App 扫码登录",
        }

    except Exception as e:
        logger.error("QR login start failed: %s", e)
        return {"session_id": session_id, "status": "error",
                "error": str(e), "message": f"启动登录失败: {e}"}


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

                try:
                    await session["browser"].close()
                    await session["playwright"].stop()
                except Exception:
                    pass

                return {"session_id": session_id, "status": "success",
                        "message": "登录成功！状态已保存"}

            if "扫码失败" in content or "登录失败" in content:
                session["status"] = "failed"
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

        await browser.close()
        await pw.stop()

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
