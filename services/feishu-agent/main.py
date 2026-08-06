# -*- coding: utf-8 -*-
"""Feishu Agent Main — Web configuration service + bot manager.

Port: 8901
Provides:
- Web scan-to-configure page (QR code)
- WebSocket long connection for receiving messages
- Command parsing and agent dispatch
- REST API for sending messages
"""
from __future__ import annotations
import asyncio, base64, hmac, json, logging, threading, time, os, sys
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

from feishu_qrcode import FeishuDeviceFlow, generate_qrcode_image
from feishu_bot import FeishuBot, save_credentials, load_credentials
from command_parser import (parse_command, format_help, format_help_card,
                            ai_refine_command)
from card_builder import (build_product_card, build_search_results_card,
                           build_task_card, build_status_card)
from ai_config import (load_ai_config, save_ai_config, fetch_models,
                        test_connection, sync_to_ai_router, DEFAULT_PROVIDERS)
from auth import (is_setup_completed, generate_totp_secret, get_totp_uri,
                   generate_totp_qrcode, verify_totp, setup_totp,
                   authenticate, verify_session, logout, get_current_secret,
                   is_login_limited, record_login_failure, reset_login_failure)
import httpx

# 允许从 feishu-agent 服务内访问 common.config（ROOT = parents[2]）
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from common import config as cfg_mod  # noqa: E402
except Exception:
    cfg_mod = None

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# 日志脱敏（详见 common/logfilter.py）。飞书回调体里带 app_secret / 验证 token。
# 此处 sys.path 已在上方 try 块插入根目录。
try:
    from common.logfilter import install as _install_logfilter
    _install_logfilter()
except Exception:
    pass

logger = logging.getLogger("feishu-agent")

# 兜底目录必须落在项目内（Docker 遗留的 "/app/data" 在 Windows 会写到盘符根）。
DATA_DIR = Path(os.environ.get("DATA_DIR")
                or Path(__file__).resolve().parents[2] / "data" / "feishu-agent")
DATA_DIR.mkdir(parents=True, exist_ok=True)
CRED_FILE = DATA_DIR / "credentials.json"

PIPELINE_URL = os.environ.get("PIPELINE_URL", "http://agent-pipeline:8903")
AI_ROUTER_URL = os.environ.get("AI_ROUTER_URL", "http://ai-router:8902")
SPIDER_URL = os.environ.get("SPIDER_URL", "http://spider-service:8904")

# Load HTML template
_TEMPLATE_FILE = Path(__file__).parent / "templates" / "index.html"
if _TEMPLATE_FILE.exists():
    HTML_PAGE = _TEMPLATE_FILE.read_text(encoding="utf-8")
else:
    HTML_PAGE = "<html><body><h1>Template not found</h1></body></html>"

app = FastAPI(title="GoofishMasterDesktop · Feishu Agent", version="2.0.0")

# 静态资源（项目图标等）。注意：auth 中间件只拦 /api/*，/static 公开可访问。
from fastapi.staticfiles import StaticFiles
_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# ============ Admin API Auth Middleware ============
# All /api/* endpoints require authentication EXCEPT the auth flow itself
# and the health probe. Two accepted credentials:
#   1. X-Auth-Token     — OTP session token (web UI after login)
#   2. X-Internal-Token — shared service secret (GOOFISH_SECRET_KEY) used by
#      agent-pipeline for server-to-server calls (/api/notify, /api/status)
# Previously every admin endpoint was wide open on the public port.
# 精确匹配公开端点：/api/auth/* 是认证流本身，/api/health 是探活。
# 用精确集合避免 /api/healthz、/api/health-debug 等意外公开（2026-08-03 加固）
# /api/health/live 与 /api/health/ready 同属探活链路（桌面控制台与看门狗无
# session 亦需读取）。ready 只回报「已配置 / 未配置」，不回显任何凭证内容；
# 服务仅绑定 127.0.0.1，故公开可接受。
_PUBLIC_API_EXACT = frozenset({"/api/health", "/api/health/live", "/api/health/ready"})
_INTERNAL_TOKEN = os.environ.get("GOOFISH_SECRET_KEY", "").strip()


@app.middleware("http")
async def admin_auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not (
        path in _PUBLIC_API_EXACT or path.startswith("/api/auth/")
    ):
        session_token = request.headers.get("X-Auth-Token", "")
        internal_token = request.headers.get("X-Internal-Token", "")
        authed = verify_session(session_token)
        if not authed and _INTERNAL_TOKEN:
            authed = hmac.compare_digest(internal_token, _INTERNAL_TOKEN)
        if not authed:
            return JSONResponse({"detail": "未认证或会话已过期"}, status_code=401)
    return await call_next(request)

_state = {
    "credentials": None,
    "bot": None,
    "bot_thread": None,
    "bot_running": False,
    "bot_error": "",
    "message_handler": None,
    "poll_tasks": {},
    "last_search_results": [],
    "tasks": {},
    "start_time": time.time(),
}


# ============ Background Task Spawning ============

# 后台任务强引用集——event loop 对 task 只持弱引用，裸 asyncio.create_task
# 可能被 GC 中途静默回收（2026-08-03 监控轮次蒸发事故的根因）
_bg_tasks: set = set()


def _spawn_bg(coro) -> asyncio.Task:
    """create_task + 强引用持有，防止后台协程被 GC 静默回收。"""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


# ============ Pipeline Dispatch ============

async def _format_last_results(open_id: str) -> str:
    """「结果列表」指令：标题+链接简略清单（数据源 pipeline Redis 缓存 24h）。"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{PIPELINE_URL}/api/last_results",
                                    params={"open_id": open_id})
        results = resp.json().get("results", [])
    except Exception as e:
        return f"⚠️ 查询结果失败: {e}"
    if not results:
        return ("📋 暂无搜索结果\n\n"
                "先发送搜索指令（如「找 Mac mini M4」），\n"
                "完成后再发「结果列表」查看全部商品")
    lines = [f"📋 上次搜索结果（共 {len(results)} 个，按分数排序）："]
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "未知")[:28]
        score = r.get("final_score", 0)
        grade = r.get("grade", "")
        price = float(r.get("price") or 0)
        url = r.get("url", "")
        flag = "🔧" if r.get("fault") else ("📦" if r.get("demoted") else "")
        lines.append(f"\n{i}. 【{score}分 {grade}】¥{price:.0f} {flag}{title}")
        if url:
            lines.append(f"🔗 {url}")
    lines.append("\n💡 发送「分析第N个」查看某商品的深度报告")
    return "\n".join(lines)


async def _dispatch_pipeline_search(keyword: str, open_id: str = "",
                                     max_price: Optional[int] = None,
                                     personal_only: bool = False,
                                     exclude_keywords: Optional[list] = None,
                                     max_pages: int = 1) -> None:
    """Dispatch a search request to the agent pipeline."""
    try:
        # pipeline 内部链路长（采集约4-8分钟+AI分析），超时给足；
        # 结果通过 /api/notify 回调推送，此处超时仅影响确认回执
        async with httpx.AsyncClient(timeout=1200) as client:
            resp = await client.post(f"{PIPELINE_URL}/api/pipeline/search", json={
                "keyword": keyword,
                "open_id": open_id,
                "max_price": max_price,
                "personal_only": personal_only,
                "exclude_keywords": exclude_keywords or [],
                "max_pages": max_pages,
            })
            data = resp.json()
            # pipeline 以 200 + success:False 返回业务失败（如采集超时），
            # 必须显式检查并告知用户，否则用户什么都收不到
            if not data.get("success"):
                err = data.get("error", "未知错误")
                logger.error("Pipeline search returned failure: %s", err)
                bot = _state.get("bot")
                if bot and open_id:
                    try:
                        await bot.send_text(open_id,
                            f"❌ 搜索 \"{keyword}\" 失败\n{err}\n请稍后重试或换个关键词")
                    except Exception:
                        pass
                return
            logger.info("Pipeline search completed: %d results, %d recommended",
                        data.get("total", 0), data.get("recommended", 0))
            if data.get("total", 0) == 0:
                bot = _state.get("bot")
                if bot and open_id:
                    try:
                        await bot.send_text(open_id,
                            f"🔍 \"{keyword}\" 未找到符合条件的商品\n"
                            f"可以试试：放宽价格区间 / 换个关键词")
                    except Exception:
                        pass
    except Exception as e:
        logger.error("Pipeline search failed: %r", e)
        # Notify user of failure
        bot = _state.get("bot")
        if bot and open_id:
            try:
                await bot.send_text(open_id,
                    f"❌ 搜索 \"{keyword}\" 失败\n错误: {type(e).__name__}\n请稍后重试")
            except Exception:
                pass


# ============ Pipeline-backed Command Helpers ============

async def _create_monitor_task(keyword: str, max_price: Optional[int],
                                open_id: str, min_price: Optional[int] = None,
                                personal_only: bool = False,
                                exclude_keywords: Optional[list] = None) -> str:
    """Create a persistent monitor task via agent-pipeline (Postgres-backed)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{PIPELINE_URL}/api/monitor/create", json={
                "keyword": keyword,
                "max_price": max_price,
                "min_price": min_price,
                "seller_type": "personal" if personal_only else None,
                "exclude_keywords": exclude_keywords or [],
                "interval_minutes": 30,
                "min_score": 60,
                "notify_open_id": open_id,
                "created_by": open_id,
            })
            data = resp.json()
    except Exception as e:
        logger.error("创建监控任务失败: %s", e)
        return f"❌ 创建监控任务失败\n错误: {e}\n请确认 agent-pipeline 服务正常运行"

    if not data.get("success"):
        return f"❌ 创建监控任务失败\n{data.get('detail', data)}"

    task = data.get("task", {})
    price_text = f"，价格上限 ¥{max_price}" if max_price else ""
    return (f"✅ 监控任务已创建（已持久化，重启不丢失）\n"
            f"任务ID: {task.get('task_id', '')}\n"
            f"关键词: {keyword}{price_text}\n"
            f"监控间隔: 30分钟\n"
            f"推送条件: AI评分 ≥ 60 分\n\n"
            f"发现符合条件的商品时会自动推送通知")


async def _list_monitor_tasks() -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{PIPELINE_URL}/api/monitor/list")
            data = resp.json()
    except Exception as e:
        return f"❌ 获取任务列表失败: {e}"

    tasks = data.get("tasks", [])
    if not tasks:
        return "📋 当前没有监控任务\n\n发送「监控 [商品名]」创建新任务"
    lines = ["📋 监控任务列表：\n"]
    for t in tasks:
        status = "🟢" if t.get("status") == "running" else "🔴"
        price = f" ≤¥{int(t['max_price'])}" if t.get("max_price") else ""
        found = f" | 已发现 {t.get('found_count', 0)}" if t.get("found_count") else ""
        lines.append(f"{status} {t.get('name', '')}{price}{found}")
    lines.append(f"\n共 {len(tasks)} 个任务（停止：发送「停止 + 关键词」）")
    return "\n".join(lines)


async def _stop_monitor_task(target: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{PIPELINE_URL}/api/monitor/{target}/stop")
            if resp.status_code == 404:
                return f"未找到匹配的监控任务: {target}"
            data = resp.json()
    except Exception as e:
        return f"❌ 停止任务失败: {e}"

    if data.get("success"):
        task = data.get("task", {})
        return f"🔴 已停止监控任务: {task.get('name', target)}"
    return f"未找到匹配的监控任务: {target}"


async def _delete_monitor_task(target: str) -> str:
    """真正删除监控任务（含 seen_items 去重记录），与「停止」语义分离。"""
    if not target:
        return "请指定任务名，例如：删除 oesp"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(f"{PIPELINE_URL}/api/monitor/{target}")
            if resp.status_code == 404:
                return f"未找到匹配的监控任务: {target}"
            data = resp.json()
    except Exception as e:
        return f"❌ 删除任务失败: {e}"

    if data.get("success"):
        task = data.get("task") or {}
        return f"🗑️ 已删除监控任务: {task.get('name', target)}"
    return f"未找到匹配的监控任务: {target}"


async def _update_monitor_task(target: str, interval_minutes: Optional[int] = None,
                               min_score: Optional[int] = None) -> str:
    """Update a monitor task's interval / score threshold via pipeline."""
    if not target:
        return "请指定任务名，例如：设置 4090 间隔60分钟"
    payload = {}
    if interval_minutes is not None:
        payload["interval_minutes"] = interval_minutes
    if min_score is not None:
        payload["min_score"] = min_score
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{PIPELINE_URL}/api/monitor/{target}/update", json=payload)
            if resp.status_code == 404:
                return f"未找到匹配的监控任务: {target}"
            data = resp.json()
    except Exception as e:
        return f"❌ 更新任务失败: {e}"

    if data.get("success"):
        task = data.get("task", {})
        parts = []
        if interval_minutes is not None:
            parts.append(f"间隔 {interval_minutes} 分钟")
        if min_score is not None:
            parts.append(f"推送阈值 {min_score} 分")
        return (f"✅ 已更新监控任务: {task.get('name', target)}\n"
                f"新配置: {'，'.join(parts)}\n"
                f"（下一个监控周期生效）")
    return f"未找到匹配的监控任务: {target}"


async def _add_blacklist(target: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{PIPELINE_URL}/api/blacklist/add", json={
                "target": target, "reason": "飞书手动拉黑",
            })
            data = resp.json()
    except Exception as e:
        return f"❌ 拉黑失败: {e}"

    if data.get("success"):
        return (f"🚫 已将 \"{target}\" 加入黑名单（已持久化）\n"
                f"后续搜索与监控将自动过滤该卖家")
    return f"❌ 拉黑失败: {data.get('detail', data)}"


async def _dispatch_analyze_index(index: Optional[int], keyword: str,
                                   open_id: str) -> None:
    """Dispatch deep analysis to pipeline; result pushed via notification."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{PIPELINE_URL}/api/pipeline/analyze_index", json={
                "open_id": open_id, "index": index or 0, "keyword": keyword,
            })
            data = resp.json()
            if not data.get("success"):
                bot = _state.get("bot")
                if bot and open_id:
                    await bot.send_text(open_id, f"⚠️ {data.get('error', '分析失败')}")
    except Exception as e:
        logger.error("深度分析请求失败: %s", e)


async def _handle_xianyu_login(open_id: str) -> Optional[str]:
    """闲鱼扫码登录：把 spider 的登录二维码转发到飞书，后台协程盯扫码结果。"""
    bot = _state.get("bot")
    if not bot or not open_id:
        return "⚠️ 机器人未就绪，请到管理后台「闲鱼登录」页操作"
    # 已是登录态则不必重复扫码
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            st = (await client.get(f"{SPIDER_URL}/api/login/status")).json()
        if st.get("logged_in"):
            return ("✅ 闲鱼当前已是登录状态，无需扫码\n"
                    "如需更换账号，请先到管理后台「闲鱼登录」页清除登录态")
    except Exception:
        pass  # 状态查询失败不阻塞登录流程
    # 启动扫码会话，取二维码（spider 内 Playwright 打开登录页截图）
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            data = (await client.post(f"{SPIDER_URL}/api/login/qrcode/start")).json()
    except Exception as e:
        logger.error("闲鱼扫码登录启动失败: %s", e)
        return f"⚠️ 启动闲鱼登录失败: {e}\n（采集服务需空闲，稍后再试）"
    if data.get("status") == "error" or not data.get("qrcode_img"):
        return f"⚠️ {data.get('message', '获取闲鱼二维码失败')}"
    session_id = data.get("session_id", "")
    # 上传二维码并发送到飞书
    try:
        image_key = await bot.upload_image(base64.b64decode(data["qrcode_img"]))
        await bot.send_image(open_id, image_key)
    except Exception as e:
        logger.error("闲鱼二维码发送飞书失败: %s", e)
        return f"⚠️ 二维码推送失败: {e}\n请到管理后台「闲鱼登录」页扫码"
    # 后台盯扫码结果（强引用防 GC 回收）
    _spawn_bg(_watch_xianyu_login(session_id, open_id, data["qrcode_img"]))
    return ("🐟 闲鱼登录二维码已推送 👆\n\n"
            "请用闲鱼 App 扫码（约 1-2 分钟内有效，二维码轮转会自动补发新图）\n"
            "扫码结果我会主动通知你")


async def _watch_xianyu_login(session_id: str, open_id: str,
                              initial_img: str) -> None:
    """轮询闲鱼扫码结果并推送；二维码轮转时补发新图（限频防刷屏）。"""
    bot = _state.get("bot")
    if not bot:
        return
    last_img = initial_img
    sends = 1
    deadline = time.time() + 240  # 4 分钟兜底
    await asyncio.sleep(8)
    while time.time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                data = (await client.get(
                    f"{SPIDER_URL}/api/login/qrcode/status",
                    params={"session_id": session_id})).json()
        except Exception:
            await asyncio.sleep(8)
            continue
        status = data.get("status")
        if status == "success":
            await bot.send_text(open_id, "✅ 闲鱼登录成功！登录态已保存，采集/监控已恢复")
            return
        if status in ("failed", "error"):
            await bot.send_text(
                open_id,
                f"❌ 闲鱼登录失败：{data.get('message', '请重试')}\n再发一次「闲鱼登录」重试")
            return
        # waiting：二维码轮转则补发（限最多 6 张、≥12s 间隔，防刷屏）
        img = data.get("qrcode_img")
        if img and img != last_img and sends < 6:
            last_img = img
            sends += 1
            try:
                image_key = await bot.upload_image(base64.b64decode(img))
                await bot.send_image(open_id, image_key)
            except Exception as e:
                logger.warning("补发闲鱼二维码失败: %s", e)
            await asyncio.sleep(12)
        else:
            await asyncio.sleep(8)
    await bot.send_text(open_id, "⏰ 闲鱼二维码已过期，未完成扫码\n重新发送「闲鱼登录」获取新二维码")


# ============ Message Handler ============

async def handle_message(payload: dict) -> Optional[str]:
    """Handle incoming Feishu messages and route to appropriate agent."""
    text = payload.get("text", "").strip()
    open_id = payload.get("open_id", "")
    chat_type = payload.get("chat_type", "")

    if not text:
        return None

    logger.info("收到消息: %s (from %s, type=%s)", text, open_id, chat_type)

    cmd = parse_command(text)
    action = cmd.get("action", "help")

    # 搜索/监控指令过一道 AI 理解提炼（正则兜底）：
    # 「要没有升级存储的」这类自然语言正则剥不干净，会让整句残留进搜索词
    if action in ("search", "monitor"):
        cmd = await ai_refine_command(text, cmd)

    if action == "help":
        topic = cmd.get("topic", "")
        if topic:
            from command_parser import format_help_topic
            return format_help_topic(topic)
        return format_help()

    if action == "chatter":
        return ("👋 我在呢～ 直接告诉我你要找什么商品吧\n\n"
                "例如：「找 Mac mini M4 5000以内」「监控 RTX4090 低于8000」\n"
                "发送「帮助」查看全部指令")

    if action == "status":
        uptime = int(time.time() - _state["start_time"])
        hours = uptime // 3600
        mins = (uptime % 3600) // 60
        task_count = "?"
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{PIPELINE_URL}/api/monitor/list")
                task_count = str(resp.json().get("total", 0))
        except Exception:
            task_count = "查询失败"
        return (f"🖥️ 系统状态\n"
                f"运行时间: {hours}小时{mins}分\n"
                f"监控任务: {task_count}\n"
                f"机器人状态: {'🟢 运行中' if _state['bot_running'] else '🔴 未运行'}")

    if action == "xianyu_login":
        return await _handle_xianyu_login(open_id)

    if action == "search":
        keyword = cmd.get("keyword", "")
        conditions = []
        if cmd.get("max_price"):
            conditions.append(f"价格≤{cmd['max_price']}元")
        if cmd.get("seller_type") == "personal":
            conditions.append("个人卖家")
        if cmd.get("condition"):
            conditions.append(cmd["condition"])
        if cmd.get("exclude_mining"):
            conditions.append("排除矿卡")
        if cmd.get("exclude_keywords"):
            conditions.append(f"排除:{'/'.join(cmd['exclude_keywords'])}")

        cond_text = f" ({', '.join(conditions)})" if conditions else ""

        # Dispatch to agent pipeline asynchronously（强引用防 GC 回收）
        _spawn_bg(_dispatch_pipeline_search(
            keyword=keyword,
            open_id=open_id,
            max_price=cmd.get("max_price"),
            personal_only=cmd.get("seller_type") == "personal",
            exclude_keywords=cmd.get("exclude_keywords"),
            max_pages=cmd.get("max_pages", 1),
        ))

        max_pages = cmd.get("max_pages", 1)
        page_text = f" {max_pages}页" if max_pages > 1 else ""
        # 时间估算：单页约 8 分钟采 + 2 分钟分析，每多一页加 8 分钟
        est_mins = 8 + (max_pages - 1) * 8 + 2
        return (f"🔍 已收到搜索请求\n"
                f"关键词: {keyword}{cond_text}{page_text}\n\n"
                f"⏳ 全流程约 {est_mins} 分钟：\n"
                f"闲鱼采集（约{8 if max_pages == 1 else f'{8}~{8 * max_pages}'}分钟）→ AI 三维分析（卖家/风险/价格）\n\n"
                f"💡 采集完成会先收到进度通知，高分商品自动推送卡片\n"
                f"期间可继续发送其他指令")

    if action == "monitor":
        keyword = cmd.get("keyword", "")
        max_price = cmd.get("max_price", 0)
        return await _create_monitor_task(
            keyword=keyword, max_price=max_price, open_id=open_id,
            personal_only=cmd.get("seller_type") == "personal",
            exclude_keywords=cmd.get("exclude_keywords"))

    if action == "list_tasks":
        return await _list_monitor_tasks()

    if action == "list_results":
        return await _format_last_results(open_id)

    if action == "stop_task":
        return await _stop_monitor_task(cmd.get("target", ""))

    if action == "stop_search":
        # 停止进行中的一次性搜索（采集阶段由 pipeline 联动 spider 中断）
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(f"{PIPELINE_URL}/api/search/stop")
                data = resp.json()
        except Exception as e:
            return f"❌ 停止搜索失败: {e}"
        if data.get("success"):
            return f"🛑 {data.get('message', '已发送停止指令')}"
        return f"ℹ️ {data.get('message', '当前没有进行中的搜索任务')}"

    if action == "delete_task":
        return await _delete_monitor_task(cmd.get("target", ""))

    if action == "set_task":
        return await _update_monitor_task(
            cmd.get("target", ""),
            interval_minutes=cmd.get("interval_minutes"),
            min_score=cmd.get("min_score"))

    if action == "analyze":
        keyword = cmd.get("keyword", "")
        index = cmd.get("index")
        _spawn_bg(_dispatch_analyze_index(index, keyword, open_id))
        if index:
            return (f"🔬 正在深度分析第 {index} 个商品...\n\n"
                    f"分析维度:\n"
                    f"• 卖家身份识别\n"
                    f"• 商品风险检测\n"
                    f"• 价格量化评估\n\n"
                    f"⏳ AI 分析完成后自动推送报告")
        return (f"🔬 正在分析: {keyword}\n\n"
                f"⏳ AI 多维度分析中...\n"
                f"分析完成后自动推送结果")

    if action == "blacklist":
        target = cmd.get("target", "")
        return await _add_blacklist(target)

    return format_help()


# ============ Bot Management ============

def _start_bot_in_thread() -> None:
    creds = load_credentials(CRED_FILE)
    if not creds:
        _state["bot_error"] = "无凭证"
        return
    if _state["bot_thread"] and _state["bot_thread"].is_alive():
        return
    try:
        bot = FeishuBot(app_id=creds["app_id"],
                        app_secret=creds["app_secret"],
                        domain=creds.get("domain", "feishu"),
                        message_handler=handle_message)
    except Exception as exc:
        _state["bot_error"] = str(exc)
        return
    _state["bot"] = bot
    _state["bot_error"] = ""

    def _run():
        try:
            _state["bot_running"] = True
            bot.run_forever()
        except Exception as exc:
            _state["bot_error"] = str(exc)
        finally:
            _state["bot_running"] = False

    t = threading.Thread(target=_run, daemon=True, name="feishu-bot")
    _state["bot_thread"] = t
    t.start()
    logger.info("飞书机器人线程已启动")


async def _background_poll(token: str, expires_in: int) -> None:
    flow = FeishuDeviceFlow()
    deadline = time.time() + max(60, min(expires_in, 3600))
    try:
        while time.time() < deadline:
            try:
                poll = await flow.poll_status(token)
            except Exception as exc:
                logger.warning("后台轮询异常: %s", exc)
                await asyncio.sleep(3)
                continue
            if poll.status == "success":
                _handle_success(poll.credentials)
                return
            if poll.status in ("expired", "fail"):
                return
            await asyncio.sleep(2)
    except asyncio.CancelledError:
        pass
    finally:
        _state["poll_tasks"].pop(token, None)


def _handle_success(creds: Dict[str, str]) -> None:
    save_credentials(creds["app_id"], creds["app_secret"],
                     creds.get("tenant_brand", "feishu"), path=CRED_FILE)
    # 回写 config.json 的 feishu.*：桌面控制台「配置概览」据此显示已配置
    # （否则 WebUI 配好后控制台仍显示未配置，与模型配置同源问题）
    if cfg_mod is not None:
        try:
            base = cfg_mod.load_config()
            feishu = base.setdefault("feishu", {})
            feishu["app_id"] = creds.get("app_id", "")
            feishu["app_secret"] = creds.get("app_secret", "")
            cfg_mod.save_config(base)
        except Exception as e:
            logger.warning("Failed to persist feishu creds to config.json: %s", e)
    if creds.get("open_id"):
        (DATA_DIR / "configured_open_id.json").write_text(
            json.dumps({"open_id": creds["open_id"]}), encoding="utf-8")
    _start_bot_in_thread()
    logger.info("扫码配置成功，机器人已启动")


# ============ API Endpoints ============

class SendBody(BaseModel):
    receive_id: str
    text: str
    receive_id_type: str = "open_id"


class NotifyBody(BaseModel):
    receive_id: str
    title: str
    content: str
    url: str = ""
    receive_id_type: str = "open_id"


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


async def _status_payload() -> Dict:
    """Assemble the status body shared by /api/status and system overview.

    The overview must NOT fetch this over HTTP: after the auth middleware
    landed, a localhost self-call without a token gets 401, whose JSON body
    still parses ({"detail": ...}) and silently reads as "not configured /
    bot not running" — a phantom outage.
    """
    creds = load_credentials(CRED_FILE)
    uptime = int(time.time() - _state["start_time"])
    # Load configured open_id
    configured_open_id = ""
    open_id_file = DATA_DIR / "configured_open_id.json"
    if open_id_file.exists():
        try:
            configured_open_id = json.loads(open_id_file.read_text(encoding="utf-8")).get("open_id", "")
        except Exception:
            pass
    active_tasks = 0
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            resp = await client.get(f"{PIPELINE_URL}/api/monitor/list")
            active_tasks = resp.json().get("total", 0)
    except Exception:
        pass
    bot = _state["bot"]
    bot_thread = _state["bot_thread"]
    return {
        "configured": creds is not None,
        "bot_running": _state["bot_running"],
        "bot_error": _state["bot_error"],
        "bot_thread_alive": bool(bot_thread and bot_thread.is_alive()),
        "last_message_at": bot.last_message_at if bot else 0.0,
        "uptime_seconds": uptime,
        "active_tasks": active_tasks,
        "configured_open_id": configured_open_id,
        "last_messages": bot.last_messages[-10:] if bot else [],
    }


@app.get("/api/status")
async def get_status():
    return await _status_payload()


@app.post("/api/qrcode")
async def get_qrcode(domain: str = Query("feishu")):
    flow = FeishuDeviceFlow(domain)
    try:
        result = await flow.fetch_qrcode()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    png = generate_qrcode_image(result.scan_url)
    task = asyncio.create_task(
        _background_poll(result.poll_token, result.expires_in))
    _state["poll_tasks"][result.poll_token] = task
    return {"qrcode_img": png, "poll_token": result.poll_token,
            "expires_in": result.expires_in, "scan_url": result.scan_url}


@app.get("/api/qrcode/status")
async def qrcode_status(token: str = Query(...)):
    flow = FeishuDeviceFlow()
    try:
        poll = await flow.poll_status(token)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if poll.status == "success":
        _handle_success(poll.credentials)
        return {"status": "success", **poll.credentials}
    return {"status": poll.status, "message": poll.message}


@app.post("/api/send")
async def send_message(body: SendBody):
    bot = _state["bot"]
    if bot is None:
        raise HTTPException(status_code=400, detail="机器人未启动")
    try:
        ok = await bot.send_text(body.receive_id, body.text,
                                 body.receive_id_type)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": ok}


@app.post("/api/notify")
async def send_notification(body: NotifyBody):
    """Send a notification card to a user (for AI analysis results)."""
    bot = _state["bot"]
    if bot is None:
        raise HTTPException(status_code=400, detail="机器人未启动")

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": body.title}
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": body.content}}
        ]
    }
    if body.url:
        card["elements"].append({"tag": "hr"})
        card["elements"].append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔗 查看详情"},
                "type": "primary",
                "url": body.url
            }]
        })

    try:
        ok = await bot.send_card(body.receive_id, card, body.receive_id_type)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": ok}


@app.post("/api/reconfigure")
async def reconfigure():
    """彻底清空飞书配置（无残留），回到未配置初始态。

    清理面（曾只删 credentials.json，残留三处）：
    1. 运行中的 bot：断开 WS 长连接（否则旧连接仍在线收消息）；
    2. 进行中的扫码轮询任务（否则扫码成功回调会把旧凭证又写回来）；
    3. config.json 的 feishu.*（扫码成功时会回写；不清则重启后
       launcher 重新注入 env、控制台配置概览仍显示「已配置」）。
    """
    # 1. 停 bot（断长连接）
    bot = _state.get("bot")
    if bot is not None:
        try:
            bot.stop()
        except Exception as e:
            logger.warning("停止机器人异常（忽略）: %s", e)
    _state["bot"] = None
    _state["bot_running"] = False
    _state["bot_error"] = ""
    _state["bot_thread"] = None

    # 2. 取消进行中的扫码轮询，防旧凭证回写
    for _tok, _t in list(_state.get("poll_tasks", {}).items()):
        try:
            _t.cancel()
        except Exception:
            pass
    _state.get("poll_tasks", {}).clear()

    # 3. 删凭证文件
    if CRED_FILE.exists():
        CRED_FILE.unlink()
    open_id_file = DATA_DIR / "configured_open_id.json"
    if open_id_file.exists():
        open_id_file.unlink()

    # 4. 清 config.json 的 feishu.*
    if cfg_mod is not None:
        try:
            base = cfg_mod.load_config()
            feishu = base.setdefault("feishu", {})
            feishu["app_id"] = ""
            feishu["app_secret"] = ""
            cfg_mod.save_config(base)
        except Exception as e:
            logger.warning("清理 config.json 飞书配置失败: %s", e)

    # 5. 清当前进程 env（readiness 立即如实显示未配置）
    os.environ.pop("FEISHU_APP_ID", None)
    os.environ.pop("FEISHU_APP_SECRET", None)

    return {"ok": True, "message": "已清除全部飞书配置，请重新扫码配置"}


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "feishu-agent", "version": "2.0.0"}


# ---- 三级健康模型：liveness / readiness（详见 common/health.py） ----
def _load_health_mod():
    try:
        _root = str(Path(__file__).resolve().parents[2])
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from common import health as _h
        return _h
    except Exception:
        return None


@app.get("/api/health/live")
async def health_live():
    """存活探针：进程在跑即 200，不触碰任何依赖（供看门狗使用）。"""
    return {"status": "alive", "service": "feishu-agent"}


@app.get("/api/health/ready")
async def health_ready():
    """就绪探针：探测飞书凭证、机器人连接、下游服务可达性。

    只回报「是否已配置」，绝不回显任何凭证内容。
    """
    _h = _load_health_mod()
    if _h is None:
        return {"service": "feishu-agent", "status": "unknown",
                "reasons": ["common.health 不可用"]}

    def _chk_feishu_cred():
        # 与运行态取凭证的路径对齐：env → credentials.json（扫码配置走这里，
        # 只写文件不写进程 env）。2026-08-06 实锤：只读 env 会在扫码热配置后
        # 误报「飞书 App 未配置」假降级，实际 bot 长连接正常收发消息。
        app_id = os.environ.get("FEISHU_APP_ID", "")
        secret = os.environ.get("FEISHU_APP_SECRET", "")
        if app_id and secret:
            return True, "已配置"
        try:
            creds = load_credentials(CRED_FILE) or {}
            if creds.get("app_id") and creds.get("app_secret"):
                return True, "已配置"
        except Exception:
            pass
        return False, "飞书 App 未配置，机器人不可用"

    def _chk_bot():
        err = _state.get("bot_error") or ""
        if err:
            return False, str(err)[:120]
        return True, "长连接正常"

    async def _chk_downstream(url: str, label: str):
        if not url:
            return False, f"{label} 地址未配置"
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{url}/api/health")
            return r.status_code == 200, f"{label} HTTP {r.status_code}"

    specs = [
        ("ai_router", lambda: _chk_downstream(AI_ROUTER_URL, "ai-router"), True),
        ("agent_pipeline", lambda: _chk_downstream(PIPELINE_URL, "agent-pipeline"), True),
        ("spider", lambda: _chk_downstream(SPIDER_URL, "spider"), False),
        ("feishu_credential", _chk_feishu_cred, False),
        ("feishu_bot", _chk_bot, False),
    ]
    report = await _h.gather_report("feishu-agent", specs, version="2.0.0")
    return JSONResponse(status_code=200 if report["ready"] else 503,
                        content=report)


# ============ Auth Endpoints ============

class OTPSetupRequest(BaseModel):
    secret: str
    code: str


class OTPLoginRequest(BaseModel):
    code: str


@app.get("/api/auth/status")
async def auth_status():
    """Check auth setup status."""
    setup_done = is_setup_completed()
    return {"setup_completed": setup_done, "needs_auth": setup_done}


@app.post("/api/auth/setup/generate")
async def auth_setup_generate():
    """Generate a new TOTP secret and QR code for setup."""
    secret = generate_totp_secret()
    qrcode_img = generate_totp_qrcode(secret)
    uri = get_totp_uri(secret)
    return {
        "secret": secret,
        "qrcode_img": qrcode_img,
        "uri": uri,
        "message": "请用验证器 App (Google Authenticator / 腾讯云验证器 等) 扫码",
    }


@app.post("/api/auth/setup/verify")
async def auth_setup_verify(body: OTPSetupRequest):
    """Verify the first TOTP code to complete setup."""
    # 防未授权接管：TOTP 已设置后禁止再用自带 secret 覆写管理员密钥
    # （此前该端点在公开前缀内且无守卫，攻击者可自带 secret+code 接管）
    if is_setup_completed():
        raise HTTPException(
            status_code=403,
            detail="TOTP 已设置完成，禁止重复初始化；如需重置请联系管理员")
    result = setup_totp(body.secret, body.code)
    if result["success"]:
        # Auto-login after setup
        auth_result = authenticate(body.code)
        if auth_result["success"]:
            result["token"] = auth_result["token"]
    return result


@app.post("/api/auth/login")
async def auth_login(request: Request, body: OTPLoginRequest):
    """Login with TOTP code (带失败限流防爆破)."""
    identifier = request.client.host if request.client else "unknown"
    if is_login_limited(identifier):
        raise HTTPException(
            status_code=429, detail="登录尝试过于频繁，请 5 分钟后再试")
    result = authenticate(body.code)
    if not result["success"]:
        record_login_failure(identifier)
        raise HTTPException(status_code=401, detail=result["error"])
    reset_login_failure(identifier)
    return result


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    """Logout and invalidate session."""
    token = request.headers.get("X-Auth-Token", "")
    logout(token)
    return {"success": True}


@app.get("/api/auth/check")
async def auth_check(request: Request):
    """Check if current session is valid."""
    token = request.headers.get("X-Auth-Token", "")
    if verify_session(token):
        return {"authenticated": True}
    return {"authenticated": False}


# ============ AI Config Endpoints ============

class AIProviderUpdate(BaseModel):
    provider: str
    api_key: str = ""
    base_url: str = ""
    selected_model: str = ""
    enabled: bool = False


@app.get("/api/ai/config")
async def get_ai_config():
    """Get current AI configuration (API keys masked)."""
    config = load_ai_config()
    # Mask API keys for display
    masked = json.loads(json.dumps(config))
    for key, provider in masked.get("providers", {}).items():
        ak = provider.get("api_key", "")
        if ak and len(ak) > 8:
            provider["api_key_masked"] = ak[:4] + "****" + ak[-4:]
        elif ak:
            provider["api_key_masked"] = "****"
        else:
            provider["api_key_masked"] = ""
        provider["has_key"] = bool(ak)
    return masked


@app.post("/api/ai/config")
async def save_ai_config_endpoint(body: dict):
    """Save AI configuration."""
    config = load_ai_config()
    providers = body.get("providers", {})

    for key, update in providers.items():
        if key in config["providers"]:
            p = config["providers"][key]
            if "api_key" in update:
                p["api_key"] = update["api_key"]
            if "base_url" in update:
                p["base_url"] = update["base_url"]
            if "selected_model" in update:
                p["selected_model"] = update["selected_model"]
            if "enabled" in update:
                p["enabled"] = update["enabled"]
            if "use_proxy" in update:
                p["use_proxy"] = bool(update["use_proxy"])
            if "vision" in update:
                p["vision"] = bool(update["vision"])
            if "vision_model" in update:
                p["vision_model"] = str(update["vision_model"] or "").strip()

    if "task_routing" in body:
        config["task_routing"] = body["task_routing"]

    # 全局代理地址（WebUI 代理设置；空串 = 清除）
    if "proxy_url" in body:
        config["proxy_url"] = str(body.get("proxy_url") or "").strip()

    save_ai_config(config)

    # 持久化回 config.json：launcher 重启时从 config.json 的 ai.* 注入
    # DEEPSEEK/GEMINI/QWEN_API_KEY 与 AI_PROXY_URL；若不回写，重启后
    # ai-router 拿不到 key（显示未配置），桌面控制台「配置概览」也读空。
    _persist_ai_config_to_config_json(config)

    # Sync to ai-router
    sync_result = await sync_to_ai_router(config, AI_ROUTER_URL)

    return {"success": True, "sync": sync_result}


def _persist_ai_config_to_config_json(config: Dict[str, Any]) -> None:
    """把 AI 配置回写到 canonical config.json（ai.* 段），保证重启后仍生效。

    WebUI 保存的是 data/feishu-agent/ai_config.json，但 launcher 重启时
    只从 config.json 的 ai.* 注入环境变量；两者分叉会导致「配置显示未配置」。
    这里把 3 个主 provider 的 key + 全局代理回写，使重启链路闭合。
    """
    if cfg_mod is None:
        return
    try:
        base = cfg_mod.load_config()
        ai = base.setdefault("ai", {})
        ai["deepseek_api_key"] = config["providers"].get("deepseek", {}).get("api_key", "")
        ai["gemini_api_key"] = config["providers"].get("gemini", {}).get("api_key", "")
        ai["qwen_api_key"] = config["providers"].get("qwen", {}).get("api_key", "")
        ai["proxy_url"] = config.get("proxy_url", "")
        cfg_mod.save_config(base)
        logger.info("AI config persisted to config.json")
    except Exception as e:
        logger.warning("Failed to persist AI config to config.json: %s", e)


@app.post("/api/ai/models")
async def fetch_provider_models(body: dict):
    """Fetch available models from a provider."""
    provider = body.get("provider", "")
    api_key = body.get("api_key", "")
    base_url = body.get("base_url", "")

    if not provider:
        raise HTTPException(status_code=400, detail="provider 必填")

    # Use saved config if api_key not provided
    if not api_key:
        config = load_ai_config()
        p = config["providers"].get(provider, {})
        api_key = p.get("api_key", "")
        base_url = base_url or p.get("base_url", "")

    if not api_key:
        raise HTTPException(status_code=400, detail="请先填写 API Key")

    models = await fetch_models(provider, api_key, base_url)
    return {"provider": provider, "models": models, "count": len(models)}


@app.post("/api/ai/test")
async def test_ai_connection(body: dict):
    """Test connection to an AI provider."""
    provider = body.get("provider", "")
    api_key = body.get("api_key", "")
    base_url = body.get("base_url", "")
    model = body.get("model", "")

    if not provider:
        raise HTTPException(status_code=400, detail="provider 必填")

    # Use saved config if not provided
    config = load_ai_config()
    p = config["providers"].get(provider, {})
    api_key = api_key or p.get("api_key", "")
    base_url = base_url or p.get("base_url", "")
    model = model or p.get("selected_model", p.get("default_model", ""))

    result = await test_connection(provider, api_key, base_url, model)
    return result


# ============ Xianyu Login Proxy Endpoints ============

@app.get("/api/xianyu/status")
async def xianyu_login_status():
    """Get Xianyu login state status from spider-service."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{SPIDER_URL}/api/login/status")
            return resp.json()
    except Exception as e:
        return {"logged_in": False, "error": str(e), "state_files": []}


@app.post("/api/xianyu/qrcode/start")
async def xianyu_qrcode_start():
    """Start Xianyu QR code login via spider-service."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{SPIDER_URL}/api/login/qrcode/start")
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Spider service error: {e}")


@app.get("/api/xianyu/qrcode/status")
async def xianyu_qrcode_status(session_id: str = Query(...)):
    """Check Xianyu QR code login status."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{SPIDER_URL}/api/login/qrcode/status",
                params={"session_id": session_id})
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Spider service error: {e}")


@app.post("/api/xianyu/login/state/upload")
async def xianyu_upload_state(request: Request):
    """Upload xianyu_state.json login state file."""
    try:
        body = await request.json()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{SPIDER_URL}/api/login/state/upload",
                json=body)
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Spider service error: {e}")


@app.delete("/api/xianyu/login/state")
async def xianyu_delete_state():
    """Delete Xianyu login state."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(f"{SPIDER_URL}/api/login/state")
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Spider service error: {e}")


# ============ Startup ============

@app.on_event("startup")
async def startup():
    logger.info("Feishu Agent 启动中...")
    _start_bot_in_thread()
    # 重启后把已保存的 ai_config.json 重放给 ai-router：launcher 仅向
    # ai-router 注入 deepseek/gemini/qwen 三个主 provider，其余（openai/
    # zhipu/moonshot）需由 feishu-agent 启动后重放才能恢复。带重试，
    # 因为 ai-router 虽先启动但可能尚未完全就绪。
    asyncio.create_task(_replay_ai_config_on_boot())
    logger.info("Feishu Agent 启动完成")


async def _replay_ai_config_on_boot(max_retry: int = 10) -> None:
    """启动后把本地 ai_config.json 重放给 ai-router（覆盖所有 provider）。"""
    for i in range(max_retry):
        try:
            cfg = load_ai_config()
            if any(p.get("api_key") for p in cfg.get("providers", {}).values()):
                await sync_to_ai_router(cfg, AI_ROUTER_URL)
                logger.info("已重放 AI 配置到 ai-router")
            return
        except Exception as e:
            if i == 0:
                logger.info("启动重放 AI 配置到 ai-router 重试中: %s", e)
            await asyncio.sleep(2)


# ============ System Overview Endpoint ============

async def _fetch_service(url: str, timeout: int = 5) -> Optional[Dict]:
    """Safely fetch JSON from a service endpoint."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            return resp.json()
    except Exception:
        return None


@app.get("/api/system/overview")
async def system_overview():
    """Aggregate status from all V2.0 services."""
    # Fetch all service statuses in parallel
    results = await asyncio.gather(
        _status_payload(),  # internal call — no self-HTTP (auth middleware 401 trap)
        _fetch_service(f"{AI_ROUTER_URL}/api/stats"),
        _fetch_service(f"{AI_ROUTER_URL}/api/models"),
        _fetch_service(f"{PIPELINE_URL}/api/stats"),
        _fetch_service(f"{SPIDER_URL}/api/status"),
        _fetch_service(f"{SPIDER_URL}/api/login/status"),
        _fetch_service(f"{AI_ROUTER_URL}/api/stats/tokens"),
        return_exceptions=True,
    )

    feishu_status = results[0] if not isinstance(results[0], Exception) else None
    ai_stats = results[1] if not isinstance(results[1], Exception) else None
    ai_models = results[2] if not isinstance(results[2], Exception) else None
    pipeline_stats = results[3] if not isinstance(results[3], Exception) else None
    spider_status = results[4] if not isinstance(results[4], Exception) else None
    xianyu_status = results[5] if not isinstance(results[5], Exception) else None
    token_stats = results[6] if not isinstance(results[6], Exception) else None

    # 基础设施探测 —— 尊重 config：未启用的后端为「可选未启用(disabled)」，属预期降级，
    # 不算异常（桌面 exe 不捆绑 PG/Redis/Qdrant，enabled=false 时不应弹红色告警）。
    # 同时汇总 system_status 与 degraded_features，让前端区分「真故障」与「可选组件未启用」。
    _BACKEND_FEATURES = {
        "postgres": "持久化监控任务 / AI 调用统计",
        "redis": "会话保持 / 登录限流 / 结果缓存",
        "qdrant": "RAG 知识库语义检索",
    }
    infra = {}
    _bk = {}
    if cfg_mod is not None:
        try:
            _bk = (cfg_mod.load_config() or {}).get("backends", {})
        except Exception:
            _bk = {}
    _qd = (_bk.get("qdrant") or {})
    _pg = (_bk.get("postgres") or {})
    _rd = (_bk.get("redis") or {})
    # P1 单用户化：向量库(RAG)已由外部 Qdrant 改为进程内 Chroma（嵌入 ai-router，
    # 服务启动即可用）。不再探测已不存在的 Qdrant HTTP 端口，按 enabled 直接判
    # running/disabled，与 postgres/redis 一致，避免命中死地址误报 error。
    infra["qdrant"] = "running" if _qd.get("enabled", False) else "disabled"
    infra["postgres"] = "running" if _pg.get("enabled", False) else "disabled"
    infra["redis"] = "running" if _rd.get("enabled", False) else "disabled"

    # 系统整体状态：只关心「必需服务」是否全绿 + 是否有真实后端故障(error)；
    # 可选后端未启用(disabled)属降级而非故障。
    _required_down = [
        k for k, s in {
            "feishu_agent": feishu_status, "ai_router": ai_stats,
            "agent_pipeline": pipeline_stats, "spider_service": spider_status,
        }.items() if not s
    ]
    _backend_error = [n for n, st in infra.items() if st == "error"]
    degraded_features = [
        _BACKEND_FEATURES[n] for n in ("postgres", "redis", "qdrant")
        if infra.get(n) == "disabled" and _BACKEND_FEATURES.get(n)
    ]
    if _required_down or _backend_error:
        system_status = "error"
    elif degraded_features:
        system_status = "degraded"
    else:
        system_status = "healthy"

    # AI config summary
    ai_config = load_ai_config()
    enabled_providers = [
        {"key": k, "name": v.get("name", k), "model": v.get("selected_model", "")}
        for k, v in ai_config.get("providers", {}).items()
        if v.get("enabled") and v.get("api_key")
    ]

    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "services": {
            "feishu_agent": {
                "status": "running" if feishu_status else "error",
                "bot_running": feishu_status.get("bot_running", False) if feishu_status else False,
                "uptime": feishu_status.get("uptime_seconds", 0) if feishu_status else 0,
                "active_tasks": feishu_status.get("active_tasks", 0) if feishu_status else 0,
            },
            "ai_router": {
                "status": "running" if ai_stats else "error",
                "total_calls": ai_stats.get("total_calls", 0) if ai_stats else 0,
                "calls_by_model": ai_stats.get("calls_by_model", {}) if ai_stats else {},
                "enabled_providers": enabled_providers,
                "token_stats": token_stats or {},
            },
            "agent_pipeline": {
                "status": "running" if pipeline_stats else "error",
                "total_analyzed": pipeline_stats.get("total_analyzed", 0) if pipeline_stats else 0,
                "recommended": pipeline_stats.get("recommended", 0) if pipeline_stats else 0,
                "filtered": pipeline_stats.get("filtered", 0) if pipeline_stats else 0,
                "total_goods": pipeline_stats.get("total_goods", 0) if pipeline_stats else 0,
                "errors": pipeline_stats.get("errors", 0) if pipeline_stats else 0,
                "monitors": pipeline_stats.get("monitors", {}) if pipeline_stats else {},
                "last_search": pipeline_stats.get("last_search", {}) if pipeline_stats else {},
            },
            "spider_service": {
                "status": "running" if spider_status else "error",
                "active_tasks": spider_status.get("active_tasks", 0) if spider_status else 0,
                "results_count": spider_status.get("results_count", 0) if spider_status else 0,
                "uptime": spider_status.get("uptime_seconds", 0) if spider_status else 0,
                "busy": bool(spider_status.get("busy", False)) if spider_status else False,
            },
        },
        "infrastructure": infra,
        "system_status": system_status,
        "degraded_features": degraded_features,
        "xianyu": {
            "logged_in": xianyu_status.get("logged_in", False) if xianyu_status else False,
            "state_files": len(xianyu_status.get("state_files", [])) if xianyu_status else 0,
        },
        "feishu": {
            "configured": feishu_status.get("configured", False) if feishu_status else False,
            "bot_running": feishu_status.get("bot_running", False) if feishu_status else False,
            "recent_messages": len(feishu_status.get("last_messages", [])) if feishu_status else 0,
        },
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8901))
    import logging
    class _HealthFilter(logging.Filter):
        def filter(self, record):
            return "/api/health" not in record.getMessage()
    logging.getLogger("uvicorn.access").addFilter(_HealthFilter())
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
