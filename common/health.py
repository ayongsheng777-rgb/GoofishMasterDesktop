# -*- coding: utf-8 -*-
"""三级健康模型（liveness / readiness / degraded）

背景：改造前每个服务的 `/api/health` 都只是 `return {"status": "ok"}` —— 进程
还活着就永远绿灯。于是出现「GUI 四个绿点，功能全不可用」：数据库句柄坏了、
AI Key 失效、Chroma 目录损坏、Chromium 缺失，健康检查一概看不见。

现在分三层：

- **liveness**（`/api/health/live`）：进程在跑就是 200。用于看门狗判断要不要重启，
  必须极轻量、绝不依赖外部资源，否则依赖抖动会引发重启风暴。
- **readiness**（`/api/health/ready`）：真实探测各依赖，返回明细。
- **degraded**：可选依赖挂了但主功能仍可用时的中间态——不报错、不掩盖。

状态判定：
  任一 **required** 检查失败      → `error`
  仅 **optional** 检查失败        → `degraded`
  全通过                          → `healthy`

`/api/health` 保持原样（返回 200 + status:ok），老调用方与看门狗不受影响。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, List

# 单项检查的超时（秒）——健康检查本身绝不能拖垮服务
CHECK_TIMEOUT = 3.0


def make_check(name: str, ok: bool, required: bool = True,
               detail: str = "") -> Dict[str, Any]:
    return {"name": name, "ok": bool(ok), "required": required, "detail": detail}


async def run_check(name: str, fn: Callable[[], Awaitable[Any] | Any],
                    required: bool = True,
                    timeout: float = CHECK_TIMEOUT) -> Dict[str, Any]:
    """执行单项检查，捕获一切异常与超时。

    检查函数可返回 bool，或 (bool, detail) 元组。任何异常都被转成失败项而不是
    500——健康端点自身崩掉是最糟糕的情况（会被误判为整个服务死亡）。
    """
    started = time.time()
    try:
        res = fn()
        if asyncio.iscoroutine(res):
            res = await asyncio.wait_for(res, timeout=timeout)
        detail = ""
        if isinstance(res, tuple):
            ok, detail = res[0], str(res[1])
        else:
            ok = bool(res)
        chk = make_check(name, ok, required, detail)
    except asyncio.TimeoutError:
        chk = make_check(name, False, required, f"检查超时（>{timeout}s）")
    except Exception as e:
        chk = make_check(name, False, required, f"{type(e).__name__}: {e}")
    chk["latency_ms"] = int((time.time() - started) * 1000)
    return chk


def summarize(service: str, checks: List[Dict[str, Any]],
              version: str = "") -> Dict[str, Any]:
    """把检查明细汇总成 readiness 报告。"""
    failed_required = [c for c in checks if c["required"] and not c["ok"]]
    failed_optional = [c for c in checks if not c["required"] and not c["ok"]]
    if failed_required:
        status = "error"
    elif failed_optional:
        status = "degraded"
    else:
        status = "healthy"
    reasons = [f"{c['name']}: {c['detail'] or '不可用'}"
               for c in failed_required + failed_optional]
    report: Dict[str, Any] = {
        "service": service,
        "status": status,
        "ready": status != "error",
        "checks": {c["name"]: c["ok"] for c in checks},
        "details": checks,
        "reasons": reasons,
    }
    if version:
        report["version"] = version
    return report


async def gather_report(service: str, specs: List[tuple],
                        version: str = "") -> Dict[str, Any]:
    """并发执行检查并汇总。

    specs: [(name, fn, required), ...]
    """
    tasks = [run_check(n, f, r) for (n, f, r) in specs]
    checks = await asyncio.gather(*tasks)
    return summarize(service, list(checks), version)
