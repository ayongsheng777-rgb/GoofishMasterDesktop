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
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

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
log = logging.getLogger("launcher")

CFG = cfg_mod.load_config()

SERVICES = [
    {"name": "ai-router",      "port_key": "ai_router",      "dir": "services/ai-router"},
    {"name": "feishu-agent",   "port_key": "feishu_agent",   "dir": "services/feishu-agent"},
    {"name": "agent-pipeline", "port_key": "agent_pipeline", "dir": "services/agent-pipeline"},
    {"name": "spider-service", "port_key": "spider",         "dir": "services/spider-service"},
]

PROCS: dict[str, subprocess.Popen] = {}
_RUNNING = True


def _data_dir(sub: str) -> str:
    d = cfg_mod.APP_DIR / "data" / sub
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _safe_env() -> dict:
    """复制当前环境变量，将单个值截断到 32000 字符以内。

    避免 Windows 单环境变量 32767 上限：当前会话 PATH 等被撑大时，
    直接 `dict(os.environ)` 透传给子进程会抛
    'environment variable is longer than 32767 characters' 导致子进程启动即崩。
    """
    env: dict = {}
    for k, v in os.environ.items():
        if len(v) > 32000:
            v = v[:32000]
        env[k] = v
    return env


def build_env(name: str) -> dict:
    """构造单个服务的环境变量（在继承系统环境基础上覆盖）。"""
    e = _safe_env()
    e["PYTHONUNBUFFERED"] = "1"
    e["GOOFISH_SECRET_KEY"] = CFG.get("secret_key", "")
    # 本地后端连接串：仅在对应后端 enabled 时注入真实地址；
    # 未启用则注入空串 + 显式 *_ENABLED=false，让各服务跳过连接尝试、
    # 干净降级（不再向死地址重试/告警），overview 也据此判 degraded。
    bu = cfg_mod.backend_urls(CFG)
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

    return e


def health_ok(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


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
    for svc in SERVICES:
        if not _RUNNING:
            break
        proc = start_service(svc)
        PROCS[svc["name"]] = proc
        wait_health(svc)
    log.info("全部服务已尝试拉起。状态：")
    status()


def _terminate(proc: subprocess.Popen, name: str, timeout: float = 8.0) -> None:
    if proc.poll() is not None:
        return
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


def stop_service(name: str) -> None:
    """关停单个服务（保留其余）。"""
    proc = PROCS.get(name)
    if proc is None:
        return
    _terminate(proc, name)
    PROCS.pop(name, None)
    log.info("%s 已关停。", name)


def restart_service(name: str) -> None:
    """重启单个服务：先停后起并等待健康。"""
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
        proc = PROCS.get(name)
        if proc and proc.poll() is None:
            ok = health_ok(port)
            print(f"  {name:16s} pid={proc.pid}  port={port}  health={'✓' if ok else '…(未就绪/降级)'}")
        else:
            print(f"  {name:16s}  stopped")


def _watchdog() -> None:
    """崩溃重启守护（仅重启意外退出的服务）。"""
    while _RUNNING:
        for svc in SERVICES:
            proc = PROCS.get(svc["name"])
            if proc and proc.poll() is not None and _RUNNING:
                log.warning("%s 意外退出(code=%s)，重启中…", svc["name"], proc.returncode)
                new = start_service(svc)
                PROCS[svc["name"]] = new
                wait_health(svc)
        time.sleep(3.0)


def _handle_signal(signum, frame):
    log.info("收到信号 %s，准备退出", signum)
    stop_all()
    sys.exit(0)


def main_start_no_block() -> None:
    """非阻塞启动：起后台线程拉起全部服务 + 看门狗，立即返回（供桌面壳调用）。"""
    global _RUNNING
    _RUNNING = True
    import threading
    threading.Thread(target=start_all, daemon=True).start()
    threading.Thread(target=_watchdog, daemon=True).start()


def main():
    parser = argparse.ArgumentParser(description="GoofishMasterDesktop 启动器")
    parser.add_argument("action", choices=["start", "stop", "restart", "status", "desktop"],
                        nargs="?", default="desktop" if getattr(sys, "frozen", False) else "start")
    parser.add_argument("--service", default=None,
                        help="冻结模式下以独立进程运行指定服务（内部使用）")
    args = parser.parse_args()

    # 冻结模式：子进程运行单个服务
    if args.service:
        run_service_mode(args.service)
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
        stop_all()
        return
    if args.action == "restart":
        stop_all()
        time.sleep(1)

    # start / restart
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
