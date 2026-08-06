# -*- coding: utf-8 -*-
"""spider-service 浏览器生命周期 —— Playwright 进程泄漏回归。

被修复的真实缺陷：`/api/login/qrcode/start` 的异常分支直接 `return {...}`，
既没关浏览器，此时会话也还没写进 `_login_sessions`（注册发生在二维码截图
成功之后）→ TTL 清理器根本看不见它。网络慢时用户反复点"获取二维码"，
每失败一次就漏一个 Chromium + 一个 node 驱动进程，几分钟吃光内存。

这里用假的 playwright/browser 三件套来验证生命周期，不启动真实浏览器
（真跑 Chromium 要几秒且依赖机器环境，不适合放单测）。
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPIDER_DIR = ROOT / "services" / "spider-service"


@pytest.fixture()
def spider(monkeypatch, tmp_path):
    """导入 spider-service/main.py（目录名带连字符，只能按路径加载）。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ACCOUNT_STATE_DIR", str(tmp_path / "state"))
    sys.path.insert(0, str(SPIDER_DIR))
    sys.modules.pop("main", None)
    try:
        mod = importlib.import_module("main")
    except Exception as e:  # 缺 playwright 等依赖时跳过而非报红
        pytest.skip(f"spider-service 依赖不全，跳过: {e}")
    yield mod
    sys.modules.pop("main", None)
    if str(SPIDER_DIR) in sys.path:
        sys.path.remove(str(SPIDER_DIR))


# ---------- 假 Playwright 三件套 ----------

class FakePage:
    def __init__(self, fail_on_goto=False):
        self._fail = fail_on_goto
        self.url = "https://passport.goofish.com/login"
        self.frames = []
        self.closed = False

    async def goto(self, *a, **k):
        if self._fail:
            raise TimeoutError("navigation timeout")

    async def wait_for_timeout(self, ms):
        return None

    async def title(self):
        return "登录"

    async def screenshot(self, **k):
        return b"PNG"

    async def content(self):
        return "<html></html>"

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page):
        self._page = page
        self.closed = False

    async def new_page(self):
        return self._page

    async def add_init_script(self, script):
        return None

    async def storage_state(self):
        return {"cookies": [{"name": "x"}]}

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


def _install_fake_launcher(monkeypatch, spider, *, fail_on_goto=False):
    """替换 _launch_browser，返回可断言的假三件套。"""
    created = []

    async def fake_launch():
        pw, browser = FakePlaywright(), FakeBrowser()
        page = FakePage(fail_on_goto=fail_on_goto)
        ctx = FakeContext(page)
        created.append({"pw": pw, "browser": browser, "context": ctx, "page": page})
        return pw, browser, ctx

    monkeypatch.setattr(spider, "_launch_browser", fake_launch)
    return created


# ---------- 泄漏回归 ----------

async def test_qrcode_start_failure_releases_browser(spider, monkeypatch):
    """启动失败时必须关浏览器 + 停驱动，且不留下悬空会话。"""
    created = _install_fake_launcher(monkeypatch, spider, fail_on_goto=True)

    resp = await spider.login_qrcode_start()

    assert resp["status"] == "error"
    assert len(created) == 1
    inst = created[0]
    assert inst["browser"].closed, "启动失败后浏览器未关闭 → Chromium 进程泄漏"
    assert inst["pw"].stopped, "启动失败后 playwright 驱动未停止 → node 进程泄漏"
    assert spider._login_sessions == {}, "失败的会话不应留在会话表里"


async def test_qrcode_start_success_keeps_browser_alive(spider, monkeypatch):
    """成功路径要保留浏览器等用户扫码——不能被清理逻辑误杀。"""
    created = _install_fake_launcher(monkeypatch, spider)
    try:
        resp = await spider.login_qrcode_start()
        assert resp["status"] == "waiting"
        assert resp["qrcode_img"]
        inst = created[0]
        assert not inst["browser"].closed
        assert len(spider._login_sessions) == 1
    finally:
        for sid in list(spider._login_sessions):
            await spider._close_login_session(sid)


async def test_concurrent_session_cap_enforced(spider, monkeypatch):
    """超过上限直接 429，而不是无限拉起 Chromium 把内存吃光。"""
    from fastapi import HTTPException
    _install_fake_launcher(monkeypatch, spider)
    monkeypatch.setattr(spider, "_MAX_LOGIN_SESSIONS", 2)
    try:
        await spider.login_qrcode_start()
        await spider.login_qrcode_start()
        assert len(spider._login_sessions) == 2

        with pytest.raises(HTTPException) as ei:
            await spider.login_qrcode_start()
        assert ei.value.status_code == 429
    finally:
        for sid in list(spider._login_sessions):
            await spider._close_login_session(sid)


async def test_stale_sessions_are_purged(spider, monkeypatch):
    """超过 TTL 的会话必须被回收（用户开了二维码就再也不回来的场景）。"""
    created = _install_fake_launcher(monkeypatch, spider)
    await spider.login_qrcode_start()
    sid = next(iter(spider._login_sessions))

    # 把创建时间往前拨到 TTL 之外
    spider._login_sessions[sid]["created_at"] -= spider._LOGIN_SESSION_TTL + 1
    await spider._purge_stale_login_sessions()

    assert spider._login_sessions == {}
    assert created[0]["browser"].closed
    assert created[0]["pw"].stopped


async def test_close_login_session_is_idempotent(spider, monkeypatch):
    """重复关闭不得抛异常（成功回调 + TTL 清扫可能撞车）。"""
    _install_fake_launcher(monkeypatch, spider)
    await spider.login_qrcode_start()
    sid = next(iter(spider._login_sessions))
    await spider._close_login_session(sid)
    await spider._close_login_session(sid)   # 第二次应静默返回
    assert spider._login_sessions == {}


async def test_shutdown_browser_survives_hanging_close(spider, monkeypatch):
    """browser.close() 卡死时必须超时返回，不能吊住 event loop。"""
    class HangingBrowser:
        async def close(self):
            await asyncio.sleep(60)

    monkeypatch.setattr(spider, "_BROWSER_CLOSE_TIMEOUT", 0.05)
    pw = FakePlaywright()

    await asyncio.wait_for(
        spider._shutdown_browser(pw, HangingBrowser(), label="hang"),
        timeout=3,
    )
    # 浏览器关不掉也必须继续把驱动停掉，否则 node 进程照样残留
    assert pw.stopped


async def test_shutdown_browser_tolerates_none(spider):
    await spider._shutdown_browser(None, None)  # 不应抛异常


async def test_app_shutdown_closes_all_sessions(spider, monkeypatch):
    """进程退出钩子要兜底回收，避免留下孤儿 chrome.exe。"""
    created = _install_fake_launcher(monkeypatch, spider)
    monkeypatch.setattr(spider, "_MAX_LOGIN_SESSIONS", 3)
    await spider.login_qrcode_start()
    await spider.login_qrcode_start()

    await spider._shutdown_login_sessions()

    assert spider._login_sessions == {}
    assert all(c["browser"].closed and c["pw"].stopped for c in created)
