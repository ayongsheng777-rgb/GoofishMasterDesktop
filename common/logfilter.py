# -*- coding: utf-8 -*-
"""日志脱敏过滤器

问题：AI Key / 飞书 Secret / 会话 token 会经由异常栈、请求体回显、调试打印
进入 `data/logs/*.log`。用户排障时把日志发到群里或贴给我们，凭据就跟着泄漏了。

做法：给 root logger 挂一个 `logging.Filter`，在**写盘之前**把敏感片段替换成
掩码（保留前若干位便于对账，例如 `sk-abc***REDACTED***`）。

性能：日志是热路径，因此先做一次廉价的子串预筛（`_HINTS`），只有命中的记录
才跑正则。绝大多数普通日志一次 `any(h in s)` 就走掉了。

安全边界：这是「防误泄漏」而非「防攻击者」——能读日志文件的人通常也能读
config.json。目标是让日常排障、截图、发工单不再顺手带出密钥。
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

# 廉价预筛：只有日志文本包含这些子串时才跑正则
_HINTS = ("sk-", "cli_", "AIza", "Bearer ", "api_key", "apikey", "app_secret",
          "secret", "token", "password", "passwd", "authorization")

_MASK = "***REDACTED***"


def _keep_head(m: re.Match) -> str:
    """保留可识别前缀（便于确认"是哪把 key"），其余打码。"""
    s = m.group(0)
    head = s[:6] if len(s) > 10 else s[:2]
    return f"{head}{_MASK}"


# 注意：顺序有意义——先匹配更具体的模式
_PATTERNS: list[tuple[re.Pattern, object]] = [
    # OpenAI / DeepSeek / Moonshot 等 sk- 前缀
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), _keep_head),
    # Google / Gemini
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{10,}"), _keep_head),
    # 飞书 app_id / app_secret（cli_ 前缀）
    (re.compile(r"\bcli_[A-Za-z0-9]{8,}"), _keep_head),
    # HTTP Authorization / Bearer
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}"),
     lambda m: f"{m.group(1)} {_MASK}"),
    # JSON 风格： "api_key": "xxx" / "app_secret":"xxx" / "token": "xxx"
    (re.compile(
        r'(?i)(["\']?(?:api[_-]?key|app[_-]?secret|secret[_-]?key|access[_-]?token'
        r'|refresh[_-]?token|token|password|passwd)["\']?\s*[:=]\s*)'
        r'(["\'])([^"\']{4,})(["\'])'),
     lambda m: f"{m.group(1)}{m.group(2)}{_MASK}{m.group(4)}"),
    # 裸赋值风格： api_key=xxx（无引号，到空白/&/; 结束）
    (re.compile(
        r'(?i)\b((?:api[_-]?key|app[_-]?secret|secret[_-]?key|access[_-]?token'
        r'|token|password|passwd)\s*[:=]\s*)([^\s"\',;&]{4,})'),
     lambda m: f"{m.group(1)}{_MASK}"),
]


def redact(text: str) -> str:
    """对单段文本做脱敏。非字符串或无命中时原样返回。"""
    if not text or not isinstance(text, str):
        return text
    low = text.lower()
    if not any(h.lower() in low for h in _HINTS):
        return text
    out = text
    for pat, repl in _PATTERNS:
        try:
            out = pat.sub(repl, out)
        except Exception:
            # 脱敏本身绝不能让日志系统崩溃
            continue
    return out


class SensitiveFilter(logging.Filter):
    """把日志记录中的敏感片段替换为掩码。

    同时处理 `record.msg` 与 `record.args`——很多代码写成
    `log.info("key=%s", api_key)`，敏感值在 args 里而不在 msg 里。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: (redact(v) if isinstance(v, str) else v)
                                   for k, v in record.args.items()}
                elif isinstance(record.args, tuple):
                    record.args = tuple(
                        redact(a) if isinstance(a, str) else a
                        for a in record.args)
        except Exception:
            pass  # 任何异常都不得影响日志输出
        return True


# uvicorn 的 access/error logger 自带 handler 且 propagate=False，
# 光挂 root 抓不到它们。
_LATE_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access",
                 "httpx", "httpcore", "playwright")

_patched = False


def _attach(target) -> None:
    """给 logger 或 handler 挂上 filter（已挂过则跳过）。"""
    if not any(isinstance(f, SensitiveFilter) for f in target.filters):
        target.addFilter(SensitiveFilter())


def install(loggers: Iterable[str] | None = None,
            auto_patch: bool = True) -> None:
    """把过滤器挂到 root 及指定 logger 的所有 handler 上。

    挂在 handler 而非 logger，是因为 logger 级 filter 不会作用于
    子 logger 冒泡上来的记录（uvicorn / httpx 等第三方日志正是这样上来的）。

    `auto_patch=True` 时额外包一层 `Logger.addHandler`：服务进程里 uvicorn 是在
    `uvicorn.run()` 之后才装自己的 handler 的，模块导入期挂的 filter 覆盖不到。
    包一层之后，任何**后来**添加的 handler 都会自动带上脱敏，不用在每个
    startup 钩子里重复调用。补丁全局只打一次。
    """
    global _patched

    for h in logging.getLogger().handlers:
        _attach(h)

    names = list(loggers) if loggers is not None else list(_LATE_LOGGERS)
    for name in names:
        lg = logging.getLogger(name)
        for h in lg.handlers:
            _attach(h)
        _attach(lg)

    if auto_patch and not _patched:
        _orig_add = logging.Logger.addHandler

        def _patched_add(self, hdlr):  # type: ignore[no-untyped-def]
            try:
                _attach(hdlr)
            except Exception:
                pass
            return _orig_add(self, hdlr)

        logging.Logger.addHandler = _patched_add  # type: ignore[assignment]
        _patched = True
