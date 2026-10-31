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
    # ⚠️ 只能往末尾追加。迁移靠 PRAGMA user_version 当列表下标，
    # 插在中间会让「版本号已经越过那一位」的老库永远跑不到这条 SQL
    # （v0.1.10 第一次发版就是这么漏的：新建库正常，升级库缺 closed_at）。
    "ALTER TABLE facts ADD COLUMN closed_at REAL NOT NULL DEFAULT 0",
]


# 兜底：即使版本号已被写坏（例如上面那种插错位置的情况），
# 也按 PRAGMA table_info 把缺的列补上。SQL 里的列定义必须和 CREATE TABLE 一致。
EXPECTED_COLUMNS = {
    "facts": {"closed_at": "REAL NOT NULL DEFAULT 0"},
    "persona": {
        "last_evolve_at": "REAL NOT NULL DEFAULT 0",
        "day_key": "TEXT NOT NULL DEFAULT ''",
        "day_used": "REAL NOT NULL DEFAULT 0",
    },
    "care": {
        "check_at": "REAL NOT NULL DEFAULT 0",
        "check_reason": "TEXT NOT NULL DEFAULT ''",
        "next_gap": "INTEGER NOT NULL DEFAULT 0",
        "last_text": "TEXT NOT NULL DEFAULT ''",
    },
}


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
    _ensure_columns(conn)
    conn.commit()
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _ensure_columns(conn: sqlite3.Connection) -> list[str]:
    """把 EXPECTED_COLUMNS 里缺的列补上，返回补了哪些。"""
    added: list[str] = []
    for table, columns in EXPECTED_COLUMNS.items():
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            continue  # 表还没建（全新库走 SCHEMA 就够）
        have = {row[1] for row in rows}
        for name, ddl in columns.items():
            if name in have:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                added.append(f"{table}.{name}")
            except sqlite3.OperationalError:
                pass  # 并发下别人刚补过
    return added


def now() -> float:
    return time.time()
