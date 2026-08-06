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


def _version_tuple(v: str) -> tuple:
    """'1.10.2' -> (1, 10, 2)；非数字段按 0 处理，永不抛异常。"""
    out = []
    for part in str(v).split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


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
            "broken": name in launcher._BROKEN,  # 看门狗熔断（重启超限停拉）
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

    def open_github(self) -> None:
        """用系统默认浏览器打开项目 GitHub 仓库页。"""
        try:
            import webbrowser
            webbrowser.open("https://github.com/ayongsheng777-rgb/GoofishMasterDesktop")
        except Exception:
            pass

    def open_help(self) -> None:
        """用系统默认浏览器打开使用说明文档（desktop/ui/help.html）。"""
        try:
            import webbrowser
            p = cfg_mod.ROOT / "desktop" / "ui" / "help.html"
            if p.exists():
                webbrowser.open(p.as_uri())
        except Exception:
            pass

    def open_url(self, url: str) -> None:
        """用系统默认浏览器打开指定 URL（仅允许 http/https，防 JS 侧注入 file://）。"""
        try:
            import webbrowser
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                webbrowser.open(url)
        except Exception:
            pass

    def check_update(self) -> Dict[str, Any]:
        """版本检查 + 更新提示（更新通道第一阶段：只提示，不自动下载）。

        查询 GitHub Releases 最新 tag 与 common.config.APP_VERSION 比较。
        任何网络/解析异常都静默返回 ok=False，前端据此保持横幅隐藏——
        绝不让版本检查影响控制台可用性。
        """
        result: Dict[str, Any] = {
            "ok": False, "current": cfg_mod.APP_VERSION,
            "latest": None, "update_available": False, "url": "", "error": "",
        }
        try:
            import requests
            api_url = f"https://api.github.com/repos/{cfg_mod.GITHUB_REPO}/releases/latest"
            # 用户若已为境外 AI 配了代理，顺手复用（GitHub 在境内也常需代理）
            proxy = (CFG.get("ai") or {}).get("proxy_url") or None
            proxies = {"http": proxy, "https": proxy} if proxy else None
            resp = requests.get(api_url, timeout=5, proxies=proxies,
                                headers={"Accept": "application/vnd.github+json"})
            if resp.status_code != 200:
                result["error"] = f"HTTP {resp.status_code}"
                return result
            data = resp.json()
            latest = str(data.get("tag_name") or "").lstrip("vV")
            result.update(ok=True, latest=latest,
                          url=str(data.get("html_url") or ""))
            result["update_available"] = (
                _version_tuple(latest) > _version_tuple(cfg_mod.APP_VERSION)
            )
            return result
        except Exception as e:  # 断网/超时/DNS/代理解析失败等一律静默
            result["error"] = str(e)[:120]
            return result
