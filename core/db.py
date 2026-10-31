"""SQLite 落地：连接、迁移、事务。

上一版把「建表 + 补列」写死在 `_init_db` 和 `_migrate` 两处，加一列要改两边。
这里只有一个 `MIGRATIONS` 列表：每项是一次性 SQL，`PRAGMA user_version` 记录进度。
"""

from __future__ import annotations

import os
import sqlite3
import time

SCHEMA = [
    # v1
    """
    CREATE TABLE IF NOT EXISTS facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL,
        slot TEXT NOT NULL,
        content TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'fact',
        importance INTEGER NOT NULL DEFAULT 3,
        confidence REAL NOT NULL DEFAULT 0.7,
        polarity INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        source TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        hits INTEGER NOT NULL DEFAULT 0,
        last_hit_at REAL NOT NULL DEFAULT 0,
        vector BLOB,
        vector_model TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_facts_scope ON facts(scope, status)",
    "CREATE INDEX IF NOT EXISTS idx_facts_slot ON facts(scope, slot)",
    # v2：任务被显式关闭的时间（0 表示还开着）。任务不再靠 superseded 表达完成。
    "ALTER TABLE facts ADD COLUMN closed_at REAL NOT NULL DEFAULT 0",
    """
    CREATE TABLE IF NOT EXISTS persona (
        scope TEXT PRIMARY KEY,
        traits TEXT NOT NULL DEFAULT '{}',
        mood TEXT NOT NULL DEFAULT '',
        version INTEGER NOT NULL DEFAULT 0,
        last_evolve_at REAL NOT NULL DEFAULT 0,
        day_key TEXT NOT NULL DEFAULT '',
        day_used REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL,
        version INTEGER NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        traits TEXT NOT NULL,
        deltas TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_persona_log_scope ON persona_log(scope, id)",
    """
    CREATE TABLE IF NOT EXISTS care (
        scope TEXT PRIMARY KEY,
        last_active REAL NOT NULL DEFAULT 0,
        last_sent REAL NOT NULL DEFAULT 0,
        sent_day TEXT NOT NULL DEFAULT '',
        sent_today INTEGER NOT NULL DEFAULT 0,
        unanswered INTEGER NOT NULL DEFAULT 0,
        next_gap INTEGER NOT NULL DEFAULT 0,
        last_text TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 1,
        check_at REAL NOT NULL DEFAULT 0,
        check_reason TEXT NOT NULL DEFAULT ''
    )
    """,
]


def connect(path: str) -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """幂等迁移，返回当前版本号。"""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    for index, sql in enumerate(SCHEMA[current:], start=current + 1):
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version={index}")
    conn.commit()
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def now() -> float:
    return time.time()
