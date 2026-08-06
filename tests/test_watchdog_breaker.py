# -*- coding: utf-8 -*-
"""launcher 看门狗重启熔断。

背景：旧 _watchdog 对意外退出的服务无限重启——反复崩溃的服务（如配置
损坏导致启动即退）会陷入重启风暴，空耗 CPU/内存并把日志刷爆。
熔断规则：600 秒窗口内自动重启超 5 次 → 停拉并标记 _BROKEN，
手动 restart_service / start_all 清除标记。
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def launcher():
    """在隔离的 APP_DIR 下导入 launcher（避免动到用户真实配置）。"""
    tmp = Path(tempfile.mkdtemp(prefix="gmd_breaker_test_"))
    from common import config as cfg_mod
    orig_app_dir, orig_cfg_path = cfg_mod.APP_DIR, cfg_mod.CONFIG_PATH
    cfg_mod.APP_DIR = tmp
    cfg_mod.CONFIG_PATH = tmp / "config" / "config.json"
    sys.modules.pop("launcher", None)
    import launcher as mod
    yield mod
    cfg_mod.APP_DIR, cfg_mod.CONFIG_PATH = orig_app_dir, orig_cfg_path
    sys.modules.pop("launcher", None)


@pytest.fixture(autouse=True)
def _clean(launcher):
    launcher._BROKEN.clear()
    launcher._RESTART_HISTORY.clear()
    yield
    launcher._BROKEN.clear()
    launcher._RESTART_HISTORY.clear()


def test_restart_within_limit_allowed(launcher):
    for _ in range(launcher._RESTART_MAX):
        assert launcher._record_restart("svc") is True
    assert "svc" not in launcher._BROKEN


def test_breaker_trips_after_limit(launcher):
    for _ in range(launcher._RESTART_MAX):
        launcher._record_restart("svc")
    assert launcher._record_restart("svc") is False
    assert "svc" in launcher._BROKEN


def test_old_restarts_expire_outside_window(launcher):
    # 窗口外的历史不应计入：5 条 11 分钟前的记录 + 1 条新记录 = 不熔断
    launcher._RESTART_HISTORY["svc"] = [
        time.time() - (launcher._RESTART_WINDOW_SEC + 60)
    ] * launcher._RESTART_MAX
    assert launcher._record_restart("svc") is True
    assert "svc" not in launcher._BROKEN


def test_manual_restart_clears_breaker(launcher):
    launcher._BROKEN.add("ghost-svc")
    launcher._RESTART_HISTORY["ghost-svc"] = [time.time()] * 10
    # 不存在的服务：restart_service 提前返回，但熔断清除先执行
    launcher.restart_service("ghost-svc")
    assert "ghost-svc" not in launcher._BROKEN
    assert "ghost-svc" not in launcher._RESTART_HISTORY


def test_breaker_per_service_isolated(launcher):
    for _ in range(launcher._RESTART_MAX + 1):
        launcher._record_restart("bad-svc")
    assert "bad-svc" in launcher._BROKEN
    assert launcher._record_restart("good-svc") is True
    assert "good-svc" not in launcher._BROKEN
