# -*- coding: utf-8 -*-
"""Windows Job Object 进程树管理（零依赖，ctypes 直调 kernel32）。

解决的问题：launcher 用裸 subprocess.Popen 拉起 4 个服务，proc.terminate()
（= TerminateProcess）只能杀直接子进程。主程序被任务管理器强杀 / 崩溃 /
系统休眠异常时，服务进程及其拉起的 Chromium / Playwright node driver
全部成为孤儿进程驻留（spider 的 Chromium 单实例 ~300MB+ 内存）。

方案：创建一个带 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 限制的 Job Object，
把每个服务进程 assign 进去。Windows 上子进程默认继承父进程所属 Job
（除非显式 CREATE_BREAKAWAY_FROM_JOB，我们不设该标志，Playwright 也不设），
因此 Chromium 等孙进程自动纳入同一 Job。当 launcher 进程死亡（含强杀），
其持有的 Job 句柄被内核关闭 → 引用计数归零 → 内核终止 Job 内全部进程。
这是内核级保证，不依赖任何优雅退出路径。

注意：**Job 必须匿名**（CreateJobObjectW 的 name=None）。命名 Job 会被
同机另一实例（如已安装的常驻桌面版）意外共享，一方退出会误杀另一方的
服务树。

已知环境差异：个别沙箱/EDR 环境会按名拦截 `AssignProcessToProcessJobObject`
（防止进程逃逸 Job 沙箱）。模块初始化时探测一次该 API，不可用时整体降级
为「无进程树保护」（与旧行为一致），不影响任何业务功能。
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
from typing import Optional

log = logging.getLogger("jobobject")

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001

# 关键 API 可用性探测（被沙箱/EDR 按名拦截时整体降级，只探测一次）
_API_AVAILABLE = False
if os.name == "nt":
    try:
        _API_AVAILABLE = hasattr(ctypes.windll.kernel32,
                                 "AssignProcessToProcessJobObject")
        if not _API_AVAILABLE:
            log.warning("当前环境无 AssignProcessToProcessJobObject（疑沙箱/EDR "
                        "拦截），进程树保护不可用，按无保护模式运行")
    except Exception:
        _API_AVAILABLE = False


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _new_job() -> Optional[int]:
    """创建一个匿名 Job Object 并设置 KILL_ON_JOB_CLOSE。失败返回 None。

    独立成函数（而非只做模块级单例）以便测试可以创建临时 Job 验证
    关闭句柄即杀进程树的行为，而不影响全局 Job。
    """
    if os.name != "nt" or not _API_AVAILABLE:
        return None
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            log.warning("CreateJobObjectW 失败(err=%s)，进程树保护不可用",
                        ctypes.GetLastError())
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject.restype = ctypes.c_bool
        k32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int,
            ctypes.POINTER(_JOBOBJECT_EXTENDED_LIMIT_INFORMATION), ctypes.c_uint32,
        ]
        ok = k32.SetInformationJobObject(
            handle, _JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            log.warning("SetInformationJobObject 失败(err=%s)，进程树保护不可用",
                        ctypes.GetLastError())
            k32.CloseHandle(ctypes.c_void_p(handle))
            return None
        return handle
    except Exception as e:  # 任何异常都不应阻断服务启动
        log.warning("Job Object 初始化异常：%s（进程树保护不可用）", e)
        return None


_job_handle: Optional[int] = None
_job_failed = False


def get_job() -> Optional[int]:
    """获取（惰性创建）全局 Job Object 句柄。非 Windows / 失败返回 None。"""
    global _job_handle, _job_failed
    if os.name != "nt" or _job_failed:
        return None
    if _job_handle is None:
        _job_handle = _new_job()
        if _job_handle is None:
            _job_failed = True  # 只尝试一次，避免每个服务启动都刷告警
    return _job_handle


def assign(proc: subprocess.Popen, name: str = "") -> bool:
    """把子进程加入全局 Job。成功/已加入返回 True；不可用或失败返回 False。

    失败不阻断业务流程——进程树保护是增强，不是前提。
    """
    job = get_job()
    if job is None or not _API_AVAILABLE:
        return False
    try:
        k32 = ctypes.windll.kernel32
        # Popen 在 Windows 上持有子进程句柄（带 PROCESS_TERMINATE 等权限）
        ph = getattr(proc, "_handle", None)
        owned = False
        if not ph:
            k32.OpenProcess.restype = ctypes.c_void_p
            ph = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE,
                                 False, proc.pid)
            owned = bool(ph)
        if not ph:
            log.warning("无法获取 %s 进程句柄，未加入 Job", name or proc.pid)
            return False
        k32.AssignProcessToProcessJobObject.restype = ctypes.c_bool
        k32.AssignProcessToProcessJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        ok = k32.AssignProcessToProcessJobObject(
            ctypes.c_void_p(job), ctypes.c_void_p(ph))
        if owned:
            k32.CloseHandle(ctypes.c_void_p(ph))
        if not ok:
            # Win8+ 支持嵌套 Job；失败多见于进程已退出或权限异常
            log.warning("%s 加入 Job 失败(err=%s)，该进程树不受退出回收保护",
                        name or proc.pid, ctypes.GetLastError())
        return bool(ok)
    except Exception as e:
        log.warning("%s 加入 Job 异常：%s", name or proc.pid, e)
        return False
