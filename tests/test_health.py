# -*- coding: utf-8 -*-
"""common/health.py —— 三级健康模型。

改造前每个 /api/health 都是 `return {"status":"ok"}`，于是出现过
「GUI 四个绿点、功能全不可用」。这里锁死三条判定规则：
  任一 required 失败 → error（ready=False）
  仅 optional 失败   → degraded（ready=True，功能降级但可用）
  全通过             → healthy
以及健康检查自身的稳健性：单项异常/超时不得让端点 500。
"""
from __future__ import annotations

import asyncio
import time

from common import health

# 无需 @pytest.mark.asyncio：pytest.ini 已设 asyncio_mode = auto，
# async def 测试会自动被 pytest-asyncio 接管。


# ---------- 状态判定 ----------

def test_all_pass_is_healthy():
    checks = [health.make_check("db", True), health.make_check("ai", True, False)]
    r = health.summarize("svc", checks)
    assert r["status"] == "healthy"
    assert r["ready"] is True
    assert r["reasons"] == []


def test_optional_failure_is_degraded_but_ready():
    """可选依赖挂了 → 降级但仍可用，不能报 error 把服务判死。"""
    checks = [health.make_check("db", True),
              health.make_check("ai", False, required=False, detail="未配置 Key")]
    r = health.summarize("svc", checks)
    assert r["status"] == "degraded"
    assert r["ready"] is True
    assert "ai: 未配置 Key" in r["reasons"]


def test_required_failure_is_error():
    checks = [health.make_check("db", False, detail="文件锁死"),
              health.make_check("ai", True, False)]
    r = health.summarize("svc", checks)
    assert r["status"] == "error"
    assert r["ready"] is False


def test_required_failure_wins_over_optional():
    checks = [health.make_check("db", False),
              health.make_check("ai", False, required=False)]
    r = health.summarize("svc", checks)
    assert r["status"] == "error"
    # required 的原因排在前面，便于一眼看到致命项
    assert r["reasons"][0].startswith("db")


def test_empty_checks_is_healthy():
    assert health.summarize("svc", [])["status"] == "healthy"


def test_report_shape():
    r = health.summarize("svc", [health.make_check("db", True)], version="2.1.0")
    assert r["service"] == "svc"
    assert r["version"] == "2.1.0"
    assert r["checks"] == {"db": True}
    assert isinstance(r["details"], list)


# ---------- 单项执行 ----------

async def test_run_check_accepts_bool_and_tuple():
    a = await health.run_check("a", lambda: True)
    assert a["ok"] is True and a["detail"] == ""
    b = await health.run_check("b", lambda: (False, "坏了"))
    assert b["ok"] is False and b["detail"] == "坏了"


async def test_run_check_supports_async_fn():
    async def probe():
        await asyncio.sleep(0)
        return True, "ok"
    r = await health.run_check("async", probe)
    assert r["ok"] is True


async def test_run_check_converts_exception_to_failure():
    """检查函数抛异常必须转成失败项，绝不能把健康端点打成 500。"""
    def boom():
        raise RuntimeError("连接被拒绝")
    r = await health.run_check("boom", boom)
    assert r["ok"] is False
    assert "RuntimeError" in r["detail"]
    assert "连接被拒绝" in r["detail"]


async def test_run_check_times_out():
    async def hang():
        await asyncio.sleep(10)
    r = await health.run_check("hang", hang, timeout=0.05)
    assert r["ok"] is False
    assert "超时" in r["detail"]


async def test_run_check_records_latency():
    r = await health.run_check("x", lambda: True)
    assert isinstance(r["latency_ms"], int) and r["latency_ms"] >= 0


# ---------- 并发聚合 ----------

async def test_gather_report_runs_checks_concurrently():
    """三个各 0.2s 的检查必须并发跑完（~0.2s），而不是串行 0.6s。

    这条很重要：feishu-agent 的 readiness 要探三个下游，串行会让面板刷新
    卡住半秒以上。
    """
    async def slow():
        await asyncio.sleep(0.2)
        return True

    started = time.monotonic()
    r = await health.gather_report(
        "svc", [("a", slow, True), ("b", slow, True), ("c", slow, False)])
    elapsed = time.monotonic() - started

    assert r["status"] == "healthy"
    assert elapsed < 0.5, f"检查未并发执行，耗时 {elapsed:.2f}s"


async def test_gather_report_mixed_results():
    r = await health.gather_report("svc", [
        ("db", lambda: True, True),
        ("ai", lambda: (False, "无 Key"), False),
        ("rag", lambda: (_ for _ in ()).throw(ValueError("坏了")), False),
    ], version="1.0")
    assert r["status"] == "degraded"
    assert r["ready"] is True
    assert r["checks"] == {"db": True, "ai": False, "rag": False}
