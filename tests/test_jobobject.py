# -*- coding: utf-8 -*-
"""common/jobobject.py 单元测试（仅 Windows 有意义，其他平台全跳过）。

注意：个别沙箱/EDR 环境按名拦截 AssignProcessToProcessJobObject
（防 Job 逃逸），此时功能级测试跳过，降级行为测试仍必须全绿——
降级路径（无保护模式）正是这些环境下的真实运行路径。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Job Object 仅 Windows")

from common import jobobject  # noqa: E402

_QUERY_LIMITED = 0x1000
_need_api = pytest.mark.skipif(
    not jobobject._API_AVAILABLE,
    reason="当前环境拦截 AssignProcessToProcessJobObject（沙箱/EDR），跳过功能测试")


def _pid_alive(pid: int) -> bool:
    h = ctypes.windll.kernel32.OpenProcess(_QUERY_LIMITED, False, pid)
    if not h:
        return False
    ctypes.windll.kernel32.CloseHandle(h)
    return True


def _wait_dead(pid: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    return not _pid_alive(pid)


@_need_api
def test_new_job_returns_handle():
    h = jobobject._new_job()
    assert h, "CreateJobObjectW 应返回有效句柄"
    ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))


@_need_api
def test_get_job_idempotent():
    j1 = jobobject.get_job()
    j2 = jobobject.get_job()
    assert j1 and j1 == j2


@_need_api
def test_assign_real_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    try:
        assert jobobject.assign(proc, "test-child") is True
        # IsProcessInJob 验证归属
        job = jobobject.get_job()
        in_job = ctypes.c_bool(False)
        ok = ctypes.windll.kernel32.IsProcessInJob(
            ctypes.c_void_p(proc._handle), ctypes.c_void_p(job),
            ctypes.byref(in_job))
        assert ok and in_job.value
    finally:
        proc.kill()
        proc.wait()


@_need_api
def test_kill_on_job_close_reaps_tree():
    """关闭 Job 句柄 → 子进程及其孙进程全部被内核回收（核心保证）。"""
    # 子进程自身再拉起一个孙进程（模拟 spider → Chromium）
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess,sys,time;"
         "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
         "print(p.pid,flush=True);time.sleep(60)"],
        stdout=subprocess.PIPE, text=True)
    try:
        grandchild_pid = int(child.stdout.readline().strip())
        assert _pid_alive(child.pid) and _pid_alive(grandchild_pid)

        # 用独立 Job（不动全局单例）验证关闭即回收
        job = jobobject._new_job()
        assert job
        k32 = ctypes.windll.kernel32
        k32.AssignProcessToProcessJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        assert k32.AssignProcessToProcessJobObject(
            ctypes.c_void_p(job), ctypes.c_void_p(child._handle))

        k32.CloseHandle(ctypes.c_void_p(job))  # 引用归零 → KILL_ON_JOB_CLOSE
        assert _wait_dead(child.pid), "关闭 Job 后子进程应被回收"
        assert _wait_dead(grandchild_pid), "关闭 Job 后孙进程应被级联回收"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_assign_failure_does_not_raise():
    """非 Windows 句柄/异常输入不得抛出（进程树保护是增强不是前提）。"""
    class FakeProc:
        _handle = None
        pid = 99999999  # 不存在的 PID → OpenProcess 失败
    assert jobobject.assign(FakeProc(), "ghost") is False


def test_api_blocked_degrades_gracefully():
    """API 被拦截的环境：get_job/assign 一律静默降级，不抛异常。"""
    if jobobject._API_AVAILABLE:
        pytest.skip("当前环境 API 可用，降级路径由 FakeProc 用例覆盖")
    assert jobobject.get_job() is None
    assert jobobject._new_job() is None
    proc = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(5)"])
    try:
        assert jobobject.assign(proc, "degraded-child") is False
    finally:
        proc.kill()
        proc.wait()
