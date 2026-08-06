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

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
