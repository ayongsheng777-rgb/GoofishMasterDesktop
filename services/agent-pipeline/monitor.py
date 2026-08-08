# -*- coding: utf-8 -*-
"""Monitor scheduler — executes monitor_tasks on their configured intervals.

Loop: every 60s scan running tasks → due? → spider search → filter
(price / exclude keywords / blacklist / per-task dedupe) → AI analysis →
score >= task threshold → Feishu notification. All state lives in Postgres
(tasks survive restarts); per-task seen items are deduped via seen_items.
"""
from __future__ import annotations
import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import httpx
from typing import Any, Dict, List, Optional

import httpx

import db

logger = logging.getLogger("agent-pipeline.monitor")

SPIDER_URL = os.environ.get("SPIDER_URL", "http://spider-service:8904")
FEISHU_AGENT_URL = os.environ.get("FEISHU_AGENT_URL", "http://feishu-agent:8901")

_scheduler_task: Optional[asyncio.Task] = None
_running_tasks: set = set()
_blacklist_cache: Dict[str, Any] = {"ts": 0.0, "seller_ids": set(), "seller_names": set()}
_last_cleanup: float = 0.0
# 后台任务强引用集——event loop 对 task 只持弱引用，裸 create_task 会被 GC
# 中途回收（2026-08-03 实锤：监控轮次静默蒸发，单飞行集合卡死任务永不调度）
_bg_tasks: set = set()


def _spawn(coro) -> asyncio.Task:
    """create_task + 强引用持有，防止后台协程被 GC 静默回收。"""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


def _to_epoch(value: Any) -> Optional[float]:
    """Normalize a DB timestamp (datetime / ISO string / None) to UTC epoch seconds.

    TIMESTAMP columns are written with CURRENT_TIMESTAMP under PG's UTC
    session, and asyncpg returns them as naive datetimes — treat naive as
    UTC. A value we can't parse returns None (task treated as due), which
    is the safe direction for a monitor scheduler.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            logger.warning("Unparseable last_run value: %r", value)
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _fmt_local(value: Any) -> Optional[str]:
    """Format a DB timestamp for display in local time.

    PG/asyncpg returns TIMESTAMP columns as datetime objects; SQLite returns
    CURRENT_TIMESTAMP as a naive UTC *string* — the old code only handled the
    datetime branch, so under SQLite the raw UTC string leaked to the UI and
    every timestamp displayed 8h early (2026-08-06 实锤：last_run 显示
    10:35，实际 18:35，用户误判监控停摆 8 小时）。
    Treat naive values as UTC and convert to local time.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


# ============ Task CRUD (DB-backed) ============

async def create_task(payload: Dict[str, Any]) -> Dict[str, Any]:
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    name = payload.get("name") or f"{payload.get('keyword', '')}监控"
    row = await db.fetchrow(
        """INSERT INTO monitor_tasks
           (task_id, name, keyword, max_price, min_price, seller_type,
            exclude_keywords, interval_minutes, notify_open_id, created_by, min_score,
            publish_within_days)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
           RETURNING task_id, name, keyword, max_price, interval_minutes, status""",
        task_id, name, payload.get("keyword", ""),
        payload.get("max_price"), payload.get("min_price"),
        payload.get("seller_type"),
        db.to_json(payload.get("exclude_keywords") or []),
        int(payload.get("interval_minutes") or 30),
        payload.get("notify_open_id"), payload.get("created_by"),
        int(payload.get("min_score") or 60),
        (max(1, min(int(payload["publish_within_days"]), 14))
         if payload.get("publish_within_days") else None),
    )
    if row is None:
        # DB unavailable — report honestly instead of pretending success
        raise RuntimeError("数据库不可用，监控任务无法持久化")
    logger.info("Monitor task created: %s (%s)", task_id, name)
    return row


async def list_tasks(include_stopped: bool = True) -> List[Dict[str, Any]]:
    rows = await db.fetch(
        """SELECT task_id, name, keyword, max_price, min_price, status,
                  interval_minutes, found_count, min_score, last_run, created_at
           FROM monitor_tasks ORDER BY created_at DESC LIMIT 100""")
    for r in rows:
        for k in ("last_run", "created_at"):
            r[k] = _fmt_local(r.get(k))
    if include_stopped:
        return rows
    return [r for r in rows if r.get("status") == "running"]


async def stop_task(task_id_or_keyword: str) -> Optional[Dict[str, Any]]:
    row = await db.fetchrow(
        """UPDATE monitor_tasks SET status='stopped', updated_at=CURRENT_TIMESTAMP
           WHERE task_id=$1 OR name ILIKE $2 OR keyword ILIKE $2
           RETURNING task_id, name, status""",
        task_id_or_keyword, f"%{task_id_or_keyword}%")
    return row


async def update_task(task_id_or_keyword: str, interval_minutes: Optional[int] = None,
                      min_score: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Update mutable task params (interval / score threshold) by id or fuzzy name.

    Bounds: interval clamped to [5, 1440] minutes, score to [0, 100] — garbage
    in here would silently break the scheduler's pacing or mute notifications.
    """
    sets: List[str] = []
    args: List[Any] = []
    if interval_minutes is not None:
        args.append(max(5, min(int(interval_minutes), 24 * 60)))
        sets.append(f"interval_minutes=${len(args)}")
    if min_score is not None:
        args.append(max(0, min(int(min_score), 100)))
        sets.append(f"min_score=${len(args)}")
    if not sets:
        return None
    args.extend([task_id_or_keyword, f"%{task_id_or_keyword}%"])
    n = len(args)
    return await db.fetchrow(
        f"""UPDATE monitor_tasks SET {', '.join(sets)}, updated_at=CURRENT_TIMESTAMP
            WHERE task_id=${n-1} OR name ILIKE ${n} OR keyword ILIKE ${n}
            RETURNING task_id, name, interval_minutes, min_score""",
        *args)


async def delete_task(task_id_or_keyword: str) -> Optional[Dict[str, Any]]:
    """Delete a monitor task by id or fuzzy name/keyword (same matching as stop_task).

    历史 bug：只按 task_id 精确匹配（飞书用户给的是关键词，永远删不掉），
    且 `res is not None` 对 "DELETE 0" 也返回 True（假成功）。
    先取出目标行（拿到 task_id 清 seen_items + 返回任务名供回执），再按行删除。
    """
    row = await db.fetchrow(
        """SELECT task_id, name, keyword FROM monitor_tasks
           WHERE task_id=$1 OR name ILIKE $2 OR keyword ILIKE $2""",
        task_id_or_keyword, f"%{task_id_or_keyword}%")
    if not row:
        return None
    res = await db.execute(
        "DELETE FROM monitor_tasks WHERE task_id=$1", row["task_id"])
    await db.execute("DELETE FROM seen_items WHERE task_id=$1", row["task_id"])
    # execute 返回受影响行数；0 说明并发下被抢先删了
    if not res:
        return None
    return row


# ============ Blacklist ============

async def get_blacklist(force: bool = False) -> Dict[str, set]:
    now = time.time()
    if not force and now - _blacklist_cache["ts"] < 60:
        return _blacklist_cache
    rows = await db.fetch("SELECT seller_id, seller_name FROM blacklist")
    _blacklist_cache["seller_ids"] = {r["seller_id"] for r in rows if r.get("seller_id")}
    _blacklist_cache["seller_names"] = {r["seller_name"] for r in rows if r.get("seller_name")}
    _blacklist_cache["ts"] = now
    return _blacklist_cache


async def add_blacklist(seller_name: str, seller_id: str = "",
                        reason: str = "", source: str = "manual") -> bool:
    res = await db.execute(
        "INSERT INTO blacklist (seller_id, seller_name, reason, source) VALUES ($1,$2,$3,$4)",
        seller_id, seller_name, reason, source)
    await get_blacklist(force=True)
    return res is not None


def is_blacklisted(item: Dict[str, Any], blacklist: Dict[str, set]) -> bool:
    seller_name = item.get("seller_name") or ""
    if not seller_name and isinstance(item.get("seller"), dict):
        seller_name = item["seller"].get("name", "") or ""
    seller_id = item.get("seller_id") or ""
    if seller_id and seller_id in blacklist["seller_ids"]:
        return True
    if seller_name:
        for name in blacklist["seller_names"]:
            if name and (name in seller_name or seller_name in name):
                return True
    return False


# ============ Scheduler ============

def start_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(_scheduler_loop())
        logger.info("Monitor scheduler started")


async def _scheduler_loop() -> None:
    global _last_cleanup
    await asyncio.sleep(15)  # let services settle after boot
    while True:
        try:
            tasks = await db.fetch(
                "SELECT * FROM monitor_tasks WHERE status='running'")
            now = time.time()
            for t in tasks:
                last_run_ts = _to_epoch(t.get("last_run"))
                interval = (t.get("interval_minutes") or 30) * 60
                due = last_run_ts is None or (now - last_run_ts) >= interval
                tid = t.get("task_id", "")
                if due and tid and tid not in _running_tasks:
                    _running_tasks.add(tid)
                    _spawn(_run_task_once_safe(t))
            # Periodic seen_items cleanup (every 6h): the dedupe table grows
            # unboundedly otherwise; 7 days far exceeds any monitor interval,
            # and entries older than that are useless for dedupe anyway.
            if now - _last_cleanup > 6 * 3600:
                _last_cleanup = now
                await db.execute(
                    "DELETE FROM seen_items WHERE first_seen < NOW() - INTERVAL '7 days'")
        except Exception as e:
            logger.error("Scheduler loop error: %s", e)
        await asyncio.sleep(60)


async def _run_task_once_safe(task: Dict[str, Any]) -> None:
    tid = task.get("task_id", "")
    try:
        await _run_task_once(task)
    except Exception as e:
        logger.error("Monitor task %s failed: %s", tid, e)
    finally:
        _running_tasks.discard(tid)
        await db.execute(
            "UPDATE monitor_tasks SET last_run=CURRENT_TIMESTAMP WHERE task_id=$1", tid)


async def _alert_login_health(task: Dict[str, Any], kind: str) -> None:
    """Feishu alert for login-expired / risk-control, 6h Redis rate-limited.

    监控是无人值守定时任务：登录失效期间每轮都静默失败，用户可能几天后
    才发现监控一直没干活（2026-08-03 实锤）。限流防刷屏——同一问题 6h
    内只提醒一次。
    """
    from main import _get_redis, send_progress_notification, \
        FEISHU_AGENT_URL, _INTERNAL_HEADERS
    r = await _get_redis()
    if r:
        try:
            # SET NX EX：抢到钥匙才发；抢不到 = 6h 内已提醒过
            if not await r.set(f"alert:{kind}", "1", nx=True, ex=6 * 3600):
                logger.info("alert %s suppressed (rate limited)", kind)
                return
        except Exception as e:
            logger.warning("alert rate-limit check failed: %s", e)

    open_id = task.get("notify_open_id") or ""
    if not open_id:
        # 与 send_feishu_notification 同款默认 open_id 兜底
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{FEISHU_AGENT_URL}/api/status",
                                        headers=_INTERNAL_HEADERS)
                data = resp.json()
                open_id = data.get("configured_open_id", "")
                if not open_id:
                    msgs = data.get("last_messages", [])
                    if msgs:
                        open_id = msgs[-1].get("open_id", "")
        except Exception:
            pass

    if kind == "login_expired":
        await send_progress_notification(
            open_id, "🔐 闲鱼登录态已失效",
            f"监控任务「{task.get('name', '')}」采集时发现闲鱼登录已过期，"
            f"所有搜索/监控已暂停工作。\n\n"
            f"👉 请到管理后台(localhost:8901)「闲鱼登录」重新扫码，恢复后监控自动继续。")
    else:
        await send_progress_notification(
            open_id, "🛡️ 闲鱼触发风控验证",
            f"监控任务「{task.get('name', '')}」采集时触发闲鱼风控，"
            f"建议降低监控频率，稍后自动重试。")


async def _run_task_once(task: Dict[str, Any]) -> None:
    """One monitoring round: search → filter → analyze → notify."""
    from main import analyze_single_item, save_analysis_to_db, send_feishu_notification

    tid = task["task_id"]
    keyword = task.get("keyword", "")
    if not keyword:
        return
    logger.info("Monitor run: %s keyword='%s'", tid, keyword)

    # 1. Spider search（V1 引擎逐商品抓详情页，单页约 8 分钟，给足超时；
    #    连接级失败由重试包装自愈——spider 崩溃后 Docker ~90s 拉起）
    from main import _spider_search_with_retry
    try:
        spider_data = await _spider_search_with_retry({
            "keyword": keyword,
            "max_pages": 1,
            "personal_only": task.get("seller_type") == "personal",
            "max_price": int(task["max_price"]) if task.get("max_price") else None,
            "min_price": int(task["min_price"]) if task.get("min_price") else None,
            "publish_within_days": (int(task["publish_within_days"])
                                    if task.get("publish_within_days") else None),
            "ai_analysis": False,
            "open_id": task.get("notify_open_id") or "",
        }, open_id=task.get("notify_open_id") or "")
    except Exception as e:
        # httpx.ReadTimeout 的 str(e) 为空，必须用 %r
        logger.error("Monitor %s spider search failed: %r", tid, e)
        return

    # 登录失效/风控要立刻飞书告警（监控是无人值守定时任务，静默失败=死监控）
    # 6h Redis 限流防刷屏：失效期间每轮都失败，但只提醒一次
    if spider_data.get("login_expired"):
        logger.error("Monitor %s: login expired", tid)
        await _alert_login_health(task, "login_expired")
        return
    if spider_data.get("risk_control"):
        logger.error("Monitor %s: risk control", tid)
        await _alert_login_health(task, "risk_control")
        return

    # 走到这 = 采集全程登录健康 → 清告警限流键（恢复后再次失效要能重新告警）
    from main import _clear_login_alerts
    await _clear_login_alerts()

    results = spider_data.get("results", []) or []
    if not results:
        logger.info("Monitor %s: no results", tid)
        return

    # 2. Filters
    blacklist = await get_blacklist()
    exclude = [k.lower() for k in (task.get("exclude_keywords") or [])]
    max_price = float(task["max_price"]) if task.get("max_price") else None
    min_price = float(task["min_price"]) if task.get("min_price") else None

    candidates = []
    for item in results:
        title = (item.get("title") or "").lower()
        price = float(item.get("price") or 0)
        if max_price is not None and price > max_price:
            continue
        if min_price is not None and price < min_price:
            continue
        if any(k in title for k in exclude):
            continue
        if is_blacklisted(item, blacklist):
            continue
        item_id = str(item.get("item_id") or item.get("id") or "")
        if not item_id:
            continue
        item["item_id"] = item_id
        candidates.append(item)

    if not candidates:
        logger.info("Monitor %s: all %d results filtered out", tid, len(results))
        return

    # 3. Per-task dedupe
    seen_rows = await db.fetch(
        "SELECT item_id FROM seen_items WHERE task_id=$1", tid)
    seen = {r["item_id"] for r in seen_rows}
    new_items = [i for i in candidates if i["item_id"] not in seen]
    if not new_items:
        logger.info("Monitor %s: %d candidates all seen before", tid, len(candidates))
        return

    # Mark seen immediately to avoid duplicate analysis on overlap
    for item in new_items:
        await db.execute(
            "INSERT INTO seen_items (task_id, item_id) VALUES ($1,$2)"
            " ON CONFLICT (task_id, item_id) DO NOTHING",
            tid, item["item_id"])

    # 4. AI analysis + threshold notify
    min_score = int(task.get("min_score") or 60)
    open_id = task.get("notify_open_id") or ""
    notified = 0
    from main import _reference_price
    ref_price = _reference_price(new_items[:10])
    for item in new_items[:10]:
        # 停止检查点：用户在轮次运行中发「停止」→ 尽快中止剩余分析与推送
        # （实测 2026-08-02：停止后仍推完 6 张卡，用户以为停止没生效；
        #  DB 查询失败返回 None 时不误伤——查不到状态照常运行）
        status = await db.fetchval(
            "SELECT status FROM monitor_tasks WHERE task_id=$1", tid)
        if status is not None and status != "running":
            logger.info("Monitor %s: stopped mid-run, aborting (%d/%d notified)",
                        tid, notified, len(new_items[:10]))
            break
        try:
            result = await analyze_single_item(item, ref_price=ref_price)
        except Exception as e:
            logger.error("Monitor %s analysis failed: %s", tid, e)
            continue
        await save_analysis_to_db(item, result, keyword=keyword)
        if result.get("final_score", 0) >= min_score and open_id:
            await send_feishu_notification(result, open_id,
                                           source=f"监控:{task.get('name', keyword)}")
            notified += 1

    await db.execute(
        "UPDATE monitor_tasks SET found_count=found_count+$2 WHERE task_id=$1",
        tid, notified)
    logger.info("Monitor %s done: %d new, %d notified", tid, len(new_items), notified)
