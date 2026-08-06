# -*- coding: utf-8 -*-
"""Async SQLite helper for agent-pipeline (P1 单用户化：Postgres -> SQLite).

原 asyncpg 驱动在桌面版默认禁用 PG，功能全部降级。P1 改为用进程内
aiosqlite 文件库（DATA_DIR/goofish.db），零外部依赖、随 exe 同级落盘。

为尽量少改调用方，这里做一层 SQL 方言转换（_norm + _bind）：
  $1..$N        -> ?（_bind 按出现顺序重排/复制参数，支持乱序与重复引用）
  ::jsonb/::text-> 去掉类型强转
  ILIKE         -> LIKE（SQLite LIKE 对 ASCII 大小写不敏感）
  EXCLUDED      -> excluded（SQLite 关键字）
  COUNT(*) FILTER (WHERE x) AS y -> SUM(CASE WHEN x THEN 1 ELSE 0 END) AS y
  NOW() - INTERVAL 'N days'     -> datetime('now','-N days')
  CURRENT_DATE - INTERVAL 'N days' -> DATE('now','-N days')
  CURRENT_DATE  -> DATE('now')
其余（CURRENT_TIMESTAMP / DATE() / COALESCE / RETURNING / ON CONFLICT）SQLite 原生支持。
jsonb 列以 TEXT 存储，读取时按 _JSON_COLS 反序列化为 Python 对象。
"""
from __future__ import annotations
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("agent-pipeline.db")

# DATA_DIR 由 launcher 注入；缺失时回退到项目内固定子目录（与 launcher._data_dir 对齐）
DATA_DIR = Path(os.environ.get("DATA_DIR")
                or Path(__file__).resolve().parents[2] / "data" / "agent-pipeline")
DB_PATH = DATA_DIR / "goofish.db"

# 桌面端固定使用内嵌 SQLite 持久化（监控任务 / 商品库 / 调用统计），
# 不依赖任何外部数据库。原逻辑用 POSTGRES_ENABLED 当开关，但桌面配置里
# postgres 后端默认禁用 → POSTGRES_ENABLED=false → 把 SQLite 也一起关掉，
# 导致监控任务无法持久化（表现为"监控一直无反馈"）。
# 改为：SQLite 默认启用，仅当显式 SQLITE_DISABLED=1/true 时才降级。
DATABASE_ENABLED = os.environ.get("SQLITE_DISABLED", "false").lower() not in ("1", "true", "yes", "on")

# 这些列以 JSON 文本存储，读取时反序列化为 Python 对象
_JSON_COLS = {"exclude_keywords", "images", "ai_analysis", "risk_reason"}

_conn = None
_disabled = False


# ---------- SQL 方言转换（PG -> SQLite） ----------
_COUNT_FILTER_RE = re.compile(
    r"COUNT\(\*\)\s+FILTER\s*\(\s*WHERE\s+(.*?)\)\s*AS\s+(\w+)",
    re.IGNORECASE | re.DOTALL,
)


_PARAM_RE = re.compile(r"\$(\d+)")


def _norm(sql: str) -> str:
    """SQL 方言转换（不含参数绑定——$N 的处理在 _bind，因为要同步重排 args）。

    注意：$N -> ? 绝不能用简单 re.sub。PG 的 $N 是「编号引用」，可乱序
    （found_count=found_count+$2 WHERE task_id=$1）可重复（ILIKE $2 出现
    两次只传一份参数）；SQLite 的 ? 是「纯位置绑定」，第 i 个 ? 吃第 i 个
    参数。直接替换会导致：乱序时参数错绑（found_count 永不更新）、重复时
    占位符多于参数（bindings 数量不符直接报错，被吞后表现为"找不到任务"）。
    """
    s = sql
    s = re.sub(r"::\w+", "", s)                       # 去 ::jsonb / ::text
    s = re.sub(r"\bILIKE\b", "LIKE", s, flags=re.IGNORECASE)
    s = re.sub(r"\bEXCLUDED\b", "excluded", s, flags=re.IGNORECASE)
    s = re.sub(r"NOW\(\)\s*-\s*INTERVAL\s+'[^']*'", "datetime('now','-7 days')", s, flags=re.IGNORECASE)
    s = re.sub(r"CURRENT_DATE\s*-\s*INTERVAL\s+'[^']*'", "DATE('now','-6 days')", s, flags=re.IGNORECASE)
    s = re.sub(r"\bCURRENT_DATE\b", "DATE('now')", s, flags=re.IGNORECASE)
    s = _COUNT_FILTER_RE.sub(r"SUM(CASE WHEN \1 THEN 1 ELSE 0 END) AS \2", s)
    return s


def _bind(query: str, args: tuple) -> tuple:
    """把 PG 风格 $N 参数转成 SQLite 位置绑定，并按出现顺序重排/复制参数。

    返回 (标准化 SQL, 重排后的 args)。无 $N 时原样返回（原生 ? 查询不受影响）。
    编号超出 args 长度时保持原样——让 sqlite 报错并进日志，而不是静默错绑。
    """
    order = [int(n) for n in _PARAM_RE.findall(query)]
    sql = _norm(_PARAM_RE.sub("?", query)) if order else _norm(query)
    if order and args:
        if max(order) <= len(args):
            args = tuple(args[i - 1] for i in order)
        else:
            logger.warning("DB bind: param index %d exceeds %d args | %s",
                           max(order), len(args), query)
    return sql, args


# ---------- DDL（原 Docker 项目 init.sql 未移植，这里从查询反推手写） ----------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS monitor_tasks (
    task_id TEXT PRIMARY KEY,
    name TEXT,
    keyword TEXT,
    max_price REAL,
    min_price REAL,
    seller_type TEXT,
    exclude_keywords TEXT,
    interval_minutes INTEGER DEFAULT 30,
    notify_open_id TEXT,
    created_by TEXT,
    min_score INTEGER DEFAULT 60,
    status TEXT DEFAULT 'running',
    found_count INTEGER DEFAULT 0,
    last_run TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS seen_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    item_id TEXT,
    first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(task_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_seen_task ON seen_items(task_id);

CREATE TABLE IF NOT EXISTS blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id TEXT,
    seller_name TEXT,
    reason TEXT,
    source TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS goods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT UNIQUE,
    title TEXT,
    description TEXT,
    price REAL,
    market_price REAL,
    discount_rate REAL,
    images TEXT,
    url TEXT,
    category TEXT,
    seller_id TEXT,
    seller_name TEXT,
    ai_score INTEGER DEFAULT 0,
    ai_analysis TEXT,
    updated_time TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS risk_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goods_id INTEGER,
    risk_score REAL,
    risk_level TEXT,
    risk_reason TEXT,
    model_used TEXT
);

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
        logger.info("SQLite 未启用（可选组件），监控/统计持久化降级")
        _disabled = True
        return None
    try:
        import aiosqlite
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = await aiosqlite.connect(str(DB_PATH))
        _conn.row_factory = aiosqlite.Row
        await _conn.execute("PRAGMA journal_mode=WAL")
        await _conn.execute("PRAGMA busy_timeout=20000")
        await _conn.execute("PRAGMA synchronous=NORMAL")
        await _conn.executescript(_SCHEMA)
        logger.info("SQLite connected: %s", DB_PATH)
        return _conn
    except Exception as e:
        logger.warning("SQLite unavailable, persistence disabled: %s", e)
        _disabled = True
        return None


def _row_to_dict(row) -> Optional[dict]:
    if row is None:
        return None
    d = dict(row)
    for col in _JSON_COLS:
        if col in d and isinstance(d[col], str):
            try:
                d[col] = json.loads(d[col])
            except Exception:
                pass
    return d


def _serialize_param(v: Any) -> Any:
    """SQLite 无法绑定 list/dict（会抛 InterfaceError）。原 Docker 项目用
    asyncpg 的 ::jsonb 自动处理，桌面端 SQLite 的文本列需要显式 JSON 化。
    save_analysis_to_db 已对 images/ai_analysis/risk_reason 调 db.to_json，
    但 monitor.create_task 的 exclude_keywords 等裸 list 仍会触发绑定失败 →
    监控任务建库报「数据库不可用」。这里在写入/查询参数层统一兜底。"""
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return v


def _serialize_args(args: tuple) -> tuple:
    return tuple(_serialize_param(a) for a in args)


async def ensure_schema() -> None:
    """确保表存在（幂等）。由服务启动时调用；连接建立时也会自动建表。"""
    conn = await _get_conn()
    if conn:
        logger.info("Schema check done")


async def execute(query: str, *args) -> int:
    """执行写操作，返回受影响行数（失败返回 0）。"""
    conn = await _get_conn()
    if conn is None:
        return 0
    try:
        sql, bound = _bind(query, _serialize_args(args))
        cur = await conn.execute(sql, bound)
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
        sql, bound = _bind(query, _serialize_args(args))
        cur = await conn.execute(sql, bound)
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
        sql, bound = _bind(query, _serialize_args(args))
        cur = await conn.execute(sql, bound)
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
        sql, bound = _bind(query, _serialize_args(args))
        cur = await conn.execute(sql, bound)
        row = await cur.fetchone()
        if row is None:
            return None
        return row[0]
    except Exception as e:
        logger.warning("DB fetchval failed: %s | %s", e, query)
        return None


def to_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


async def close() -> None:
    """关停时收编 WAL 再关闭连接。

    WAL 模式下数据先写 goofish.db-wal，主库文件长期停在几 KB；不
    checkpoint 就退出的话：① 主库看起来「空的」（2026-08-06 实测：
    运行数小时主库 4KB、WAL 1.5MB）；② 进程被强杀时 WAL 恢复窗口
    更大。TRUNCATE 模式 checkpoint 后 WAL 归零、数据全部并入主库。
    """
    global _conn
    conn, _conn = _conn, None
    if conn is None:
        return
    try:
        await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        await conn.close()
        logger.info("SQLite closed (WAL checkpointed): %s", DB_PATH)
    except Exception as e:
        logger.warning("SQLite close failed: %s", e)
