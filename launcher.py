# -*- coding: utf-8 -*-
"""
GoofishMasterDesktop 启动编排器
- 按依赖顺序子进程拉起 4 个服务（端口 8911-8914，绑定 127.0.0.1）
- 健康检查（/api/health），崩溃自动重启
- 优雅关停（SIGTERM + 超时强杀）
- CLI: python launcher.py start | stop | restart | status

与原 Docker 项目零关联：独立目录、独立端口、独立数据目录。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# 冻结感知：PyInstaller onedir 下资源（services/config/...）位于 sys._MEIPASS
# （即 _internal 目录），不能用 __file__ 或 exe 父目录。
if getattr(sys, "frozen", False):
    _mp = getattr(sys, "_MEIPASS", None)
    ROOT = Path(_mp).resolve() if _mp else Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from common import config as cfg_mod  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [launcher] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

# 日志脱敏：launcher 会打印子进程的启动环境摘要，里面带 AI Key。
try:
    from common.logfilter import install as _install_logfilter  # noqa: E402
    _install_logfilter()
except Exception:
    pass

log = logging.getLogger("launcher")

CFG = cfg_mod.load_config()

# 服务定义 + 依赖声明。
# `depends` 表示「该服务运行期会调用谁」，启动顺序由拓扑排序求出，
# 保证被调用方先就绪。此前是硬编码列表且 feishu-agent 排在 agent-pipeline
# 之前，而 feishu-agent 启动后即可能转发指令给 pipeline（:8913 尚未监听）
# → connection refused。改为声明依赖后顺序由代码保证，新增服务不易排错。
_SERVICE_DEFS = [
    {"name": "ai-router",      "port_key": "ai_router",      "dir": "services/ai-router",
     "depends": []},
    {"name": "agent-pipeline", "port_key": "agent_pipeline", "dir": "services/agent-pipeline",
     "depends": ["ai-router"]},
    {"name": "spider-service", "port_key": "spider",         "dir": "services/spider-service",
     "depends": ["ai-router", "agent-pipeline"]},
    {"name": "feishu-agent",   "port_key": "feishu_agent",   "dir": "services/feishu-agent",
     "depends": ["ai-router", "agent-pipeline", "spider-service"]},
]


def _topo_sort(defs: list[dict]) -> list[dict]:
    """按 depends 拓扑排序（Kahn）。存在环时退回声明顺序并告警，绝不死锁。"""
    by_name = {d["name"]: d for d in defs}
    indeg = {d["name"]: 0 for d in defs}
    children: dict[str, list[str]] = {d["name"]: [] for d in defs}
    for d in defs:
        for dep in d.get("depends", []):
            if dep not in by_name:
                log.warning("服务 %s 声明了未知依赖 %s，已忽略", d["name"], dep)
                continue
            indeg[d["name"]] += 1
            children[dep].append(d["name"])
    # 用声明顺序作为同层内的稳定次序（结果可复现）
    order: list[dict] = []
    ready = [d["name"] for d in defs if indeg[d["name"]] == 0]
    while ready:
        cur = ready.pop(0)
        order.append(by_name[cur])
        for nxt in children[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
    if len(order) != len(defs):
        missing = [d["name"] for d in defs if d not in order]
        log.error("服务依赖存在循环（%s），回退到声明顺序启动", ", ".join(missing))
        return list(defs)
    return order


SERVICES = _topo_sort(_SERVICE_DEFS)

PROCS: dict[str, subprocess.Popen] = {}
_RUNNING = True

# 看门狗重启熔断：服务反复崩溃时，无限重启只会空耗资源并刷爆日志。
# 统计窗口内自动重启次数超限 → 熔断停拉、标记状态，等待人工排查后手动重启
# （手动 restart_service / start_all 会清除熔断标记）。
_BROKEN: set[str] = set()
_RESTART_HISTORY: dict[str, list[float]] = {}
_RESTART_WINDOW_SEC = 600   # 统计窗口：10 分钟
_RESTART_MAX = 5            # 窗口内允许的最大自动重启次数


def _record_restart(name: str) -> bool:
    """记录一次自动重启并检查熔断。返回 False 表示已熔断，不应再重启。"""
    now = time.time()
    hist = [t for t in _RESTART_HISTORY.get(name, [])
            if now - t < _RESTART_WINDOW_SEC]
    hist.append(now)
    _RESTART_HISTORY[name] = hist
    if len(hist) > _RESTART_MAX:
        _BROKEN.add(name)
        return False
    return True


# ---------------------------------------------------------------------------
# 桌面运行前置依赖检测（WebView2 Runtime / 打包的 Playwright Chromium）
# ---------------------------------------------------------------------------
def _webview2_installed() -> bool:
    """检测 Microsoft Edge WebView2 Runtime 是否已安装（固定注册表 GUID）。"""
    try:
        import winreg
    except Exception:
        return True  # 非 Windows / 无法检测 → 假定可用（避免误报）
    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A08C11}"
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for base in (r"SOFTWARE\Microsoft\EdgeUpdate\Clients",
                     r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"):
            try:
                with winreg.OpenKey(root, base + "\\" + guid):
                    return True
            except OSError:
                continue
    return False


def _bundled_webview2_runtime() -> bool:
    """检测是否随包携带固定版本 WebView2 运行时（webview2_runtime/msedgewebview2.exe）。"""
    try:
        exe_dir = Path(sys.executable).resolve().parent
        return (exe_dir / "webview2_runtime" / "msedgewebview2.exe").exists()
    except Exception:
        return False


def _bundled_browsers_dir() -> Path:
    """打包产物中随附的 Playwright Chromium 目录（安装时落盘到 app 同级）。"""
    env_dir = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env_dir:
        return Path(env_dir).resolve()
    if getattr(sys, "frozen", False):
        # 冻结模式：playwright-browsers 随 exe 一起落在 app 目录下（与 _internal 同级）
        return Path(sys.executable).resolve().parent / "playwright-browsers"
    return (ROOT / "playwright-browsers").resolve()


def _chromium_installed() -> bool:
    """检测打包的 Playwright Chromium 是否存在于随附浏览器目录。"""
    bdir = _bundled_browsers_dir()
    if not bdir.exists():
        return False
    # Playwright 浏览器目录形如 chromium-<revision> / chromium_headless_shell-<revision>
    found = any(p.name.startswith("chromium") for p in bdir.iterdir() if p.is_dir())
    return found


def check_prerequisites() -> dict:
    """返回前置依赖检测结果（供桌面控制台 / preflight 命令使用）。"""
    webview2 = _webview2_installed() or _bundled_webview2_runtime()
    chromium = _chromium_installed()
    return {
        "webview2_installed": webview2,
        "webview2_message": (
            "已就绪（系统 Runtime 或随包固定版本）" if webview2 else
            "未检测到 WebView2 Runtime 且未随包携带固定版本，桌面窗口无法打开"
        ),
        "chromium_installed": chromium,
        "chromium_message": (
            "已随附 Chromium" if chromium else
            "未找到随附的 Chromium，采集服务将使用系统 Chrome/Edge（需另行安装）"
        ),
        "all_ok": webview2 and chromium,
    }


def _app_dir() -> Path:
    """配置/数据落盘根目录（exe 同级；开发模式为项目根）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return ROOT


def _data_dir(sub: str) -> str:
    d = cfg_mod.APP_DIR / "data" / sub
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


# 单个环境变量的安全上限（Windows 硬上限 32767，留出余量）
_ENV_VALUE_LIMIT = 32000

# 这些变量是「以 os.pathsep 分隔的路径列表」，超长时必须按整条目裁剪。
# 从中间硬切会产生半截路径（如 PATH 末尾变成 "C:\Program Files\Pyt"），
# 子进程据此找不到 python / playwright / chrome，且报错完全不指向真因。
_PATH_LIKE_VARS = {"PATH", "PYTHONPATH", "PSMODULEPATH", "LIB", "INCLUDE",
                   "CLASSPATH", "LD_LIBRARY_PATH"}


def _truncate_path_like(name: str, value: str) -> str:
    """按 os.pathsep 逐条保留，丢弃放不下的**完整**条目（不切碎任何一条）。"""
    parts = [p for p in value.split(os.pathsep) if p]
    kept: list[str] = []
    total = 0
    dropped = 0
    for p in parts:
        add = len(p) + (1 if kept else 0)
        if total + add > _ENV_VALUE_LIMIT:
            dropped += 1
            continue
        kept.append(p)
        total += add
    if dropped:
        log.warning("环境变量 %s 超长，已丢弃末尾 %d 个完整路径条目（保留 %d 条，"
                    "未产生半截路径）", name, dropped, len(kept))
    return os.pathsep.join(kept)


def _safe_env() -> dict:
    """复制当前环境变量，保证单个值不超过 Windows 上限。

    避免 Windows 单环境变量 32767 上限：当前会话 PATH 等被撑大时，
    直接 `dict(os.environ)` 透传给子进程会抛
    'environment variable is longer than 32767 characters' 导致子进程启动即崩。

    关键：**路径列表型变量按条目裁剪，绝不从中间截断**（见 _PATH_LIKE_VARS）。
    """
    env: dict = {}
    for k, v in os.environ.items():
        if len(v) > _ENV_VALUE_LIMIT:
            if k.upper() in _PATH_LIKE_VARS:
                v = _truncate_path_like(k, v)
            else:
                log.warning("环境变量 %s 超长（%d 字符），已截断至 %d",
                            k, len(v), _ENV_VALUE_LIMIT)
                v = v[:_ENV_VALUE_LIMIT]
        env[k] = v
    return env


# 服务名 → data 子目录（与 _data_dir 注入保持一致）
_DATA_SUB = {
    "feishu-agent": "feishu-agent",
    "ai-router": "ai-router",
    "agent-pipeline": "agent-pipeline",
    "spider-service": "spider",
}


def build_env(name: str) -> dict:
    """构造单个服务的环境变量（在继承系统环境基础上覆盖）。"""
    e = _safe_env()
    e["PYTHONUNBUFFERED"] = "1"
    e["GOOFISH_SECRET_KEY"] = CFG.get("secret_key", "")
    # 指向打包随附的 Chromium（安装时落盘到 app 同级 playwright-browsers）
    e["PLAYWRIGHT_BROWSERS_PATH"] = str(_bundled_browsers_dir())

    # 存储引擎：桌面版恒为 sqlite（进程内 aiosqlite），显式声明避免歧义。
    # SQLITE_DISABLED 是各服务 db.py 唯一认可的降级开关，与 POSTGRES_ENABLED 解耦。
    engine = cfg_mod.storage_engine(CFG)
    e["STORAGE_ENGINE"] = engine
    e["SQLITE_DISABLED"] = "false"

    # 本地后端连接串：仅在对应后端 enabled 时注入真实地址；
    # 未启用则注入空串 + 显式 *_ENABLED=false，让各服务跳过连接尝试、
    # 干净降级（不再向死地址重试/告警），overview 也据此判 degraded。
    # DATABASE_URL 现在如实反映 sqlite 落盘位置（不再伪造 postgresql:// 串）。
    _sub = _DATA_SUB.get(name)
    bu = cfg_mod.backend_urls(CFG, _data_dir(_sub) if _sub else None)
    be = CFG.get("backends", {}) or {}
    def _on(name: str, url: str) -> None:
        if (be.get(name) or {}).get("enabled", False):
            e[f"{name.upper()}_URL"] = url
            e[f"{name.upper()}_ENABLED"] = "true"
        else:
            e[f"{name.upper()}_URL"] = ""
            e[f"{name.upper()}_ENABLED"] = "false"
    _on("redis", bu["REDIS_URL"])
    _on("postgres", bu["DATABASE_URL"])
    _on("qdrant", bu["QDRANT_URL"])
    # 注意：上面的 _on 注入的是 <NAME>_URL（即 POSTGRES_URL），历史上文档写作
    # "注入 DATABASE_URL" 但实际从未注入过——各服务的 db.py 也从不读它，
    # 而是由 DATA_DIR 推导 SQLite 路径。这里补一个语义正确的 DATABASE_URL，
    # 让「配置声明」与「实际存储」对得上，排障时不再被 postgresql:// 误导。
    e["DATABASE_URL"] = bu["DATABASE_URL"]

    urls = cfg_mod.service_urls(CFG)
    p = CFG["ports"]

    if name == "feishu-agent":
        e["PORT"] = str(p["feishu_agent"])
        e["DATA_DIR"] = _data_dir("feishu-agent")
        e["AI_ROUTER_URL"] = urls["ai_router"]
        e["PIPELINE_URL"] = urls["agent_pipeline"]
        e["SPIDER_URL"] = urls["spider"]
        e["FEISHU_APP_ID"] = CFG["feishu"].get("app_id", "")
        e["FEISHU_APP_SECRET"] = CFG["feishu"].get("app_secret", "")

    elif name == "ai-router":
        e["PORT"] = str(p["ai_router"])
        e["DATA_DIR"] = _data_dir("ai-router")
        e["KNOWLEDGE_DIR"] = str(ROOT / "knowledge-base")
        ai = CFG.get("ai", {})
        e["DEEPSEEK_API_KEY"] = ai.get("deepseek_api_key", "")
        e["GEMINI_API_KEY"] = ai.get("gemini_api_key", "")
        e["QWEN_API_KEY"] = ai.get("qwen_api_key", "")
        e["AI_PROXY_URL"] = ai.get("proxy_url", "")
        e["AI_SLOT_TIMEOUT"] = "60"

    elif name == "agent-pipeline":
        e["PORT"] = str(p["agent_pipeline"])
        e["DATA_DIR"] = _data_dir("agent-pipeline")
        e["AI_ROUTER_URL"] = urls["ai_router"]
        e["FEISHU_AGENT_URL"] = urls["feishu_agent"]
        e["SPIDER_URL"] = urls["spider"]
        e["AI_CONCURRENCY"] = "5"

    elif name == "spider-service":
        e["PORT"] = str(p["spider"])
        e["DATA_DIR"] = _data_dir("spider")
        e["ACCOUNT_STATE_DIR"] = _data_dir("spider-state")
        e["AI_ROUTER_URL"] = urls["ai_router"]
        e["PIPELINE_URL"] = urls["agent_pipeline"]
        e["FEISHU_AGENT_URL"] = urls["feishu_agent"]
        e["RUN_HEADLESS"] = "true"
        e["RUNNING_IN_DOCKER"] = "false"
        # 商品图下载的代理兜底：本机代理客户端（增强/TUN 模式）会间歇性
        # 劫持/打断系统 DNS，导致 img.alicdn.com getaddrinfo failed
        # （2026-08-06 两次实锤）。spider.image_download_proxy 优先，
        # 缺省复用 ai.proxy_url；为空则直连失败即报错（维持原行为）。
        spider_cfg = CFG.get("spider") or {}
        e["IMAGE_DOWNLOAD_PROXY"] = (
            spider_cfg.get("image_download_proxy", "")
            or (CFG.get("ai") or {}).get("proxy_url", "")
        )
        # 桌面端优先使用随附的 Playwright Chromium（而非系统 Chrome/Edge），
        # 保证离线环境也能采集；scraper 读取该变量决定是否走 bundled 通道。
        if _chromium_installed():
            e["GOOFISH_USE_BUNDLED_CHROMIUM"] = "true"

    return e


def health_ok(port: int) -> bool:
    """liveness：进程是否在监听并响应。看门狗据此决定要不要重启。

    刻意保持只看 HTTP 200——依赖（DB/AI Key/Chromium）的问题应由 readiness
    暴露，绝不能触发重启，否则依赖抖动会引发无意义的重启风暴。
    """
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def readiness(port: int, timeout: float = 5.0) -> dict:
    """readiness：拉取单个服务的依赖体检报告（/api/health/ready）。

    老版本服务没有该端点（404）时退回 liveness 结果，标记 status=unknown，
    保证新 launcher 配旧服务不会误报故障。
    """
    url = f"http://127.0.0.1:{port}/api/health/ready"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 503 = degraded/error，响应体里仍是完整报告
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"status": "error", "ready": False,
                    "reasons": [f"HTTP {e.code}"]}
    except Exception as e:
        alive = health_ok(port)
        return {"status": "unknown" if alive else "error",
                "ready": alive,
                "reasons": [] if alive else [f"不可达: {e}"]}


def readiness_all() -> dict:
    """聚合 4 个服务的就绪报告，给出整体状态（供桌面控制台展示）。"""
    services: dict = {}
    worst = "healthy"
    rank = {"healthy": 0, "unknown": 1, "degraded": 2, "error": 3}
    for svc in SERVICES:
        port = CFG["ports"][svc["port_key"]]
        rep = readiness(port)
        services[svc["name"]] = rep
        st = rep.get("status", "unknown")
        if rank.get(st, 1) > rank.get(worst, 0):
            worst = st
    reasons = []
    for nm, rep in services.items():
        for r in rep.get("reasons", []) or []:
            reasons.append(f"{nm} / {r}")
    return {"status": worst, "services": services, "reasons": reasons}


def start_service(svc: dict) -> subprocess.Popen:
    port = CFG["ports"][svc["port_key"]]
    env = build_env(svc["name"])
    logdir = cfg_mod.APP_DIR / "data" / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = open(logdir / f"{svc['name']}.log", "a", encoding="utf-8")

    if getattr(sys, "frozen", False):
        # 冻结模式：复用同一个 exe，以 --service 模式拉起对应服务
        # （避免把每份依赖打包 4 次；子进程仍是独立进程，互不影响）
        cmd = [sys.executable, "--service", svc["name"]]
        cwd = str(ROOT)
    else:
        # 开发模式：直接 python main.py（cwd 指向服务目录）
        cmd = [sys.executable, "-u", "main.py"]
        cwd = str(ROOT / svc["dir"])

    log.info("启动 %s (port=%s, cmd=%s, log=%s)", svc["name"], port, cmd, logfile.name)
    proc = subprocess.Popen(
        cmd,
        cwd=cwd, env=env,
        stdout=logfile, stderr=subprocess.STDOUT,
    )
    # 进程树保护：加入 Windows Job Object（KILL_ON_JOB_CLOSE）。主程序被强杀/
    # 崩溃时，内核级回收该服务及其拉起的 Chromium / Playwright 孙进程，
    # 不再残留孤儿进程。非 Windows 或初始化失败时静默降级（不阻断启动）。
    try:
        from common import jobobject
        if jobobject.assign(proc, svc["name"]):
            log.debug("%s 已加入进程树保护 Job", svc["name"])
    except Exception as e:
        log.warning("%s 进程树保护挂载失败（不影响启动）：%s", svc["name"], e)
    return proc


def run_service_mode(name: str) -> None:
    """冻结模式入口：以独立进程运行单个服务（由 launcher 经 --service 拉起）。"""
    svc = next((s for s in SERVICES if s["name"] == name), None)
    if svc is None:
        log.error("未知服务：%s", name)
        sys.exit(2)

    env = build_env(name)
    os.environ.update(env)

    svc_dir = ROOT / "services" / name
    os.chdir(str(svc_dir))
    sys.path.insert(0, str(svc_dir))

    import uvicorn
    import main as service_main  # 该服务的 FastAPI app 在模块级定义
    port = int(os.environ.get("PORT", CFG["ports"][svc["port_key"]]))
    log.info("运行服务 %s (port=%s, cwd=%s)", name, port, svc_dir)
    # 仅绑定 127.0.0.1（桌面本地服务，无需对外暴露）
    uvicorn.run(service_main.app, host="127.0.0.1", port=port, log_level="info")


def wait_health(svc: dict, timeout: float = 40.0) -> bool:
    port = CFG["ports"][svc["port_key"]]
    deadline = time.time() + timeout
    while time.time() < deadline:
        if health_ok(port):
            log.info("%s 健康就绪 ✓", svc["name"])
            return True
        time.sleep(1.0)
    log.warning("%s 健康检查超时（可能缺少后端/浏览器，属预期降级）", svc["name"])
    return False


def start_all() -> None:
    global _RUNNING
    _RUNNING = True
    _BROKEN.clear()          # 显式启动视为人工介入，重置熔断状态
    _RESTART_HISTORY.clear()
    for svc in SERVICES:
        if not _RUNNING:
            break
        proc = start_service(svc)
        PROCS[svc["name"]] = proc
        wait_health(svc)
    log.info("全部服务已尝试拉起。状态：")
    status()


def _graceful_http_shutdown(name: str, timeout: float = 2.0) -> bool:
    """先请服务自己优雅退出（FastAPI 内部端点做 WAL checkpoint 等清理）。

    背景：windowed 子进程无控制台，CTRL_BREAK_EVENT 投递不到；
    proc.terminate() 在 Windows 是 TerminateProcess 硬杀，shutdown 事件
    永远不触发（2026-08-06 实测 stop 后 WAL 原样残留实锤）。
    改为走服务自带的 POST /api/internal/shutdown（X-Internal-Token 鉴权）。
    """
    svc = next((s for s in SERVICES if s["name"] == name), None)
    token = os.environ.get("GOOFISH_SECRET_KEY", "") or CFG.get("secret_key", "")
    if svc is None or not token:
        return False
    port = CFG["ports"][svc["port_key"]]
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/internal/shutdown",
            method="POST",
            headers={"X-Internal-Token": token},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _terminate(proc: subprocess.Popen, name: str, timeout: float = 8.0) -> None:
    if proc.poll() is not None:
        return
    # 1. 优雅通道：服务自清理（WAL checkpoint）后自行退出
    if _graceful_http_shutdown(name):
        try:
            proc.wait(timeout=timeout + 4)
            return
        except Exception:
            log.warning("%s 接受优雅关停但未及时退出，降级 terminate", name)
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=timeout)
    except Exception:
        log.warning("%s 未响应 terminate，强制杀死", name)
        try:
            proc.kill()
        except Exception:
            pass


def stop_all() -> None:
    global _RUNNING
    _RUNNING = False
    log.info("正在关停所有服务…")
    for name, proc in list(PROCS.items()):
        _terminate(proc, name)
    PROCS.clear()
    log.info("已关停。")


# ---------- 跨进程控制（CLI stop/restart 触达运行中的编排器） ----------
# 2026-08-06 实锤：旧实现 CLI `stop` 只调本进程的 stop_all()，而新 CLI 进程
# 的 PROCS 恒为空 → 对运行中的实例（尤其 GUI 模式）完全无效却打印「已关停」，
# 升级覆盖安装前只能靠强杀。改为标志文件 + pid 文件机制：
#   CLI 写 launcher.stop → 编排器看门狗消费 → stop_all() + 退出进程。

def _ctrl_dir() -> Path:
    d = cfg_mod.APP_DIR / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stop_flag() -> Path:
    return _ctrl_dir() / "launcher.stop"


def _pid_file() -> Path:
    return _ctrl_dir() / "launcher.pid"


def _orchestrator_pid() -> Optional[int]:
    """读取运行中编排器的 pid（不存在/已死返回 None）。"""
    try:
        raw = _pid_file().read_text(encoding="utf-8").strip()
        pid = int(raw)
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return pid
    except Exception:
        pass
    return None


def _write_pid_file() -> None:
    try:
        _pid_file().write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass


def _remove_pid_file() -> None:
    try:
        _pid_file().unlink(missing_ok=True)
    except Exception:
        pass


def _consume_stop_flag() -> bool:
    """看门狗每次循环调用：有停止请求则全停并返回 True。"""
    try:
        if _stop_flag().exists():
            _stop_flag().unlink(missing_ok=True)
            log.info("收到 CLI 停止请求，正在关停…")
            stop_all()
            return True
    except Exception:
        pass
    return False


def cli_stop() -> None:
    """CLI 停止：给运行中的编排器发停止请求；本进程持有服务时直接停。"""
    if PROCS:
        stop_all()
        return
    pid = _orchestrator_pid()
    if pid is None:
        print("没有正在运行的实例。")
        return
    try:
        _stop_flag().write_text(str(time.time()), encoding="utf-8")
    except Exception as e:
        print(f"写入停止请求失败: {e}")
        return
    # 等编排器消费标志并退出（看门狗 3s 一轮 + 关停耗时）
    for _ in range(30):
        time.sleep(0.5)
        if not _stop_flag().exists() and _orchestrator_pid() is None:
            print("已关停。")
            return
    print("停止请求已发送，但实例退出超时——请检查 data/logs/launcher.log")


def stop_service(name: str) -> None:
    """关停单个服务（保留其余）。"""
    proc = PROCS.get(name)
    if proc is None:
        return
    _terminate(proc, name)
    PROCS.pop(name, None)
    log.info("%s 已关停。", name)


def restart_service(name: str) -> None:
    """重启单个服务：先停后起并等待健康。手动重启会清除该服务的熔断标记。"""
    _BROKEN.discard(name)
    _RESTART_HISTORY.pop(name, None)
    stop_service(name)
    svc = next((s for s in SERVICES if s["name"] == name), None)
    if svc is None:
        log.error("未知服务：%s", name)
        return
    PROCS[name] = start_service(svc)
    wait_health(svc)


def status() -> None:
    for svc in SERVICES:
        name = svc["name"]
        port = CFG["ports"][svc["port_key"]]
        if name in _BROKEN:
            print(f"  {name:16s}  ⛔ 已熔断（{_RESTART_WINDOW_SEC // 60} 分钟内重启超 {_RESTART_MAX} 次），请查日志后手动重启")
            continue
        proc = PROCS.get(name)
        if proc and proc.poll() is None:
            ok = health_ok(port)
            print(f"  {name:16s} pid={proc.pid}  port={port}  health={'✓' if ok else '…(未就绪/降级)'}")
        else:
            print(f"  {name:16s}  stopped")


def _watchdog() -> None:
    """崩溃重启守护（仅重启意外退出的服务；窗口期内重启超限则熔断停拉）。"""
    while _RUNNING:
        # CLI stop/restart 的跨进程停止请求：全停后退出编排器进程
        if _consume_stop_flag():
            _remove_pid_file()
            os._exit(0)
        for svc in SERVICES:
            name = svc["name"]
            if name in _BROKEN:
                continue  # 已熔断：等待人工排查后手动重启
            proc = PROCS.get(name)
            if proc and proc.poll() is not None and _RUNNING:
                if not _record_restart(name):
                    log.error("%s 在 %d 分钟内重启超过 %d 次，已熔断停拉。"
                              "请查看 data/logs/%s.log 排查后手动重启该服务。",
                              name, _RESTART_WINDOW_SEC // 60, _RESTART_MAX, name)
                    continue
                log.warning("%s 意外退出(code=%s)，重启中…", name, proc.returncode)
                new = start_service(svc)
                PROCS[name] = new
                wait_health(svc)
        time.sleep(3.0)


def _handle_signal(signum, frame):
    log.info("收到信号 %s，准备退出", signum)
    stop_all()
    _remove_pid_file()
    sys.exit(0)


def main_start_no_block() -> None:
    """非阻塞启动：起后台线程拉起全部服务 + 看门狗，立即返回（供桌面壳调用）。"""
    global _RUNNING
    _RUNNING = True
    _write_pid_file()
    import threading
    threading.Thread(target=start_all, daemon=True).start()
    threading.Thread(target=_watchdog, daemon=True).start()


def main():
    parser = argparse.ArgumentParser(description="GoofishMasterDesktop 启动器")
    parser.add_argument("action", choices=["start", "stop", "restart", "status", "desktop", "preflight"],
                        nargs="?", default="desktop" if getattr(sys, "frozen", False) else "start")
    parser.add_argument("--service", default=None,
                        help="冻结模式下以独立进程运行指定服务（内部使用）")
    args = parser.parse_args()

    # 冻结模式：子进程运行单个服务
    if args.service:
        run_service_mode(args.service)
        return

    # 前置依赖检测（JSON 输出，供控制台/脚本读取）
    if args.action == "preflight":
        print(json.dumps(check_prerequisites(), ensure_ascii=False))
        return

    # 冻结模式（无控制台）：把 stdout/stderr 重定向到日志文件，避免丢失现场
    if getattr(sys, "frozen", False):
        try:
            logdir = cfg_mod.APP_DIR / "data" / "logs"
            logdir.mkdir(parents=True, exist_ok=True)
            f = open(logdir / "launcher.log", "a", encoding="utf-8", buffering=1)
            sys.stdout = f
            sys.stderr = f
            # windowed 模式下 sys.stderr 初始为 None，logging 的 StreamHandler.stream
            # 会是 None，导致 log.xxx 抛 "NoneType has no attribute 'write'"。
            # 强制把仍为 None 的 handler 重定向到日志文件。
            for _h in logging.root.handlers:
                if getattr(_h, "stream", None) is None:
                    _h.stream = f
        except Exception:
            pass

    # 桌面 GUI 模式：打开控制台窗口，并自动拉起后端（退出时优雅关停）
    if args.action == "desktop":
        try:
            import desktop.app
            desktop.app.run()
        except Exception as e:
            import traceback as _tb
            log.error("桌面模式异常:\n%s", _tb.format_exc())
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None,
                    f"桌面模式启动失败：\n{e}\n\n详见 data/logs/desktop-crash.log",
                    "GoofishMasterDesktop", 0x10,
                )
            except Exception:
                pass
        return

    if args.action == "status":
        status()
        return
    if args.action == "stop":
        cli_stop()
        return
    if args.action == "restart":
        cli_stop()
        time.sleep(1)

    # start / restart
    _write_pid_file()
    # 清掉可能残留的停止标志（否则看门狗第一轮就会自杀）
    try:
        _stop_flag().unlink(missing_ok=True)
    except Exception:
        pass
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    try:
        start_all()
        if os.name == "nt":
            # Windows 下 SIGINT 不一定触发；用主线程阻塞 + 看门狗
            import threading
            t = threading.Thread(target=_watchdog, daemon=True)
            t.start()
            while _RUNNING:
                time.sleep(1.0)
        else:
            _watchdog()
    except KeyboardInterrupt:
        stop_all()


if __name__ == "__main__":
    main()
