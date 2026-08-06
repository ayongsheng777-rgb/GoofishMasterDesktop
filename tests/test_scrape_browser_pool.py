# -*- coding: utf-8 -*-
"""spider 采集浏览器复用池（_ScrapeBrowserPool）单元测试。

用假的 playwright/browser 验证生命周期，不启动真实浏览器。
覆盖：同键复用 / 异键隔离 / 崩溃丢弃重建 / 空闲回收 / 使用中不回收 /
启动失败清理驱动 / shutdown 全量回收。
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

from conftest import guarded_stdio

ROOT = Path(__file__).resolve().parent.parent
SPIDER_DIR = ROOT / "services" / "spider-service"


@pytest.fixture()
def scraper(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ACCOUNT_STATE_DIR", str(tmp_path / "state"))
    sys.path.insert(0, str(SPIDER_DIR))
    for m in ("src.scraper", "src.ai_handler"):
        sys.modules.pop(m, None)
    # src/ai_handler.py 导入期会重建 sys.stdout/stderr，必须罩导入期保护，
    # 否则拆坏 pytest 捕获流（见 conftest.guarded_stdio）。
    try:
        with guarded_stdio():
            mod = importlib.import_module("src.scraper")
    except Exception as e:  # 缺 playwright 等依赖时跳过而非报红
        pytest.skip(f"scraper 依赖不全，跳过: {e}")
    yield mod
    sys.modules.pop("src.scraper", None)
    if str(SPIDER_DIR) in sys.path:
        sys.path.remove(str(SPIDER_DIR))


class FakeBrowser:
    def __init__(self):
        self.closed = False
        self._connected = True

    def is_connected(self):
        return self._connected

    async def close(self):
        self.closed = True
        self._connected = False


class FakePlaywright:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


def _make_pool(scraper, fail_launch: bool = False):
    pool = scraper._ScrapeBrowserPool()
    launches = {"n": 0}

    async def fake_launch(proxy_server):
        launches["n"] += 1
        if fail_launch:
            raise RuntimeError("boom")
        return FakePlaywright(), FakeBrowser()

    pool._launch = fake_launch
    return pool, launches


async def test_same_key_reuses_instance(scraper):
    pool, launches = _make_pool(scraper)
    e1 = await pool.acquire(None)
    await pool.release(e1)
    e2 = await pool.acquire(None)
    assert e2 is e1, "同启动参数应复用同一实例"
    assert launches["n"] == 1, "不应重复启动浏览器"
    await pool.release(e2)
    await pool.shutdown()


async def test_different_proxy_gets_own_instance(scraper):
    pool, launches = _make_pool(scraper)
    e1 = await pool.acquire(None)
    e2 = await pool.acquire("http://127.0.0.1:7890")
    assert e1 is not e2, "代理不同不得共享实例"
    assert launches["n"] == 2
    await pool.shutdown()


async def test_crashed_browser_dropped_and_rebuilt(scraper):
    pool, launches = _make_pool(scraper)
    e1 = await pool.acquire(None)
    e1.browser._connected = False  # 模拟任务期间浏览器崩溃
    await pool.release(e1)         # release 应检测并丢弃
    await asyncio.sleep(0.05)      # 等后台 dispose
    assert e1.browser.closed
    e2 = await pool.acquire(None)
    assert e2 is not e1 and launches["n"] == 2, "崩溃实例应被替换重建"
    await pool.shutdown()


async def test_idle_entry_reaped_by_sweeper(scraper):
    pool, _ = _make_pool(scraper)
    pool.IDLE_TTL = 0.05
    pool.SWEEP_INTERVAL = 0.05
    e = await pool.acquire(None)
    pw = e.playwright
    await pool.release(e)
    for _ in range(20):  # 最多等 1s
        await asyncio.sleep(0.05)
        if pw.stopped:
            break
    assert e.browser.closed and pw.stopped, "空闲超时实例应被清扫回收"


async def test_busy_entry_never_reaped(scraper):
    pool, _ = _make_pool(scraper)
    pool.IDLE_TTL = 0.05
    pool.SWEEP_INTERVAL = 0.05
    e = await pool.acquire(None)   # 持有不 release → users>0
    await asyncio.sleep(0.3)
    assert not e.browser.closed, "使用中的实例绝不应被回收"
    await pool.release(e)
    await pool.shutdown()


async def test_launch_failure_stops_driver(scraper):
    pool = scraper._ScrapeBrowserPool()

    async def fail_launch(proxy_server):
        raise RuntimeError("boom")
    pool._launch = fail_launch
    with pytest.raises(RuntimeError):
        await pool.acquire(None)
    assert pool._entries == {}, "启动失败不得在池中留下半初始化条目"


async def test_shutdown_disposes_all(scraper):
    pool, _ = _make_pool(scraper)
    e1 = await pool.acquire(None)
    e2 = await pool.acquire("http://proxy:1")
    await pool.shutdown()
    assert e1.browser.closed and e1.playwright.stopped
    assert e2.browser.closed and e2.playwright.stopped
    assert pool._entries == {}
