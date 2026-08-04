# -*- coding: utf-8 -*-
"""Async SQLite helper for ai-router (P1 单用户化：Postgres -> SQLite).

原 asyncpg 驱动在桌面版默认禁用 PG，持久化降级。P1 改为进程内 aiosqlite
文件库（DATA_DIR/goofish.db），零外部依赖。SQL 方言转换与 agent-pipeline
保持一致（详见 agent-pipeline/db.py 顶部说明）。
"""
from __future__ import annotations
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("ai-router.db")

DATA_DIR = Path(os.environ.get("DATA_DIR")
                or Path(__file__).resolve().parents[2] / "data" / "ai-router")
DB_PATH = DATA_DIR / "goofish.db"

DATABASE_ENABLED = os.environ.get("POSTGRES_ENABLED", "true").lower() in ("1", "true", "yes", "on")

_JSON_COLS = set()

_conn = None
_disabled = False


_COUNT_FILTER_RE = re.compile(
    r"COUNT\(\*\)\s+FILTER\s*\(\s*WHERE\s+(.*?)\)\s*AS\s+(\w+)",
    re.IGNORECASE | re.DOTALL,
)


def _norm(sql: str) -> str:
    s = sql
    s = re.sub(r"\$\d+", "?", s)
    s = re.sub(r"::\w+", "", s)
    s = re.sub(r"\bILIKE\b", "LIKE", s, flags=re.IGNORECASE)
    s = re.sub(r"\bEXCLUDED\b", "excluded", s, flags=re.IGNORECASE)
    s = re.sub(r"NOW\(\)\s*-\s*INTERVAL\s+'[^']*'", "datetime('now','-7 days')", s, flags=re.IGNORECASE)
    s = re.sub(r"CURRENT_DATE\s*-\s*INTERVAL\s+'[^']*'", "DATE('now','-6 days')", s, flags=re.IGNORECASE)
    s = re.sub(r"\bCURRENT_DATE\b", "DATE('now')", s, flags=re.IGNORECASE)
    s = _COUNT_FILTER_RE.sub(r"SUM(CASE WHEN \1 THEN 1 ELSE 0 END) AS \2", s)
    return s


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT,
    input TEXT,
    output TEXT,
    model TEXT,
    tokens INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


async def _get_conn():
    global _conn, _disabled
    if _disabled:
        return None
    if _conn is not None:
        return _conn
    if not DATABASE_ENABLED:
        logger.info("SQLite 未启用（可选组件），持久化功能降级为内存/无")
        _disabled = True
        return None
    try:
        import aiosqlite
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = await aiosqlite.connect(str(DB_PATH))
        _conn.row_factory = aiosqlite.Row
        await _conn.execute("PRAGMA journal_mode=WAL")
        await _conn.execute("PRAGMA busy_timeout=20000")
        await _conn.executescript(_SCHEMA)
        logger.info("SQLite connected: %s", DB_PATH)
        return _conn
    except Exception as e:
        logger.warning("SQLite unavailable, persistence disabled: %s", e)
        _disabled = True
        return None


def _row_to_dict(row) -> Optional[dict]:
    return dict(row) if row is not None else None


async def ensure_schema() -> None:
    conn = await _get_conn()
    if conn:
        logger.info("Schema check done")


async def execute(query: str, *args) -> int:
    conn = await _get_conn()
    if conn is None:
        return 0
    try:
        cur = await conn.execute(_norm(query), args)
        await conn.commit()
        return cur.rowcount
    except Exception as e:
        logger.warning("DB execute failed: %s | %s", e, query)
        return 0


async def fetch(query: str, *args) -> list:
    conn = await _get_conn()
    if conn is None:
        return []
    try:
        cur = await conn.execute(_norm(query), args)
        rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows]
    except Exception as e:
        logger.warning("DB fetch failed: %s | %s", e, query)
        return []


async def fetchrow(query: str, *args) -> Optional[dict]:
    conn = await _get_conn()
    if conn is None:
        return None
    try:
        cur = await conn.execute(_norm(query), args)
        row = await cur.fetchone()
        return _row_to_dict(row)
    except Exception as e:
        logger.warning("DB fetchrow failed: %s | %s", e, query)
        return None


async def fetchval(query: str, *args) -> Any:
    conn = await _get_conn()
    if conn is None:
        return None
    try:
        cur = await conn.execute(_norm(query), args)
        row = await cur.fetchone()
        if row is None:
            return None
        return row[0]
    except Exception as e:
        logger.warning("DB fetchval failed: %s | %s", e, query)
        return None


def to_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


async def log_ai_call(task_type: str, input_text: str, output: Any,
                      model: str, tokens: int, latency_ms: int) -> None:
    """Fire-and-forget AI call logging into ai_logs."""
    try:
        await execute(
            "INSERT INTO ai_logs (task_type, input, output, model, tokens, latency_ms)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            task_type, (input_text or "")[:8000], to_json(output or {}),
            model or "", int(tokens or 0), int(latency_ms or 0),
        )
    except Exception as e:
        logger.warning("ai_logs insert failed: %s", e)
