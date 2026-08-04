# -*- coding: utf-8 -*-
"""
GoofishMasterDesktop · 桌面控制台 Python 桥接层
暴露给前端（pywebview JS）调用的 API。本模块不依赖任何 GUI 库，
可独立在命令行/测试中导入，便于无界面环境验证。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List

import launcher  # noqa: E402
import common.config as cfg_mod  # noqa: E402

CFG = cfg_mod.load_config()
_LOCK = threading.Lock()


def _status() -> List[Dict[str, Any]]:
    out = []
    for svc in launcher.SERVICES:
        name = svc["name"]
        port = CFG["ports"][svc["port_key"]]
        proc = launcher.PROCS.get(name)
        running = proc is not None and proc.poll() is None
        health = launcher.health_ok(port) if running else False
        out.append({
            "name": name,
            "port": port,
            "running": running,
            "health": health,
            "pid": proc.pid if running else None,
        })
    return out


def _logs(target: str, lines: int) -> List[str]:
    logdir = cfg_mod.APP_DIR / "data" / "logs"
    path = logdir / ("launcher.log" if target == "launcher" else f"{target}.log")
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.readlines()
        return [ln.rstrip("\n") for ln in data[-max(1, lines):]]
    except Exception as e:  # noqa
        return [f"读取日志失败: {e}"]


def _config() -> Dict[str, Any]:
    backends = CFG.get("backends", {})
    ai = CFG.get("ai", {})
    feishu = CFG.get("feishu", {})
    return {
        "ports": CFG.get("ports", {}),
        "backends_enabled": any(b.get("enabled") for b in backends.values()),
        "ai_configured": {
            "deepseek_api_key": bool(ai.get("deepseek_api_key")),
            "gemini_api_key": bool(ai.get("gemini_api_key")),
            "qwen_api_key": bool(ai.get("qwen_api_key")),
        },
        "feishu_configured": bool(feishu.get("app_id")),
        "app_dir": str(cfg_mod.APP_DIR),
    }


class Api:
    """pywebview js_api 对象：JS 侧通过 window.pywebview.api.<method> 调用。"""

    def get_status(self) -> List[Dict[str, Any]]:
        with _LOCK:
            return _status()

    def start_all(self) -> List[Dict[str, Any]]:
        with _LOCK:
            if not launcher.PROCS:
                launcher.main_start_no_block()
            return _status()

    def stop_all(self) -> List[Dict[str, Any]]:
        with _LOCK:
            launcher.stop_all()
            return _status()

    def restart_service(self, name: str) -> List[Dict[str, Any]]:
        with _LOCK:
            launcher.restart_service(name)
            return _status()

    def get_logs(self, target: str = "launcher", lines: int = 300) -> List[str]:
        return _logs(target, int(lines))

    def get_config(self) -> Dict[str, Any]:
        return _config()

    def check_prerequisites(self) -> Dict[str, Any]:
        """返回 WebView2 Runtime 与随附 Chromium 的检测结果。"""
        return launcher.check_prerequisites()

    def open_data_dir(self) -> None:
        try:
            import os
            os.startfile(str(cfg_mod.APP_DIR))  # Windows
        except Exception:
            try:
                import webbrowser
                webbrowser.open(str(cfg_mod.APP_DIR))
            except Exception:
                pass

    def open_frontend(self) -> None:
        """用系统默认浏览器打开飞书智能体管理后台（127.0.0.1:feishu_agent 端口）。"""
        port = CFG.get("ports", {}).get("feishu_agent", 8911)
        url = f"http://127.0.0.1:{port}"
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
