# -*- coding: utf-8 -*-
"""
GoofishMasterDesktop · 桌面壳（pywebview + pystray）
- 打开本地控制台页面（desktop/ui/index.html），不再跳转浏览器后台
- 系统托盘：显示控制台 / 启动后端 / 停止后端 / 退出
- 启动时拉起 launcher 后端，退出时优雅关停
- 任何异常都会落盘 data/logs/desktop-crash.log 并弹窗，避免"静默闪退"

依赖：pip install pywebview pystray
（无 GUI 环境如 CI/服务器上运行会优雅跳过，仅启动后端）
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import traceback
from pathlib import Path

log = logging.getLogger("desktop")


def _show_error(title: str, message: str) -> None:
    """弹出 Windows 错误框（无 GUI 会话时静默失败）。"""
    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass


def _write_crash(stage: str, exc: BaseException) -> None:
    try:
        from common import config as cfg_mod
        logdir = cfg_mod.APP_DIR / "data" / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        with open(logdir / "desktop-crash.log", "a", encoding="utf-8") as f:
            from datetime import datetime
            f.write(f"\n=== [{stage}] 崩溃 @ {datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
            f.write(f"exe = {sys.executable}\nfrozen = {getattr(sys, 'frozen', False)}\n")
            f.write(traceback.format_exc())
            f.write("\n")
    except Exception:
        pass


def _webview2_installed() -> bool:
    """检测 Microsoft Edge WebView2 Runtime 是否已安装（固定注册表 GUID）。"""
    try:
        import winreg
    except Exception:
        return True  # 非 Windows / 无法检测 → 假定可用
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


def _resolve_webview2_runtime() -> "str | None":
    """返回随包携带的固定版本 WebView2 运行时目录（含 msedgewebview2.exe）。

    该目录由安装器随附到 exe 同级（webview2_runtime/），或开发态位于仓库根。
    优先使用它可让程序完全不依赖系统 WebView2 Runtime，免 UAC、免联网。
    原生 WebView2 加载器会直接读取环境变量 WEBVIEW2_RUNTIME_PATH。
    """
    try:
        cand = Path(sys.executable).resolve().parent / "webview2_runtime"
        if (cand / "msedgewebview2.exe").exists():
            return str(cand)
    except Exception:
        pass
    return None


def run():
    # 固定版本 WebView2 运行时：若随包携带 webview2_runtime/，则优先使用
    # （原生 WebView2 加载器直接读 WEBVIEW2_RUNTIME_PATH，免系统 Runtime / 免 UAC / 免联网）
    _rt = _resolve_webview2_runtime()
    if _rt:
        os.environ["WEBVIEW2_RUNTIME_PATH"] = _rt

    # 业务依赖延迟导入，且包在 try 内：模块级只依赖标准库，绝不因业务 import 失败而整机崩溃
    try:
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
        from common import config as cfg_mod
        import launcher
        from desktop.api import Api
    except Exception as e:
        _write_crash("import", e)
        _show_error("GoofishMasterDesktop", f"加载依赖失败：\n{e}\n\n详见 data/logs/desktop-crash.log")
        return

    log.warning("[desktop] stage=import_ok")

    try:
        try:
            import pywebview as webview  # noqa  (部分环境 import 名为 pywebview)
        except ImportError:
            import webview  # noqa            (本环境实际 import 名为 webview)
        log.warning("[desktop] stage=webview_imported")
        import pystray  # noqa
        log.warning("[desktop] stage=pystray_imported")
    except ImportError as e:
        log.warning("未安装 pywebview/pystray，降级为仅后端模式：%s", e)
        launcher.main_start_no_block()
        return

    # 注：WebView2 运行时检测改由下方 webview.start() 的 except 捕获并友好提示，
    # 避免在无桌面会话（如服务/无头）下探测注册表导致原生崩溃。
    try:
        ui_path = cfg_mod.ROOT / "desktop" / "ui" / "index.html"
        if not ui_path.exists():
            raise FileNotFoundError(f"找不到控制台页面：{ui_path}")
        url = ui_path.as_uri()  # file:// 绝对路径，WebView2 可直接加载
        log.warning("[desktop] stage=ui_path=%s", ui_path)

        # 品牌图标：优先用打包进 _internal 的 app.ico（多尺寸），缺失时回退 logo-256.png
        # 注：当前 pywebview 版本的 create_window 不支持 icon= 关键字（会 TypeError 崩溃），
        # 窗口图标由 exe 内嵌的 app.ico（PyInstaller spec 已设）提供，无需此参数。
        icon_path = cfg_mod.ROOT / "app.ico"

        api = Api()
        window = webview.create_window(
            "GoofishMasterDesktop · 桌面控制台",
            url=url,
            js_api=api,
            width=1180,
            height=760,
            min_size=(900, 600),
        )
        log.warning("[desktop] stage=window_created")

        # 启动后端（后台线程：拉起 4 服务 + 看门狗）
        launcher.main_start_no_block()
        log.warning("[desktop] stage=backend_started")

        # 关闭窗口 → 最小化到托盘（尽力；旧版 API 不支持则直接退出）
        def on_closing():
            try:
                window.hide()
            except Exception:
                pass
            return False

        try:
            window.closing_event = on_closing
        except Exception:
            pass

        # 最小化窗口 → 同样隐藏到托盘（不占任务栏；托盘「显示控制台」召回）
        def on_minimized():
            try:
                window.hide()
            except Exception:
                pass

        try:
            window.events.minimized += on_minimized
        except Exception:
            pass  # 旧版 pywebview 无 events API，最小化走系统默认行为

        # 托盘图标：优先 app.ico（与 exe / 安装器统一品牌），缺失回退 logo-256.png
        icon_img = None
        for cand in (icon_path, cfg_mod.ROOT / "services" / "feishu-agent" / "static" / "logo-256.png"):
            try:
                import PIL.Image
                if cand and Path(cand).exists():
                    icon_img = PIL.Image.open(cand)
                    break
            except Exception:
                icon_img = None

        from pystray import Menu, MenuItem

        def show_window(icon, item):
            try:
                window.show()
            except Exception:
                pass

        def open_frontend(icon, item):
            """系统浏览器打开管理后台（端口随 config.json）。"""
            try:
                import webbrowser
                port = cfg_mod.load_config().get("ports", {}).get("feishu_agent", 8911)
                webbrowser.open(f"http://127.0.0.1:{port}")
            except Exception:
                pass

        def open_logs(icon, item):
            """资源管理器打开日志目录。"""
            try:
                logdir = cfg_mod.APP_DIR / "data" / "logs"
                logdir.mkdir(parents=True, exist_ok=True)
                os.startfile(str(logdir))  # Windows
            except Exception:
                pass

        def start_backend(icon, item):
            if not launcher.PROCS:
                launcher.main_start_no_block()

        def stop_backend(icon, item):
            launcher.stop_all()

        def quit_app(icon, item):
            launcher.stop_all()
            try:
                window.destroy()
            except Exception:
                pass
            icon.stop()

        menu = Menu(
            MenuItem("显示控制台", show_window),
            MenuItem("打开管理后台", open_frontend),
            MenuItem("查看日志", open_logs),
            Menu.SEPARATOR,
            MenuItem("启动后端", start_backend),
            MenuItem("停止后端", stop_backend),
            Menu.SEPARATOR,
            MenuItem("退出", quit_app),
        )
        icon = pystray.Icon("goofish", icon_img, "GoofishMasterDesktop", menu)

        # 托盘在独立线程运行，主线程交给 pywebview 事件循环
        threading.Thread(target=icon.run, daemon=True).start()
        log.warning("[desktop] stage=tray_started")

        log.warning("[desktop] stage=before_start")
        webview.start()
        log.warning("[desktop] stage=pywebview_returned")
    except Exception as e:
        _write_crash("run", e)
        log.error("桌面运行异常:\n%s", traceback.format_exc())
        msg = f"桌面窗口启动异常：\n{e}\n\n详见 data/logs/desktop-crash.log"
        # WebView2 Runtime 缺失时的友好指引
        if "webview2" in str(e).lower():
            if _rt:
                msg = ("WebView2 运行时加载失败，但已检测到随包携带的固定版本运行时。\n\n"
                       "请确认安装目录下 webview2_runtime\\msedgewebview2.exe 未被杀毒软件隔离或删除，\n"
                       "或重启后再试。")
            else:
                msg = ("未找到随包携带的 WebView2 固定版本运行时（webview2_runtime 目录缺失），\n"
                       "且系统未安装 Microsoft Edge WebView2 Runtime。\n\n"
                       "请重新安装本软件，或从以下地址手动安装 WebView2 Runtime：\n"
                       "https://developer.microsoft.com/zh-cn/microsoft-edge/webview2/\n\n"
                       "（后端服务仍可后台运行，可在浏览器打开 http://127.0.0.1:8911 管理）")
        _show_error("GoofishMasterDesktop · 启动失败", msg)
    finally:
        # 窗口真正关闭（或销毁）后，确保后端关停
        try:
            launcher.stop_all()
        except Exception:
            pass


if __name__ == "__main__":
    run()
