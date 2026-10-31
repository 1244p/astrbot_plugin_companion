"""人格与主动消息的持久状态。"""

from __future__ import annotations

import json
import sqlite3

from . import db
from .config import Config, TRAIT_KEYS
from .persona import clamp, parse_traits


class PersonaRepo:
    def __init__(self, conn: sqlite3.Connection, cfg: Config):
        self.conn = conn
        self.cfg = cfg

    def get(self, scope: str) -> dict:
        row = self.conn.execute("SELECT * FROM persona WHERE scope=?", (scope,)).fetchone()
        baseline = dict(self.cfg.baseline)
        if row is None:
            return {
                "scope": scope,
                "traits": dict(baseline),
                "mood": "",
                "version": 0,
                "last_evolve_at": 0.0,
                "day_key": "",
                "day_used": 0.0,
            }
        traits = dict(baseline)
        traits.update(parse_traits(row["traits"]))
        return {
            "scope": scope,
            "traits": {k: clamp(float(traits.get(k, baseline[k]))) for k in TRAIT_KEYS},
            "mood": row["mood"],
            "version": row["version"],
            "last_evolve_at": row["last_evolve_at"],
            "day_key": row["day_key"],
            "day_used": row["day_used"],
        }

    def save(self, scope: str, traits: dict, deltas: dict, reason: str,
             day_key: str, day_used: float, mood: str = "") -> dict:
        state = self.get(scope)
        version = int(state["version"]) + 1
        stamp = db.now()
        self.conn.execute(
            """INSERT INTO persona (scope, traits, mood, version, last_evolve_at, day_key,
                                    day_used, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(scope) DO UPDATE SET
                 traits=excluded.traits, mood=excluded.mood, version=excluded.version,
                 last_evolve_at=excluded.last_evolve_at, day_key=excluded.day_key,
                 day_used=excluded.day_used, updated_at=excluded.updated_at""",
            (
                scope,
                json.dumps(traits, ensure_ascii=False),
                mood[:200],
                version,
                stamp,
                day_key,
                round(float(day_used), 4),
                stamp,
            ),
        )
        self.conn.execute(
            """INSERT INTO persona_log (scope, version, reason, traits, deltas, created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                scope,
                version,
                reason[:200],
                json.dumps(traits, ensure_ascii=False),
                json.dumps(deltas, ensure_ascii=False),
                stamp,
            ),
        )
        self.conn.commit()
        return {"version": version, "traits": traits}

    def history(self, scope: str, limit: int = 12) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM persona_log WHERE scope=? ORDER BY id DESC LIMIT ?", (scope, limit)
        ).fetchall()
        return [
            {
                "version": r["version"],
                "reason": r["reason"],
                "traits": parse_traits(r["traits"]),
                "deltas": json.loads(r["deltas"] or "{}"),
                "created_at": r["created_at"],
            }
            for r in rows
        ]


class CareRepo:
    def __init__(self, conn: sqlite3.Connection, cfg: Config):
        self.conn = conn
        self.cfg = cfg

    def get(self, scope: str) -> dict:
        row = self.conn.execute("SELECT * FROM care WHERE scope=?", (scope,)).fetchone()
        if row is None:
            return {
                "scope": scope,
                "last_active": 0.0,
                "last_sent": 0.0,
                "sent_day": "",
                "sent_today": 0,
                "unanswered": 0,
                "next_gap": 0,
                "last_text": "",
                "enabled": 1,
                "check_at": 0.0,
                "check_reason": "",
            }
        return dict(row)

    def _ensure(self, scope: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO care (scope) VALUES (?)", (scope,))

    def touch_active(self, scope: str, stamp: float) -> None:
        self._ensure(scope)
        self.conn.execute(
            "UPDATE care SET last_active=?, unanswered=0 WHERE scope=?", (stamp, scope)
        )
        self.conn.commit()

    def mark_sent(self, scope: str, stamp: float, text: str, gap: int, day: str) -> None:
        self._ensure(scope)
        self.conn.execute(
            """UPDATE care SET last_sent=?, last_text=?, next_gap=?, sent_day=?,
                              sent_today=CASE WHEN sent_day=? THEN sent_today+1 ELSE 1 END,
                              unanswered=unanswered+1, check_at=?, check_reason='ready'
               WHERE scope=?""",
            (stamp, text[:200], gap, day, day, stamp, scope),
        )
        self.conn.commit()

    def mark_skipped(self, scope: str, stamp: float, reason: str) -> None:
        self._ensure(scope)
        self.conn.execute(
            "UPDATE care SET check_at=?, check_reason=? WHERE scope=?", (stamp, reason, scope)
        )
        self.conn.commit()

    def set_enabled(self, scope: str, enabled: bool) -> None:
        self._ensure(scope)
        self.conn.execute(
            "UPDATE care SET enabled=? WHERE scope=?", (1 if enabled else 0, scope)
        )
        self.conn.commit()

    def reset_day(self, scope: str, day: str) -> None:
        self.conn.execute(
            "UPDATE care SET sent_day=?, sent_today=0 WHERE scope=?", (day, scope)
        )
        self.conn.commit()

    def scopes(self) -> list[str]:
        rows = self.conn.execute("SELECT scope FROM care").fetchall()
        return [r["scope"] for r in rows]


def known_scopes(conn: sqlite3.Connection) -> list[str]:
    """facts ∪ persona ∪ care：重启后不用等用户先说话才恢复状态。"""
    rows = conn.execute(
        """SELECT scope FROM facts UNION SELECT scope FROM persona UNION SELECT scope FROM care"""
    ).fetchall()
    return [r["scope"] for r in rows]
