# -*- coding: utf-8 -*-
"""pytest 公共夹具。

测试目标是 `common/` 与 `launcher.py` 里的**纯逻辑**：配置合并、密钥加解密、
拓扑排序、环境变量裁剪、日志脱敏、健康聚合。这些是回归事故的高发区，且
不需要真的拉起服务/浏览器就能验证。

刻意不测的部分：
- 需要真实 Chromium 的采集流程（跑一次好几分钟，放冒烟测试 smoke_test.py）
- 需要真实飞书/AI 凭据的外部调用
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@contextlib.contextmanager
def guarded_stdio():
    """导入期 stdio 保护。

    `services/spider-service/src/ai_handler.py` 在 **import 时**用
    `io.TextIOWrapper(sys.stdout.buffer)` 重建 stdout/stderr。被包装的
    wrapper 一旦 GC，会连带关闭底层 buffer——若那是 pytest 的捕获流，
    后续所有 capture 读写都会炸 `ValueError: I/O operation on closed file`
    （表现为本测试文件之后的用例成批 ERROR）。

    对策：导入期间换成一次性 BytesIO 替身流，让被关闭的是替身，导完还原。
    任何会（间接）导入 spider-service 模块链的 fixture 都必须用它包裹
    `importlib.import_module(...)` 调用。
    """
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    try:
        yield
    finally:
        sys.stdout, sys.stderr = real_out, real_err


@pytest.fixture()
def tmp_cfg_path(tmp_path: Path) -> Path:
    """一个临时 config.json 路径（父目录已建好，文件尚不存在）。"""
    d = tmp_path / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d / "config.json"


@pytest.fixture()
def clean_logging():
    """隔离 logging 全局状态，避免测试之间互相污染 handler/filter。"""
    import logging
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
