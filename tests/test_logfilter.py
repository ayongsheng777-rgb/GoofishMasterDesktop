# -*- coding: utf-8 -*-
"""common/logfilter.py —— 日志脱敏。

目标场景：用户排障时把 data/logs/*.log 发到群里或贴给我们，凭据不能跟着走。
这里既验证「该打码的打掉」，也验证「不该动的别动」——过度脱敏会把日志变成
一堆 REDACTED，反而没法排障。
"""
from __future__ import annotations

import io
import logging

import pytest

from common.logfilter import SensitiveFilter, install, redact


# ---------- 该打码的 ----------

@pytest.mark.parametrize("secret", [
    "sk-abc123456789XYZabcdef",              # OpenAI / DeepSeek
    "AIzaSyD-1234567890abcdefghij",          # Google / Gemini
    "cli_a1b2c3d4e5f6g7h8",                  # 飞书 app_id
])
def test_known_key_prefixes_are_masked(secret):
    out = redact(f"request failed with key {secret}")
    assert secret not in out
    assert "REDACTED" in out
    # 保留可识别前缀，便于确认"是哪把 key"
    assert secret[:4] in out


@pytest.mark.parametrize("line", [
    '{"api_key": "abcdef123456"}',
    "{'app_secret': 'zzzzzzzzzzzz'}",
    'password: "hunter2000"',
    "access_token=aaaaaaaaaaaa",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.body.sig",
])
def test_keyed_assignments_are_masked(line):
    out = redact(line)
    assert "REDACTED" in out, f"未脱敏: {line}"


def test_secret_value_itself_disappears():
    out = redact('{"deepseek_api_key": "sk-supersecretvalue123"}')
    assert "supersecretvalue" not in out


# ---------- 不该动的 ----------

@pytest.mark.parametrize("line", [
    "Search completed: processed=12, stored=8",
    "GET http://127.0.0.1:8912/api/health 200 OK",
    "启动 ai-router (pid=4321) 成功",
    "商品标题: iPhone 15 Pro 256G 国行",
])
def test_normal_logs_untouched(line):
    assert redact(line) == line


def test_short_values_not_over_masked():
    """4 字符以下的值不脱敏——多半是 true/false/0 这类无害配置。"""
    assert redact("token=ok") == "token=ok"


def test_non_string_input_passthrough():
    assert redact(None) is None
    assert redact(123) == 123
    assert redact("") == ""


# ---------- 集成到 logging ----------

def test_filter_masks_msg_and_args(clean_logging):
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(SensitiveFilter())

    lg = logging.getLogger("t.masks")
    lg.handlers[:] = [handler]
    lg.propagate = False
    lg.setLevel(logging.INFO)

    # 敏感值在 msg 里
    lg.info("using sk-inline1234567890")
    # 敏感值在 args 里（log.info("key=%s", k) 这种最常见的写法）
    lg.info("using %s", "sk-fromargs1234567890")
    # dict 风格 args
    lg.info("using %(k)s", {"k": "sk-fromdict1234567890"})

    out = buf.getvalue()
    assert "inline1234567890" not in out
    assert "fromargs1234567890" not in out
    assert "fromdict1234567890" not in out
    assert out.count("REDACTED") == 3


def test_install_covers_handlers_added_later(clean_logging):
    """uvicorn 是在服务启动后才装 handler 的，必须也被覆盖。"""
    buf = io.StringIO()
    logging.basicConfig(level=logging.INFO, stream=buf, force=True)
    install()

    late = logging.getLogger("uvicorn.access.latetest")
    late.propagate = False
    late.setLevel(logging.INFO)
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(message)s"))
    late.addHandler(h)  # 补丁应在此刻自动挂上 filter

    assert any(isinstance(f, SensitiveFilter) for f in h.filters)
    late.info("GET /cb?token=abcdef123456 200")
    assert "abcdef123456" not in buf.getvalue()


def test_install_is_idempotent(clean_logging):
    """重复安装不得叠加多个 filter（否则每条日志被扫 N 遍）。"""
    buf = io.StringIO()
    logging.basicConfig(level=logging.INFO, stream=buf, force=True)
    for _ in range(3):
        install()
    root = logging.getLogger()
    for h in root.handlers:
        n = sum(isinstance(f, SensitiveFilter) for f in h.filters)
        assert n <= 1


def test_filter_never_breaks_logging(clean_logging):
    """脱敏内部异常绝不能吞掉日志本身。"""
    class Boom:
        def __str__(self):
            return "sk-boom1234567890"

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(SensitiveFilter())
    lg = logging.getLogger("t.boom")
    lg.handlers[:] = [handler]
    lg.propagate = False
    lg.setLevel(logging.INFO)

    lg.info("obj=%s", Boom())          # 非 str args，过滤器应原样放行
    assert "obj=" in buf.getvalue()
