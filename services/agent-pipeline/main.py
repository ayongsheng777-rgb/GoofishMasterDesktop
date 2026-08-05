# -*- coding: utf-8 -*-
"""Agent Pipeline — Orchestrates the full AI analysis pipeline.

Flow: Spider Results → Seller Agent → Risk Agent → Price Agent → Decision Agent → Feishu Notification
"""
from __future__ import annotations
import asyncio, json, logging, os, re, time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

import db
import monitor

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("agent-pipeline")

app = FastAPI(title="GoofishMasterDesktop · Agent Pipeline", version="2.1.0")

# Service URLs
AI_ROUTER_URL = os.environ.get("AI_ROUTER_URL", "http://ai-router:8902")
FEISHU_AGENT_URL = os.environ.get("FEISHU_AGENT_URL", "http://feishu-agent:8901")
SPIDER_URL = os.environ.get("SPIDER_URL", "http://spider-service:8904")

# Cap concurrent item analyses. Each item fans out to 3 parallel AI calls
# (seller/risk/price), and overlapping monitor tasks would otherwise multiply
# in-flight requests → provider 429s. Default 3 items = max 9 AI requests.
AI_CONCURRENCY = int(os.environ.get("AI_CONCURRENCY", "3"))
_ai_semaphore = asyncio.Semaphore(AI_CONCURRENCY)

# Shared secret for service-to-service calls into feishu-agent's admin APIs
# (its auth middleware accepts this as an alternative to a user session).
_INTERNAL_HEADERS = {}
if os.environ.get("GOOFISH_SECRET_KEY", "").strip():
    _INTERNAL_HEADERS["X-Internal-Token"] = os.environ["GOOFISH_SECRET_KEY"].strip()

# Stats
_stats = {
    "total_analyzed": 0,
    "recommended": 0,
    "filtered": 0,
    "errors": 0,
    "start_time": time.time(),
}

# Last analyzed results per user (for「分析第N个」deep-dive).
# Backed by Redis (24h TTL) so a container restart no longer wipes the
# "analyze the Nth result" context; memory dict stays as L1/fallback.
_last_results: Dict[str, List[Dict[str, Any]]] = {}
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
REDIS_ENABLED = os.environ.get("REDIS_ENABLED", "true").lower() in ("1", "true", "yes", "on")
_LAST_RESULTS_TTL = 24 * 3600
_redis_client = None


async def _get_redis():
    """Lazy Redis client; None when unavailable (memory-only fallback)."""
    global _redis_client
    if _redis_client is False:
        return None
    if _redis_client is not None:
        return _redis_client
    if not REDIS_ENABLED:
        logger.info("Redis 未启用（可选组件），结果缓存降级为内存")
        _redis_client = False
        return None
    try:
        # P1 单用户化：用进程内 fakeredis 替代外部 Redis 服务（零依赖）。
        # 其余 API（set/get/ttl/delete/incr/expire + nx）完全兼容。
        import fakeredis.aioredis as aioredis
        client = aioredis.FakeRedis(decode_responses=True)
        await client.ping()
        _redis_client = client
        logger.info("Redis(fakeredis) connected (last-results cache)")
    except Exception as e:
        logger.warning("Redis(fakeredis) unavailable, last-results cache is memory-only: %s", e)
        _redis_client = False
    return _redis_client or None


async def _clear_login_alerts() -> None:
    """登录恢复健康后清掉告警限流键——否则「恢复→再次失效」的告警会被
    6h 限流吞掉（2026-08-03 实锤：重扫码后二次过期，告警被压制）。"""
    r = await _get_redis()
    if r:
        try:
            await r.delete("alert:login_expired", "alert:risk_control")
        except Exception:
            pass


async def _save_last_results(open_id: str, results: List[Dict[str, Any]]) -> None:
    _last_results[open_id] = results[:10]
    r = await _get_redis()
    if r and open_id:
        try:
            await r.set(f"last_results:{open_id}",
                        json.dumps(results[:10], ensure_ascii=False, default=str),
                        ex=_LAST_RESULTS_TTL)
        except Exception as e:
            logger.warning("Redis save last_results failed: %s", e)


async def _load_last_results(open_id: str) -> List[Dict[str, Any]]:
    cached = _last_results.get(open_id)
    if cached:
        return cached
    r = await _get_redis()
    if r and open_id:
        try:
            raw = await r.get(f"last_results:{open_id}")
            if raw:
                data = json.loads(raw)
                if isinstance(data, list):
                    _last_results[open_id] = data
                    return data
        except Exception as e:
            logger.warning("Redis load last_results failed: %s", e)
    return []


# Last search meta for WebUI 任务中心（type=search 与 monitor 任务并列展示）
# 持久化到磁盘（agent-pipeline DATA_DIR/last_search.json）：fakeredis 是进程内
# 内存、非持久化，重启即清空 → 搜索任务「消失」。落盘后重启可恢复可见性。
_last_search: Dict[str, Any] = {}
_LAST_SEARCH_FILE = db.DATA_DIR / "last_search.json"


async def _save_last_search(meta: Dict[str, Any]) -> None:
    global _last_search
    _last_search = dict(meta)
    # 落盘持久化（优先，重启可恢复）
    try:
        db.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _LAST_SEARCH_FILE.write_text(
            json.dumps(_last_search, ensure_ascii=False, default=str),
            encoding="utf-8")
    except Exception as e:
        logger.warning("Save last_search file failed: %s", e)
    # 次级：fakeredis（跨进程共享，但不持久化）
    r = await _get_redis()
    if r:
        try:
            await r.set("last_search:global",
                        json.dumps(meta, ensure_ascii=False), ex=_LAST_RESULTS_TTL)
        except Exception as e:
            logger.warning("Redis save last_search failed: %s", e)


async def _load_last_search() -> Dict[str, Any]:
    if _last_search:
        return _last_search
    # 优先从磁盘恢复（重启后这里能拿到）
    try:
        if _LAST_SEARCH_FILE.exists():
            data = json.loads(_LAST_SEARCH_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _last_search.update(data)
                return _last_search
    except Exception as e:
        logger.warning("Load last_search file failed: %s", e)
    # 次级：fakeredis
    r = await _get_redis()
    if r:
        try:
            raw = await r.get("last_search:global")
            if raw:
                data = json.loads(raw)
                if isinstance(data, dict):
                    _last_search.update(data)
                    return _last_search
        except Exception as e:
            logger.warning("Redis load last_search failed: %s", e)
    return {}


# ============ Request/Response Models ============

class AnalyzeItemRequest(BaseModel):
    title: str
    description: str = ""
    price: float = 0
    images: List[str] = Field(default_factory=list)
    url: str = ""
    seller_name: str = ""
    seller_id: str = ""
    category: str = ""
    location: str = ""


class BatchAnalyzeRequest(BaseModel):
    items: List[Dict[str, Any]]
    keyword: str = ""
    open_id: Optional[str] = None


class PipelineResult(BaseModel):
    item_id: str
    title: str
    price: float
    url: str
    seller_name: str
    seller_type: str
    seller_score: int
    risk_score: int
    risk_level: str
    risk_reasons: List[str]
    price_score: int
    final_score: int
    recommend: bool
    reasons: List[str]
    grade: str


# ============ Agent Pipeline Core ============

async def analyze_single_item(item: Dict[str, Any],
                              ref_price: float = 0.0) -> Dict[str, Any]:
    """Run a single item through the full Agent pipeline.

    ref_price: 批次主力价位（中位数），用于配件类目不匹配惩罚；0 = 不启用。
    """
    content = json.dumps(item, ensure_ascii=False)

    async with _ai_semaphore:
        async with httpx.AsyncClient(timeout=120) as client:
            # Run all analyses in parallel
            seller_task = client.post(f"{AI_ROUTER_URL}/api/analyze/seller", json=item)
            risk_task = client.post(f"{AI_ROUTER_URL}/api/analyze/risk", json=item)
            price_task = client.post(f"{AI_ROUTER_URL}/api/analyze/price", json=item)

            seller_resp, risk_resp, price_resp = await asyncio.gather(
                seller_task, risk_task, price_task, return_exceptions=True
            )

    # Parse results
    seller_data = _safe_parse(seller_resp)
    risk_data = _safe_parse(risk_resp)
    price_data = _safe_parse(price_resp)
    # 风险分析「跑了但结果废了」（模型输出非法 JSON，重试后仍 parsed=null）
    # 与「没跑」区分开：缺失 ≠ 安全，交给决策层保守降权
    risk_broken = _is_broken_analysis(risk_resp)

    # Decision Agent: combine all scores
    result = _decision_agent(item, seller_data, risk_data, price_data,
                             ref_price=ref_price, risk_broken=risk_broken)
    return result


def _safe_parse(resp) -> Dict[str, Any]:
    """Safely parse AI router response."""
    if isinstance(resp, Exception):
        return {}
    try:
        data = resp.json()
        return data.get("parsed", {}) or {}
    except Exception:
        return {}


def _is_broken_analysis(resp) -> bool:
    """HTTP 成功但 parsed 为空（模型输出非法 JSON，ai-router 重试后仍失败）。

    2026-08-03 实例：ai_logs id=1214 risk 输出完整但字符串内含未转义
    ASCII 引号 → parsed=null → 当 neutral 50 处理 → 高风险面交骗局商品
    被误推。此类必须保守降权而非当中性。
    """
    if isinstance(resp, Exception):
        return False
    try:
        d = resp.json()
        return bool(d.get("success")) and not d.get("parsed")
    except Exception:
        return False


def _to_float(v: Any, default: float = 0.0) -> float:
    """Defensive numeric coercion for AI-returned fields.

    DeepSeek occasionally returns "8.7%" / "65.77分" instead of a number;
    a bare `>` comparison or int() on those raises TypeError/ValueError and
    the whole item is silently dropped from results (observed 2026-08-02,
    ai_logs id=456: discount_rate="8.7%" killed an iPhone 15 Plus).
    """
    if isinstance(v, bool):  # bool is int subclass; never a score
        return default
    if isinstance(v, (int, float)):
        return float(v)
    if v is None:
        return default
    try:
        return float(str(v).replace("%", "").replace("分", "").strip())
    except (TypeError, ValueError):
        return default


def _demand_match_score(item: Dict[str, Any]) -> int:
    """Simple keyword-in-title match heuristic (0-100)."""
    keyword = (item.get("_keyword") or "").strip().lower()
    title = (item.get("title") or "").lower()
    if not keyword or not title:
        return 60  # neutral when no keyword context
    tokens = [t for t in keyword.replace("/", " ").split() if len(t) >= 2]
    if not tokens:
        return 60
    hits = sum(1 for t in tokens if t in title)
    return min(100, int(40 + 60 * hits / len(tokens)))


# ============ 配件类目不匹配惩罚 ============
# 背景：搜「Mac mini M4」时 ¥75 的「M4线」因 price_score 极高而冲到榜首。
# 规则：配件词命中标题 + 价格低于批次主力价位 80%+ → 压到 45 分（推荐线以下）。
# 豁免：搜索词本身是配件；标题含 主机/整机/单机/全套（整机带配件的组合）。
_ACCESSORY_DEVIATION = 0.8   # 价格低于主力价位 80%+ 视为偏离
_ACCESSORY_CAP = 45          # 降权后的分数上限（低于推荐线 60）

_ACCESSORY_WORDS = (
    "电源线", "数据线", "充电线", "连接线", "转接线", "线缆", "线材",
    "充电器", "充电头", "适配器", "配件", "贴膜", "保护膜", "保护壳",
    "保护套", "支架", "底座", "键盘", "鼠标", "耳机", "转接头", "转接器",
    "扩展坞", "包装盒", "说明书", "散热器", "风扇", "螺丝",
    # 2026-08-02 补充：实测漏网配件（防水胶以 78 分混进推荐榜第 3）
    "钢化膜", "手机膜", "镜头膜", "防水胶", "中框", "后盖", "读卡器",
    "手机壳", "卡贴",
)
_HOST_WORDS = ("主机", "整机", "单机", "全套")

# ============ 故障硬降权 ============
# 背景（2026-08-02 b460 任务实测）：AI risk 校准方向性失真——「坏的坏的坏的」
# 铭瑄坏板仅 45 分、「针脚废了」70 分，而「正常使用但便宜」的好板反被打 92 分
# （疑似引流）。坏板靠 price+match 分压线（≥60）混进推荐榜 5 占 3。
# 标题明示损坏是确定性信号，决策层硬规则兜底，不依赖 AI 校准。
_FAULT_WORDS = (
    "坏板", "坏的", "问题板", "故障", "弯针", "点不亮", "不亮机",
    "进水", "烧毁", "烧坏", "料板", "尸体", "维修失败", "当坏",
)
# 针脚类松散表述：「针脚不小心弄变形或歪了」并非「针脚变形」连续子串，
# 用窗口正则覆盖（实测漏网，2026-08-02）
_FAULT_PATTERNS = tuple(re.compile(p) for p in (
    r"针脚.{0,6}(?:废|弯|歪|变形|断|损坏|氧化)",
    r"(?:废|弯|歪|变形|断).{0,4}针脚",
))


def _is_fault_title(title: str) -> bool:
    t = (title or "").lower()
    if not t:
        return False
    for w in _FAULT_WORDS:
        idx = t.find(w)
        while idx >= 0:
            # 否定语境豁免：「非设备故障的退货」「无故障」「不是坏的」
            # （实测误伤 2026-08-02 oesp id=182：退货条款「非设备故障」命中）
            prefix = t[max(0, idx - 3):idx]
            if not re.search(r"[非无没不]\S{0,2}$", prefix):
                return True
            idx = t.find(w, idx + 1)
    return any(p.search(t) for p in _FAULT_PATTERNS)


def _is_accessory_title(title: str) -> bool:
    t = (title or "").lower()
    if not t:
        return False
    # 找配件词最早出现位置（含「M4线」式：字母/数字紧跟「线」，「无线」不命中）
    acc_pos = -1
    for w in _ACCESSORY_WORDS:
        p = t.find(w)
        if p >= 0 and (acc_pos < 0 or p < acc_pos):
            acc_pos = p
    m = re.search(r"[a-z0-9]线", t)
    if m and (acc_pos < 0 or m.start() < acc_pos):
        acc_pos = m.start()
    if acc_pos < 0:
        return False
    # 主机词必须出现在配件词之前才豁免（卖主机顺带配件，如「M4主机 送线」）；
    # 配件词在前、主机词在后（如「M4线 买的整机拿出来的」）卖的就是配件，不豁免
    host_pos = min((t.find(w) for w in _HOST_WORDS if t.find(w) >= 0),
                   default=-1)
    if 0 <= host_pos < acc_pos:
        return False
    return True


def _keyword_is_accessory(keyword: str) -> bool:
    k = (keyword or "").lower()
    if not k:
        return False
    return (any(w in k for w in _ACCESSORY_WORDS)
            or bool(re.search(r"[a-z0-9]线", k)))


def _matches_exclude(item: Dict[str, Any], exclude_keywords: List[str]) -> bool:
    """用户排除词命中判定（exclude_keywords 已 lower）。

    子串匹配之外：排除词含「配件」时按配件词表判定标题——否则
    「不要手机配件」提取出的整词「手机配件」对钢化膜/手机壳/防水胶等
    不含「配件」字样的标题完全失效（observed 2026-08-02: 过滤 0 件）。
    """
    title = (item.get("title") or "").lower()
    if not title:
        return False
    if any(k in title for k in exclude_keywords):
        return True
    if any("配件" in k for k in exclude_keywords) and _is_accessory_title(title):
        return True
    return False


def _median(values: List[float]) -> float:
    s = sorted(v for v in values if v > 0)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _reference_price(items: List[Dict[str, Any]]) -> float:
    """批次主力价位：优先非配件、非故障标题商品的中位价（回退全体）。

    故障板价格（¥30-80）会拖垮中位数——b460 任务实测主力价位被 12 块
    坏板拉到 ¥84（正常板 ¥100-150），导致配件降权阈值与「远低于主力价」
    判定整体失真。"""
    prices = [float(i.get("price") or 0) for i in items]
    non_acc = [p for i, p in zip(items, prices)
               if p > 0
               and not _is_accessory_title(str(i.get("title") or ""))
               and not _is_fault_title(str(i.get("title") or ""))]
    return _median(non_acc) or _median(prices)


def _decision_agent(
    item: Dict[str, Any],
    seller: Dict[str, Any],
    risk: Dict[str, Any],
    price: Dict[str, Any],
    ref_price: float = 0.0,
    risk_broken: bool = False,
) -> Dict[str, Any]:
    """Decision Agent: combine all agent scores into a final recommendation."""

    # Extract scores (all AI-returned numerics go through _to_float: the model
    # occasionally emits "85" / "8.7%" / "65分" as strings)
    seller_type = seller.get("seller_type", "未知")
    seller_score = int(_to_float(seller.get("seller_score", seller.get("confidence", 50)), 50))
    risk_score = int(_to_float(risk.get("risk_score", 50), 50))
    risk_level = risk.get("risk_level", "medium")
    risk_reasons = risk.get("risk_reasons", risk.get("reasons", []))
    price_score = int(_to_float(price.get("price_score", 50), 50))

    # 风险分析 broken（跑了但 JSON 废了）：缺失 ≠ 安全，保守按高风险处理。
    # 2026-08-03 实测：面交骗局商品（AI 实际判 high/82）因 parsed=null 被当
    # neutral 50 而误推。
    if risk_broken:
        risk_score = max(risk_score, 70)
        risk_level = "high"
        risk_reasons = ["风险分析结果异常（模型输出解析失败），保守按高风险处理"]

    # Final Score = 风险安全 30% + 价格优势 30% + 卖家可信 25% + 需求匹配 15%
    # Demand match: keyword tokens present in title (simple heuristic until
    # user preference profiles land)
    match_score = _demand_match_score(item)
    final_score = int(
        (100 - risk_score) * 0.30 +     # Risk safety (inverted)
        price_score * 0.30 +            # Price advantage
        seller_score * 0.25 +           # Seller trust
        match_score * 0.15              # Demand match
    )
    final_score = max(0, min(100, final_score))
    if risk_broken:
        # 风险未知就不进推荐位（59 < 推荐阈值 60）
        final_score = min(final_score, 59)

    # 配件类目不匹配惩罚：配件标题 + 价格低于主力价位 80%+（搜索词本身是配件时豁免）
    demoted = False
    fault = False
    item_price = float(item.get("price") or 0)
    title = str(item.get("title") or "")
    keyword = str(item.get("_keyword") or "")
    if (ref_price > 0 and item_price > 0
            and item_price <= ref_price * (1 - _ACCESSORY_DEVIATION)
            and _is_accessory_title(title)
            and not _keyword_is_accessory(keyword)):
        demoted = True
        logger.info("配件降权: 「%s」¥%.0f << 主力价位 ¥%.0f（-%d%%）",
                    title[:30], item_price, ref_price,
                    int(_ACCESSORY_DEVIATION * 100))
        final_score = min(final_score, _ACCESSORY_CAP)

    # 故障硬降权：标题明示损坏/故障 → 不推荐（确定性信号，优先于 AI 打分）
    if _is_fault_title(title):
        fault = True
        logger.info("故障降权: 「%s」标题明示损坏/故障", title[:30])
        final_score = min(final_score, _ACCESSORY_CAP)

    # Grade
    if final_score >= 90:
        grade = "A"
    elif final_score >= 75:
        grade = "B"
    elif final_score >= 60:
        grade = "C"
    else:
        grade = "D"

    recommend = final_score >= 60

    # Combine reasons
    reasons = []
    if fault:
        reasons.append("标题明示损坏/故障（坏板/针脚/点不亮等），不推荐")
    if demoted:
        reasons.append(f"配件类商品（¥{item_price:.0f} 远低于主力价位 ¥{ref_price:.0f}），已降权")
    if seller_type == "个人卖家":
        reasons.append(f"个人卖家 (可信度 {seller_score}分)")
    elif seller_type != "未知":
        reasons.append(f"{seller_type} (可信度 {seller_score}分)")

    if risk_reasons:
        reasons.extend(risk_reasons[:3])

    if price.get("is_good_deal"):
        reasons.append("价格低于市场价")

    discount_rate = _to_float(price.get("discount_rate", 0))
    if discount_rate > 20:
        reasons.append(f"折扣 {discount_rate:.0f}%")

    return {
        "item_id": item.get("item_id", ""),
        "title": item.get("title", "未知"),
        "price": item.get("price", 0),
        "url": item.get("url", ""),
        "seller_name": item.get("seller_name", item.get("seller", {}).get("name", "未知")),
        "seller_type": seller_type,
        "seller_score": seller_score,
        "risk_score": risk_score,
        "risk_level": risk_level,
        "risk_reasons": risk_reasons,
        "price_score": price_score,
        "final_score": final_score,
        "recommend": recommend,
        "demoted": demoted,
        "fault": fault,
        "reasons": reasons[:5],
        "grade": grade,
        "market_price": _to_float(price.get("market_price", 0)),
        "discount_rate": discount_rate,
    }


async def send_progress_notification(open_id: str, title: str, content: str) -> None:
    """Send a plain progress card (no action button) via feishu-agent.

    Long searches (~10+ min) otherwise give the user zero interim feedback,
    which reads exactly like a dead pipeline.
    """
    if not open_id:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(f"{FEISHU_AGENT_URL}/api/notify",
                              headers=_INTERNAL_HEADERS, json={
                "receive_id": open_id,
                "title": title,
                "content": content,
            })
    except Exception as e:
        logger.warning("Progress notification failed: %s", e)


# ============ Spider 调用韧性 ============

_SPIDER_RETRYABLE = (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ConnectTimeout)


async def _wait_spider_healthy(max_wait: int = 150) -> bool:
    """Poll spider health until OK or timeout (Docker auto-restart takes ~90s)."""
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{SPIDER_URL}/api/health")
                if r.status_code == 200:
                    logger.info("Spider healthy again, retrying search")
                    return True
        except Exception:
            pass
        await asyncio.sleep(5)
    return False


async def _spider_search_with_retry(payload: dict, open_id: str = "") -> dict:
    """Call spider /api/search/sync with one auto-retry on transport failure.

    spider 的 Chromium 在采集收尾（用户数据+图片下载的资源峰值）偶尔崩溃，
    Docker restart policy ~90s 拉起；崩溃瞬间全局锁会让排队任务一并被掐断
    （2026-08-02 19:51 实测：监控采集 28/30 时崩溃，监控+搜索双任务同时
    RemoteProtocolError）。连接级失败 → 等 spider 恢复健康后自动重试一次，
    而不是直接把失败甩给用户。ReadTimeout 不重试（真超时重试大概率再超）。
    """
    last_err: Exception = RuntimeError("unreachable")
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=960) as client:
                resp = await client.post(f"{SPIDER_URL}/api/search/sync", json=payload)
                return resp.json()
        except _SPIDER_RETRYABLE as e:
            last_err = e
            if attempt == 1:
                logger.warning("Spider transport failed (%r), waiting for restart then retrying", e)
                await send_progress_notification(
                    open_id, "🕷️ 采集服务重启中",
                    f"采集服务异常（{type(e).__name__}），等待自动恢复后重试一次…")
                await _wait_spider_healthy(max_wait=150)
            else:
                logger.error("Spider retry also failed: %r", e)
        except Exception as e:  # ReadTimeout 等不可重试错误直接抛出
            raise
    raise last_err


async def send_feishu_notification(
    result: Dict[str, Any],
    open_id: Optional[str] = None,
    source: str = "",
) -> None:
    """Send analysis result to Feishu via feishu-agent."""
    if not open_id:
        # Try to get default open_id from feishu-agent
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{FEISHU_AGENT_URL}/api/status",
                                        headers=_INTERNAL_HEADERS)
                data = resp.json()
                open_id = data.get("configured_open_id", "")
                if not open_id:
                    messages = data.get("last_messages", [])
                    if messages:
                        open_id = messages[-1].get("open_id", "")
        except Exception:
            pass

    if not open_id:
        logger.warning("No open_id available for notification")
        return

    # Build notification card
    score_emoji = "🌟" if result["final_score"] >= 80 else "✅" if result["final_score"] >= 60 else "⚠️"
    risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(result["risk_level"], "⚪")

    content_lines = [
        f"**📦 {result['title'][:50]}**",
        f"💰 价格: ¥{result['price']}",
    ]
    if result.get("market_price"):
        content_lines.append(f"📊 市场价: ¥{result['market_price']}")
    content_lines.extend([
        f"👤 卖家: {result['seller_name']} ({result['seller_type']})",
        f"{risk_emoji} 风险: {result['risk_level']} ({result['risk_score']}分)",
        f"⭐ AI评分: {result['final_score']}分 ({result['grade']}级)",
    ])
    if source:
        content_lines.append(f"🔔 来源: {source}")

    if result.get("reasons"):
        content_lines.append("\n**📋 分析理由**")
        for r in result["reasons"][:4]:
            content_lines.append(f"• {r}")

    if result.get("recommend"):
        content_lines.append("\n✅ **推荐购买**")
    else:
        content_lines.append("\n❌ **不建议购买**")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(f"{FEISHU_AGENT_URL}/api/notify",
                              headers=_INTERNAL_HEADERS, json={
                "receive_id": open_id,
                "title": f"{score_emoji} AI发现商品 ({result['final_score']}分)",
                "content": "\n".join(content_lines),
                "url": result.get("url", ""),
            })
        logger.info("Notification sent for: %s", result["title"][:30])
    except Exception as e:
        logger.error("Failed to send notification: %s", e)


# ============ DB Persistence ============

async def save_analysis_to_db(item: Dict[str, Any], result: Dict[str, Any],
                              keyword: str = "") -> None:
    """Persist goods + risk_analysis (best-effort, non-blocking on failure)."""
    try:
        item_id = str(item.get("item_id") or item.get("id") or "")
        if not item_id:
            return
        goods_id = await db.fetchval(
            """INSERT INTO goods (item_id, title, description, price, market_price,
                   discount_rate, images, url, category, seller_id, seller_name,
                   ai_score, ai_analysis)
               VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11,$12,$13::jsonb)
               ON CONFLICT (item_id) DO UPDATE SET
                   price=EXCLUDED.price, ai_score=EXCLUDED.ai_score,
                   ai_analysis=EXCLUDED.ai_analysis, updated_time=CURRENT_TIMESTAMP
               RETURNING id""",
            item_id, item.get("title", "")[:500],
            (item.get("description") or "")[:4000],
            float(item.get("price") or 0),
            float(result.get("market_price") or 0),
            float(result.get("discount_rate") or 0),
            db.to_json(item.get("images") or []),
            item.get("url", ""), item.get("category", "") or keyword,
            str(item.get("seller_id") or ""), result.get("seller_name", ""),
            int(result.get("final_score") or 0), db.to_json(result),
        )
        if goods_id:
            await db.execute(
                """INSERT INTO risk_analysis (goods_id, risk_score, risk_level,
                       risk_reason, model_used)
                   VALUES ($1,$2,$3,$4::jsonb,$5)""",
                goods_id, float(result.get("risk_score") or 0),
                result.get("risk_level", ""), db.to_json(result.get("risk_reasons") or []),
                "ai-router")
    except Exception as e:
        logger.warning("save_analysis_to_db failed: %s", e)


# ============ API Endpoints ============

@app.post("/api/analyze/item")
async def analyze_item(request: AnalyzeItemRequest, background_tasks: BackgroundTasks):
    """Analyze a single item through the full Agent pipeline."""
    item = request.model_dump()
    result = await analyze_single_item(item)
    _stats["total_analyzed"] += 1
    if result["recommend"]:
        _stats["recommended"] += 1
    else:
        _stats["filtered"] += 1
    background_tasks.add_task(save_analysis_to_db, item, result)
    return {"success": True, "result": result}


@app.post("/api/analyze/batch")
async def analyze_batch(request: BatchAnalyzeRequest, background_tasks: BackgroundTasks):
    """Analyze a batch of items from spider results."""
    items = request.items
    keyword = request.keyword
    open_id = request.open_id

    logger.info("Batch analysis: %d items for keyword '%s'", len(items), keyword)

    ref_price = _reference_price(items)
    if ref_price > 0:
        logger.info("批次主力价位: ¥%.0f（配件偏离 %d%%+ 将降权）",
                    ref_price, int(_ACCESSORY_DEVIATION * 100))

    for item in items:
        item["_keyword"] = keyword  # 决策打分依赖 _keyword，gather 前必须赋值
    _tasks = [analyze_single_item(it, ref_price=ref_price) for it in items]
    _outcomes = await asyncio.gather(*_tasks, return_exceptions=True)
    results = []
    for item, outcome in zip(items, _outcomes):
        if isinstance(outcome, Exception):
            logger.error("Analysis failed for '%s': %s",
                         item.get("商品标题", ""), outcome)
            _stats["errors"] += 1
            continue
        results.append(outcome)
        background_tasks.add_task(save_analysis_to_db, item, outcome, keyword)
        _stats["total_analyzed"] += 1
        if outcome["recommend"]:
            _stats["recommended"] += 1
        else:
            _stats["filtered"] += 1

    # Sort by final score (best first)
    results.sort(key=lambda x: x["final_score"], reverse=True)

    # Send notifications for top items
    if open_id:
        top_items = [r for r in results if r["recommend"]][:5]
        for item_result in top_items:
            background_tasks.add_task(send_feishu_notification, item_result, open_id)

    return {
        "success": True,
        "keyword": keyword,
        "total": len(results),
        "recommended": sum(1 for r in results if r["recommend"]),
        "results": results[:20],
    }


@app.post("/api/pipeline/search")
async def pipeline_search(data: dict, background_tasks: BackgroundTasks):
    """Full pipeline: search via spider → analyze → notify.

    This is the main entry point for the feishu-agent.
    """
    keyword = data.get("keyword", "")
    open_id = data.get("open_id", "")
    max_price = data.get("max_price")
    personal_only = data.get("personal_only", False)
    exclude_keywords = [k.lower() for k in (data.get("exclude_keywords") or []) if k]

    if not keyword:
        raise HTTPException(status_code=400, detail="关键词不能为空")

    logger.info("Pipeline search: keyword='%s', open_id=%s", keyword, open_id)

    # 闲鱼登录态预检：未登录时闲鱼返回登录墙、抓不到任何商品，
    # 不应误导用户为"未找到符合条件的商品"。先查 spider 登录态，
    # 未登录直接提示去「🐟 闲鱼登录」扫码，省下整轮空采集。
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            st = await client.get(f"{SPIDER_URL}/api/login/status")
            login_info = st.json()
        if not login_info.get("logged_in"):
            logger.warning("Pipeline search blocked: xianyu not logged in (keyword='%s')", keyword)
            await _save_last_search({
                "type": "search", "keyword": keyword,
                "time": time.strftime("%Y-%m-%d %H:%M"),
                "status": "failed",
            })
            return {"success": False,
                    "error": "尚未登录闲鱼。请先到控制台「🐟 闲鱼登录」标签扫码登录，"
                             "再发起搜索 / 监控"}
    except Exception as e:
        # 登录态查询失败不阻断搜索（旧版 spider 可能无此接口 / 临时抖动），
        # 交由后续采集环节自然失败并报错
        logger.debug("登录态预检跳过（%r），继续走搜索链路", e)

    # 任务中心可见性：搜索开始立即登记 running 状态（此前只在完成时写
    # last_search，采集+分析约 10 分钟内 WebUI 完全看不到这次搜索，
    # 用户误以为任务丢了——2026-08-03 实锤）
    await _save_last_search({
        "type": "search", "keyword": keyword,
        "time": time.strftime("%Y-%m-%d %H:%M"),
        "status": "running",
    })

    # Step 1: Trigger spider search（V1 引擎逐商品抓详情，单页约 8 分钟）
    # 注意：交互式搜索只用 1 页（约 30 条）——2 页实测 949s 会打穿 900s 超时
    try:
        spider_data = await _spider_search_with_retry({
            "keyword": keyword,
            "max_pages": 1,
            "personal_only": personal_only,
            "max_price": max_price,
            "ai_analysis": False,  # We'll do analysis ourselves
            "open_id": open_id,
        }, open_id=open_id)
    except Exception as e:
        # httpx.ReadTimeout 的 str(e) 为空，必须用 %r 才能看到异常类型
        logger.error("Spider search failed: %r", e)
        await _save_last_search({
            "type": "search", "keyword": keyword,
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "status": "failed",
        })
        return {"success": False,
                "error": f"采集超时或失败({type(e).__name__})，闲鱼页面响应较慢，请稍后重试"}

    # 登录态过期要如实告知（真实解法是重新扫码），不能混同「未找到商品」
    # ——2026-08-03 实锤：登录过期返回 0 结果，用户被误导去换关键词
    if spider_data.get("login_expired"):
        logger.error("Spider login expired, keyword='%s'", keyword)
        await _save_last_search({
            "type": "search", "keyword": keyword,
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "status": "failed",
        })
        return {"success": False,
                "error": "闲鱼登录态已过期。请到管理后台(localhost:8901)「闲鱼登录」重新扫码，然后再试"}

    if spider_data.get("risk_control"):
        logger.error("Spider risk control triggered, keyword='%s'", keyword)
        await _save_last_search({
            "type": "search", "keyword": keyword,
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "status": "failed",
        })
        return {"success": False,
                "error": "闲鱼触发了风控验证，请稍后再试（频繁采集容易触发）"}

    # 采集本身失败（浏览器崩溃 / 异常）要如实告知，不能混同「未找到商品」
    # ——2026-08-05 实锤：status=failed 被当 total=0 处理，用户被误导去换关键词
    if spider_data.get("status") == "failed":
        err = spider_data.get("error") or "采集失败（未知原因）"
        logger.error("Spider search failed (status=failed): %s", err)
        await _save_last_search({
            "type": "search", "keyword": keyword,
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "status": "failed",
        })
        return {"success": False,
                "error": f"采集失败：{err}"}

    # 走到这 = 采集全程登录健康 → 清告警限流键（恢复后再次失效要能重新告警）
    await _clear_login_alerts()

    results = spider_data.get("results", [])
    if not results:
        await _save_last_search({
            "type": "search", "keyword": keyword,
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "status": "done", "total": 0, "recommended": 0,
        })
        return {
            "success": True,
            "keyword": keyword,
            "total": 0,
            "message": "未找到相关商品",
        }

    # Step 1.5: blacklist filter + user exclude keywords（如「不要配件」）
    blacklist = await monitor.get_blacklist()
    before = len(results)
    results = [i for i in results if not monitor.is_blacklisted(i, blacklist)]
    if before != len(results):
        logger.info("Blacklist filtered %d items for '%s'", before - len(results), keyword)
    if exclude_keywords:
        before = len(results)
        results = [i for i in results if not _matches_exclude(i, exclude_keywords)]
        if before != len(results):
            logger.info("Exclude keywords %s filtered %d items for '%s'",
                        exclude_keywords, before - len(results), keyword)

    # Progress update: crawl done, AI analysis starting (the wait up to this
    # point is ~8 min; analysis adds a few more — tell the user we're alive)
    analyze_count = min(len(results), 20)
    await send_progress_notification(
        open_id,
        f"📦 采集完成：{len(results)} 件商品",
        f"关键词: **{keyword}**\n"
        f"正在对前 {analyze_count} 件做 AI 分析（卖家/风险/价格三维），"
        f"预计 {max(1, analyze_count * 8 // 60) + 1} 分钟…\n"
        f"高分商品分析完自动推送卡片")

    # Step 2: AI Analysis（并发：三维已并行，外层用 gather 真正并发，
    # 信号量 _ai_semaphore 自动限流；单件异常隔离不中断整批）
    ref_price = _reference_price(results[:20])
    if ref_price > 0:
        logger.info("批次主力价位: ¥%.0f（配件偏离 %d%%+ 将降权）",
                    ref_price, int(_ACCESSORY_DEVIATION * 100))
    for item in results[:20]:
        item["_keyword"] = keyword  # 决策打分依赖 _keyword，gather 前必须赋值
    _tasks = [analyze_single_item(it, ref_price=ref_price) for it in results[:20]]
    _outcomes = await asyncio.gather(*_tasks, return_exceptions=True)
    analyzed = []
    for item, outcome in zip(results[:20], _outcomes):
        if isinstance(outcome, Exception):
            logger.error("Analysis failed for '%s': %s",
                         item.get("商品标题", ""), outcome)
            continue
        analyzed.append(outcome)
        background_tasks.add_task(save_analysis_to_db, item, outcome, keyword)

    analyzed.sort(key=lambda x: x["final_score"], reverse=True)

    # Cache per user for「分析第N个」(Redis-backed, survives restarts)
    if open_id:
        await _save_last_results(open_id, analyzed)

    # Step 3: Send notifications for recommended items
    recommended = [r for r in analyzed if r["recommend"]]
    if open_id and recommended:
        for r in recommended[:5]:
            background_tasks.add_task(send_feishu_notification, r, open_id)

    # 记录最近一次搜索（WebUI 任务中心展示；Redis 持久，重建不丢）
    await _save_last_search({
        "type": "search", "keyword": keyword,
        "time": time.strftime("%Y-%m-%d %H:%M"),
        "status": "done",
        "total": len(analyzed), "recommended": len(recommended),
    })

    return {
        "success": True,
        "keyword": keyword,
        "total": len(analyzed),
        "recommended": len(recommended),
        "results": analyzed[:10],
    }


# ============ Deep Analysis（「分析第N个」） ============

@app.post("/api/pipeline/analyze_index")
async def analyze_index(data: dict, background_tasks: BackgroundTasks):
    """Deep analysis of the Nth item from the user's last search results."""
    open_id = data.get("open_id", "")
    index = int(data.get("index") or 0)
    keyword = data.get("keyword", "")

    cached = await _load_last_results(open_id)
    item: Optional[Dict[str, Any]] = None
    if index >= 1 and cached and index <= len(cached):
        # Rebuild a minimal item dict from the cached analysis result
        r = cached[index - 1]
        item = {
            "item_id": r.get("item_id", ""),
            "title": r.get("title", ""),
            "price": r.get("price", 0),
            "url": r.get("url", ""),
            "seller_name": r.get("seller_name", ""),
        }
    elif keyword:
        item = {"title": keyword, "price": 0}

    if not item:
        return {"success": False,
                "error": "没有可分析的商品。请先发送「找 + 商品名」搜索，再发送「分析第N个」"}

    async def _deep_analyze_and_notify():
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                resp = await client.post(f"{AI_ROUTER_URL}/api/analyze/product",
                                         json=item)
                data = resp.json()
            combined = data.get("combined", {})
            if open_id and combined:
                result = {
                    "title": item.get("title", "未知"),
                    "price": item.get("price", 0),
                    "url": item.get("url", ""),
                    "seller_name": item.get("seller_name", "未知"),
                    "seller_type": combined.get("seller_type", "未知"),
                    "seller_score": combined.get("seller_score", 0),
                    "risk_score": combined.get("risk_score", 0),
                    "risk_level": combined.get("risk_level", "未知"),
                    "risk_reasons": combined.get("reasons", []),
                    "price_score": combined.get("price_score", 0),
                    "final_score": combined.get("final_score", 0),
                    "recommend": combined.get("recommend", False),
                    "reasons": combined.get("reasons", []),
                    "grade": "A" if combined.get("final_score", 0) >= 90 else
                             "B" if combined.get("final_score", 0) >= 75 else
                             "C" if combined.get("final_score", 0) >= 60 else "D",
                    "market_price": 0,
                }
                await send_feishu_notification(result, open_id, source="深度分析")
        except Exception as e:
            logger.error("Deep analysis failed: %s", e)

    background_tasks.add_task(_deep_analyze_and_notify)
    return {"success": True, "message": "深度分析进行中，完成后自动推送"}


# ============ Monitor Endpoints ============

@app.post("/api/monitor/create")
async def monitor_create(data: dict):
    """Create a persistent monitoring task (Postgres-backed)."""
    keyword = (data.get("keyword") or "").strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="关键词不能为空")
    try:
        row = await monitor.create_task(data)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"success": True, "task": row}


@app.get("/api/monitor/list")
async def monitor_list():
    tasks = await monitor.list_tasks()
    return {"tasks": tasks, "total": len(tasks)}


@app.post("/api/monitor/{task_id}/stop")
async def monitor_stop(task_id: str):
    row = await monitor.stop_task(task_id)
    if not row:
        raise HTTPException(status_code=404, detail="未找到匹配的监控任务")
    return {"success": True, "task": row}


@app.post("/api/monitor/{task_id}/update")
async def monitor_update(task_id: str, data: dict):
    """Update a monitor task's interval_minutes / min_score (Feishu「设置」指令)."""
    row = await monitor.update_task(
        task_id,
        interval_minutes=data.get("interval_minutes"),
        min_score=data.get("min_score"))
    if not row:
        raise HTTPException(status_code=404, detail="未找到匹配的监控任务或无可更新参数")
    return {"success": True, "task": row}


@app.delete("/api/monitor/{task_id}")
async def monitor_delete(task_id: str):
    row = await monitor.delete_task(task_id)
    if not row:
        raise HTTPException(status_code=404, detail="未找到匹配的监控任务")
    return {"success": True, "task": row}


# ============ Blacklist Endpoints ============

@app.post("/api/blacklist/add")
async def blacklist_add(data: dict):
    seller_name = (data.get("seller_name") or data.get("target") or "").strip()
    if not seller_name:
        raise HTTPException(status_code=400, detail="卖家名不能为空")
    ok = await monitor.add_blacklist(
        seller_name=seller_name,
        seller_id=data.get("seller_id", ""),
        reason=data.get("reason", "飞书手动拉黑"),
        source=data.get("source", "manual"),
    )
    if not ok:
        raise HTTPException(status_code=503, detail="数据库不可用，拉黑失败")
    return {"success": True, "seller_name": seller_name}


@app.get("/api/blacklist/list")
async def blacklist_list():
    rows = await db.fetch(
        "SELECT id, seller_id, seller_name, reason, source, created_at"
        " FROM blacklist ORDER BY created_at DESC LIMIT 200")
    return {"items": rows, "total": len(rows)}


@app.delete("/api/blacklist/{entry_id}")
async def blacklist_delete(entry_id: int):
    await db.execute("DELETE FROM blacklist WHERE id=$1", entry_id)
    await monitor.get_blacklist(force=True)
    return {"success": True}


@app.get("/api/stats")
async def get_stats():
    """Get pipeline statistics (DB-backed, survives container rebuilds).

    历史 bug：纯内存 _stats 且 pipeline_search 主链路从未更新 → WebUI
    「已分析/推荐」恒为 0。改为 goods 表聚合（ai_score>0 = 已分析，
    >=60 = 推荐），内存计数降级为 session 附属数据。"""
    row = await db.fetchrow(
        """SELECT COUNT(*) FILTER (WHERE ai_score > 0) AS analyzed,
                  COUNT(*) FILTER (WHERE ai_score >= 60) AS recommended,
                  COUNT(*) FILTER (WHERE ai_score > 0 AND ai_score < 60) AS filtered,
                  COUNT(*) AS total_goods
           FROM goods""")
    if row:
        # 监控任务统计与列表（monitor_tasks 表，WebUI 任务中心）
        mon = await db.fetchrow(
            """SELECT COUNT(*) FILTER (WHERE status='running') AS running,
                      COUNT(*) FILTER (WHERE status!='running') AS stopped,
                      COUNT(*) AS total
               FROM monitor_tasks""") or {}
        # 任务中心需展示全部任务（含已停止）——前端 renderTaskCenter 有
        # 「已停止」状态渲染；曾用 include_stopped=False 导致与飞书「任务列表」
        # 不一致（飞书显示 stopped 任务，WebUI 显示"暂无任务"）。
        mon_tasks = await monitor.list_tasks(include_stopped=True)
        last_search = await _load_last_search()
        return {
            "total_analyzed": int(row["analyzed"] or 0),
            "recommended": int(row["recommended"] or 0),
            "filtered": int(row["filtered"] or 0),
            "total_goods": int(row["total_goods"] or 0),
            "errors": _stats["errors"],
            "monitors": {
                "running": int(mon.get("running") or 0),
                "stopped": int(mon.get("stopped") or 0),
                "total": int(mon.get("total") or 0),
                "tasks": [{
                    "type": "monitor", "name": t.get("name", ""),
                    "keyword": t.get("keyword", ""),
                    "interval_minutes": t.get("interval_minutes", 30),
                    "min_score": t.get("min_score", 60),
                    "status": t.get("status", ""),
                    "last_run": t.get("last_run") or "未运行",
                    "found_count": t.get("found_count", 0),
                } for t in mon_tasks[:10]],
            },
            "last_search": last_search,
            "session": {k: v for k, v in _stats.items() if k != "start_time"},
            "source": "database",
            "uptime_seconds": int(time.time() - _stats["start_time"]),
        }
    # DB 不可用时回退内存（可能为 0，但接口不挂）
    return {
        **_stats,
        "total_goods": 0,
        "session": {k: v for k, v in _stats.items() if k != "start_time"},
        "source": "memory",
        "uptime_seconds": int(time.time() - _stats["start_time"]),
    }


@app.get("/api/last_results")
async def get_last_results(open_id: str = ""):
    """上次搜索结果（「结果列表」指令数据源，Redis 24h TTL）。"""
    if not open_id:
        return {"results": [], "total": 0}
    results = await _load_last_results(open_id)
    return {"results": results, "total": len(results)}


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "agent-pipeline", "version": "2.1.0"}


@app.on_event("startup")
async def startup():
    await db.ensure_schema()
    monitor.start_scheduler()
    logger.info("Agent Pipeline 启动完成（监控调度器已启动）")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8903))
    import logging
    class _HealthFilter(logging.Filter):
        def filter(self, record):
            return "/api/health" not in record.getMessage()
    logging.getLogger("uvicorn.access").addFilter(_HealthFilter())
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
