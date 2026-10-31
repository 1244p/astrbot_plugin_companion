"""把 memory_evo 的库搬到 companion（只读旧的，写新的）。

两套库结构不同：旧版是 subject/predicate/object，新版是 slot/content。
这个脚本只做能一一对应的搬运，向量一律不搬——旧模型和新模型维度未必相同，
搬过来只会是错的余弦值，交给新版的后台回填重新算。

用法：
    python scripts/migrate_from_evo.py --old /path/memory_evo/memory.db \
        --new /path/astrbot_plugin_companion/memory.db            # 干跑，只报告
    ... --apply                                                  # 真写入
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

KINDS = {"preference", "profile", "fact", "event", "task"}
STATUS_MAP = {"active": "active", "suspect": "suspect", "superseded": "superseded"}


def connect_ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.DatabaseError:
        return set()


def read_old_facts(conn: sqlite3.Connection) -> list[dict]:
    if not table_exists(conn, "facts"):
        return []
    rows = conn.execute("SELECT * FROM facts").fetchall()
    out = []
    for row in rows:
        data = dict(row)
        slot = (data.get("predicate") or data.get("subject") or "其他").strip()[:20]
        content = (data.get("object") or "").strip().replace("\n", " ")
        if not content:
            continue
        kind = (data.get("kind") or "fact").lower()
        if kind not in KINDS:
            kind = "fact"
        out.append(
            {
                "scope": data.get("scope") or "",
                "slot": slot,
                "content": content[:300],
                "kind": kind,
                "importance": int(data.get("importance") or 3),
                "confidence": float(data.get("confidence") or 0.7),
                "polarity": int(data.get("polarity") or 0),
                "status": STATUS_MAP.get((data.get("status") or "active").lower(), "active"),
                "hits": int(data.get("hits") or 0),
                "created_at": float(data.get("created_at") or 0),
                "updated_at": float(data.get("updated_at") or 0),
            }
        )
    return out


def read_old_persona(conn: sqlite3.Connection) -> list[dict]:
    if not table_exists(conn, "persona"):
        return []
    out = []
    for row in conn.execute("SELECT * FROM persona").fetchall():
        data = dict(row)
        try:
            traits = json.loads(data.get("traits") or "{}")
        except ValueError:
            traits = {}
        if not isinstance(traits, dict) or not traits:
            continue
        out.append(
            {
                "scope": data.get("scope") or "",
                "traits": traits,
                "mood": (data.get("mood") or "")[:200],
                "version": int(data.get("version") or 0),
                "last_evolve_at": float(data.get("last_evolve_at") or 0),
                "day_key": data.get("day_key") or "",
                "day_used": float(data.get("day_used") or 0),
            }
        )
    return out


def read_old_care(conn: sqlite3.Connection) -> list[dict]:
    if not table_exists(conn, "proactive"):
        return []
    out = []
    for row in conn.execute("SELECT * FROM proactive").fetchall():
        data = dict(row)
        out.append(
            {
                "scope": data.get("scope") or "",
                "last_active": float(data.get("last_active") or 0),
                "last_sent": float(data.get("last_sent") or 0),
                "sent_day": data.get("sent_date") or "",
                "sent_today": int(data.get("sent_today") or 0),
                "unanswered": int(data.get("unanswered") or 0),
                "next_gap": int(data.get("next_gap") or 0),
                "last_text": (data.get("last_text") or "")[:200],
                "enabled": int(data.get("enabled") or 1),
            }
        )
    return out


def ensure_new_schema(conn: sqlite3.Connection) -> None:
    """新版第一次打开库时会建表；这里补一次，保证脚本可以独立跑。"""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core import db  # noqa: PLC0415

    db.migrate(conn)


def write_facts(conn: sqlite3.Connection, rows: list[dict]) -> tuple[int, int]:
    inserted = skipped = 0
    for row in rows:
        exists = conn.execute(
            "SELECT 1 FROM facts WHERE scope=? AND slot=? AND content=? AND status=? LIMIT 1",
            (row["scope"], row["slot"], row["content"], row["status"]),
        ).fetchone()
        if exists:
            skipped += 1
            continue
        conn.execute(
            """INSERT INTO facts (scope, slot, content, kind, importance, confidence,
                                  polarity, status, source, hits, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["scope"], row["slot"], row["content"], row["kind"], row["importance"],
                row["confidence"], row["polarity"], row["status"], "migrate", row["hits"],
                row["created_at"] or row["updated_at"], row["updated_at"],
            ),
        )
        inserted += 1
    conn.commit()
    return inserted, skipped


def write_persona(conn: sqlite3.Connection, rows: list[dict]) -> int:
    done = 0
    for row in rows:
        exists = conn.execute(
            "SELECT 1 FROM persona WHERE scope=?", (row["scope"],)
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """INSERT INTO persona (scope, traits, mood, version, last_evolve_at, day_key,
                                    day_used, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                row["scope"], json.dumps(row["traits"], ensure_ascii=False), row["mood"],
                row["version"], row["last_evolve_at"], row["day_key"], row["day_used"],
                0,
            ),
        )
        done += 1
    conn.commit()
    return done


def write_care(conn: sqlite3.Connection, rows: list[dict]) -> int:
    done = 0
    for row in rows:
        exists = conn.execute("SELECT 1 FROM care WHERE scope=?", (row["scope"],)).fetchone()
        if exists:
            continue
        conn.execute(
            """INSERT INTO care (scope, last_active, last_sent, sent_day, sent_today,
                                 unanswered, next_gap, last_text, enabled)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                row["scope"], row["last_active"], row["last_sent"], row["sent_day"],
                row["sent_today"], row["unanswered"], row["next_gap"], row["last_text"],
                row["enabled"],
            ),
        )
        done += 1
    conn.commit()
    return done


def report(old_path: str, new_path: str, apply: bool) -> int:
    if not os.path.exists(old_path):
        print(f"旧库不存在：{old_path}")
        return 2
    old = connect_ro(old_path)
    facts = read_old_facts(old)
    persona = read_old_persona(old)
    care = read_old_care(old)
    print(f"旧库：{old_path}")
    print(f"  事实 {len(facts)} 条（其中 superseded "
          f"{sum(1 for f in facts if f['status'] == 'superseded')} 条）")
    print(f"  人格 {len(persona)} 个会话")
    print(f"  主动状态 {len(care)} 个会话")
    if not apply:
        print("\n干跑完成。加 --apply 才会写进新库。")
        return 0

    conn = sqlite3.connect(new_path)
    conn.row_factory = sqlite3.Row
    ensure_new_schema(conn)
    inserted, skipped = write_facts(conn, facts)
    people = write_persona(conn, persona)
    cared = write_care(conn, care)
    conn.commit()
    print(f"\n写入新库：{new_path}")
    print(f"  事实 新增 {inserted}，已存在跳过 {skipped}")
    print(f"  人格 新增 {people}")
    print(f"  主动状态 新增 {cared}")
    print("向量没搬：旧模型和新模型维度不一定相同，交给新插件后台回填重算。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="memory_evo → companion 数据迁移")
    parser.add_argument("--old", required=True, help="旧库 memory.db 路径（只读打开）")
    parser.add_argument("--new", required=True, help="新库 memory.db 路径")
    parser.add_argument("--apply", action="store_true", help="真的写入，默认只干跑")
    args = parser.parse_args()
    return report(args.old, args.new, args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
