"""事实记忆：写入、去重、召回打分。

一条事实 = (slot, content)。`slot` 是归一化后的谓词（「饮食」「作息」），
同槽位新值会替换旧值，但旧行保留并标 `superseded`，这样「为什么现在这么说」
永远能追到来源。
"""

from __future__ import annotations

import json
import math
import sqlite3
import struct
from dataclasses import dataclass, field

from . import db
from .config import Config

HALF_LIFE_DAYS = {
    "preference": 30.0,
    "profile": 60.0,
    "fact": 14.0,
    "event": 7.0,
    "task": 1.0,
}
KINDS = tuple(HALF_LIFE_DAYS)

W_SIMILARITY = 0.5
W_RECENCY = 0.2
W_IMPORTANCE = 0.2
W_CONFIDENCE = 0.1


@dataclass
class Fact:
    scope: str
    slot: str
    content: str
    kind: str = "fact"
    importance: int = 3
    confidence: float = 0.7
    polarity: int = 0
    source: str = ""
    vector: list[float] | None = None
    vector_model: str = ""
    id: int = 0
    status: str = "active"
    state: str = "doing"
    closed_at: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0
    hits: int = 0
    last_hit_at: float = 0.0
    score: float = 0.0
    why: dict = field(default_factory=dict)


def pack_vector(vector: list[float] | None) -> bytes | None:
    if not vector:
        return None
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_vector(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob[: count * 4]))


def normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm <= 1e-9:
        return []
    return [v / norm for v in vector]


def overlap(a: str, b: str) -> float:
    """字面相似度：字符二元组 Jaccard，无向量时的兜底。"""
    a, b = a.strip(), b.strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ga = {a[i : i + 2] for i in range(len(a) - 1)} or {a}
    gb = {b[i : i + 2] for i in range(len(b) - 1)} or {b}
    return len(ga & gb) / len(ga | gb)


NEGATIONS = ("不爱", "不喜欢", "不吃", "不喝", "不去", "不想", "讨厌", "别", "不", "没", "无")
LIKES = ("爱吃", "爱喝", "喜欢", "偏好", "最爱")
PUNCT = " 	。，、；：！？…~～.,;:!?()（）[]【】\"'“”"


def _norm(text: str) -> str:
    return "".join(ch for ch in str(text) if ch not in PUNCT).lower()


def core_of(content: str) -> str:
    """剥掉「爱/不爱/喜欢/讨厌」这类极性词，剩下的才是被评价的对象。"""
    text = _norm(content)
    changed = True
    while changed:
        changed = False
        for word in LIKES + NEGATIONS:
            if text.startswith(word) and len(text) > len(word):
                text = text[len(word):]
                changed = True
    return text


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return max(0.0, min(1.0, sum(x * y for x, y in zip(left, right))))


def recency(updated_at: float, kind: str, at: float) -> float:
    half_life = HALF_LIFE_DAYS.get(kind, 14.0)
    age_days = max(0.0, (at - updated_at) / 86400.0)
    return 0.5 ** (age_days / half_life)


TASK_STATE_OF = {"done": "done", "closed": "done", "cancelled": "cancelled"}


class FactStore:
    def __init__(self, conn: sqlite3.Connection, cfg: Config):
        self.conn = conn
        self.cfg = cfg

    # ---------- 写入 ----------

    def active_slot(self, scope: str, slot: str) -> list[Fact]:
        rows = self.conn.execute(
            "SELECT * FROM facts WHERE scope=? AND slot=? AND status IN ('active','suspect')",
            (scope, slot),
        ).fetchall()
        return [self._row(r) for r in rows]

    TASK_STATUS_OF = {"done": "done", "cancelled": "cancelled"}
    TASK_MATCH = 0.55

    def _open_tasks(self, scope: str) -> list[Fact]:
        rows = self.conn.execute(
            "SELECT * FROM facts WHERE scope=? AND kind='task' AND status='active'"
            " ORDER BY updated_at DESC",
            (scope,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def _write_task(self, fact: Fact, stamp: float) -> tuple[Fact, str]:
        """任务是多值的：同槽位不等于互斥，只有「同一件事」才互相影响。

        - state=doing：内容几乎相同则算重复；否则新增，绝不覆盖别的任务
        - state=done/cancelled：在**所有未完成任务**里找最像的一条关掉；
          找不到就把它当成「一件已经结束、没登记过的事」直接落成已关闭
        """
        state = fact.state if fact.state in ("done", "cancelled") else "doing"
        opening = self._open_tasks(fact.scope)

        if state == "doing":
            for old in opening:
                if _norm(old.content) == _norm(fact.content) or overlap(old.content, fact.content) >= 0.9:
                    self.conn.execute(
                        "UPDATE facts SET hits=hits+1, last_hit_at=? WHERE id=?", (stamp, old.id)
                    )
                    self.conn.commit()
                    old.hits += 1
                    return old, "duplicate"
            status, closed_at, action = "active", 0.0, "create"
        else:
            best, best_score = None, 0.0
            for old in opening:
                score = overlap(old.content, fact.content)
                if score > best_score:
                    best, best_score = old, score
            if best is not None and best_score >= self.TASK_MATCH:
                status = self.TASK_STATUS_OF[state]
                self.conn.execute(
                    "UPDATE facts SET status=?, closed_at=?, updated_at=? WHERE id=?",
                    (status, stamp, stamp, best.id),
                )
                self.conn.commit()
                best.status = status
                best.closed_at = stamp
                return best, "closed"
            status, closed_at, action = self.TASK_STATUS_OF[state], stamp, "closed"

        cursor = self.conn.execute(
            """INSERT INTO facts
               (scope, slot, content, kind, importance, confidence, polarity, status,
                source, created_at, updated_at, closed_at, vector, vector_model)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fact.scope,
                fact.slot,
                fact.content,
                "task",
                max(1, min(5, fact.importance)),
                max(0.0, min(1.0, fact.confidence)),
                0 if fact.polarity == 0 else (1 if fact.polarity > 0 else -1),
                status,
                fact.source[:200],
                stamp,
                stamp,
                closed_at,
                pack_vector(normalize(fact.vector) if fact.vector else None),
                fact.vector_model[:60],
            ),
        )
        self.conn.commit()
        fact.id = int(cursor.lastrowid)
        fact.status = status
        fact.state = state
        fact.closed_at = closed_at
        fact.created_at = stamp
        fact.updated_at = stamp
        return fact, action

    def close(self, scope: str, ids: list[int], state: str = "done") -> int:
        """关闭或重开任务（面板「完成/重开」按钮、`/memory done`）。

        state=done/cancelled 只作用于还在办的任务；state=active 是「重开」，
        只作用于已关闭的任务——两个方向都幂等，点错不会把状态搅乱。
        """
        marks = ",".join("?" * len(ids))
        if state == "active":
            cursor = self.conn.execute(
                f"UPDATE facts SET status='active', closed_at=0, updated_at=? WHERE scope=?"
                f" AND kind='task' AND status IN ('done','cancelled') AND id IN ({marks})",
                [db.now(), scope, *ids],
            )
        else:
            status = self.TASK_STATUS_OF.get(state, "done")
            cursor = self.conn.execute(
                f"UPDATE facts SET status=?, closed_at=?, updated_at=? WHERE scope=?"
                f" AND kind='task' AND status='active' AND id IN ({marks})",
                [status, db.now(), db.now(), scope, *ids],
            )
        self.conn.commit()
        return cursor.rowcount

    def write(self, fact: Fact) -> tuple[Fact, str]:
        """返回 (落库后的事实, 动作)。动作 ∈ create/replace/suspect/duplicate。"""
        fact.content = fact.content.strip()[:300]
        if not fact.slot or not fact.content:
            raise ValueError("slot 和 content 不能为空")
        if fact.kind not in KINDS:
            fact.kind = "fact"
        stamp = db.now()
        if fact.kind == "task":
            return self._write_task(fact, stamp)
        existing = self.active_slot(fact.scope, fact.slot)

        for old in existing:
            if old.status == "active" and (
                _norm(old.content) == _norm(fact.content)
                or overlap(old.content, fact.content) >= 0.9
            ):
                self.conn.execute(
                    "UPDATE facts SET hits=hits+1, last_hit_at=? WHERE id=?", (stamp, old.id)
                )
                self.conn.commit()
                old.hits += 1
                return old, "duplicate"

        conflict = [
            old
            for old in existing
            if old.status == "active"
            and old.polarity
            and fact.polarity
            and old.polarity != fact.polarity
            and core_of(old.content) == core_of(fact.content)
        ]
        if conflict:
            status = "suspect"
            action = "suspect"
        else:
            status = "active"
            action = "replace" if existing else "create"
            for old in existing:
                if old.status == "active":
                    self.conn.execute(
                        "UPDATE facts SET status='superseded', updated_at=? WHERE id=?",
                        (stamp, old.id),
                    )

        cursor = self.conn.execute(
            """INSERT INTO facts
               (scope, slot, content, kind, importance, confidence, polarity, status,
                source, created_at, updated_at, vector, vector_model)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fact.scope,
                fact.slot,
                fact.content,
                fact.kind,
                max(1, min(5, fact.importance)),
                max(0.0, min(1.0, fact.confidence)),
                0 if fact.polarity == 0 else (1 if fact.polarity > 0 else -1),
                status,
                fact.source[:200],
                stamp,
                stamp,
                pack_vector(normalize(fact.vector) if fact.vector else None),
                fact.vector_model[:60],
            ),
        )
        self.conn.commit()
        fact.id = int(cursor.lastrowid)
        fact.status = status
        fact.created_at = stamp
        fact.updated_at = stamp
        return fact, action

    def set_vector(self, fact_id: int, vector: list[float], model: str) -> None:
        self.conn.execute(
            "UPDATE facts SET vector=?, vector_model=? WHERE id=?",
            (pack_vector(normalize(vector)), model[:60], fact_id),
        )
        self.conn.commit()

    # ---------- 查询 ----------

    def list_facts(
        self,
        scope: str,
        slot: str = "",
        kind: str = "",
        status: str = "",
        query: str = "",
        limit: int = 50,
    ) -> list[Fact]:
        sql = "SELECT * FROM facts WHERE scope=?"
        args: list = [scope]
        if slot:
            sql += " AND slot=?"
            args.append(slot)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if status == "live":
            # 「有效」= 没被取代的一切：在办、已完成、已取消、待确认。
            # 被取代的旧值只在选「全部」时出现，否则翻列表全是历史噪音。
            sql += " AND status!='superseded'"
        elif status:
            sql += " AND status=?"
            args.append(status)
        if query:
            sql += " AND (content LIKE ? OR slot LIKE ?)"
            args.extend([f"%{query}%", f"%{query}%"])
        sql += " ORDER BY status='active' DESC, importance DESC, updated_at DESC LIMIT ?"
        args.append(max(1, min(500, limit)))
        return [self._row(r) for r in self.conn.execute(sql, args).fetchall()]

    def known(self, scope: str, limit: int = 8) -> list[Fact]:
        rows = self.conn.execute(
            """SELECT * FROM facts WHERE scope=? AND status='active'
               ORDER BY importance DESC, updated_at DESC LIMIT ?""",
            (scope, limit),
        ).fetchall()
        return [self._row(r) for r in rows]

    def recall(self, scope: str, query: str, query_vector: list[float] | None) -> list[Fact]:
        cfg = self.cfg
        rows = self.conn.execute(
            "SELECT * FROM facts WHERE scope=? AND status IN ('active'%s)"
            % (",'suspect'" if cfg.include_suspect else ""),
            (scope,),
        ).fetchall()
        stamp = db.now()
        scored: list[Fact] = []
        for row in rows:
            fact = self._row(row)
            vector = unpack_vector(row["vector"])
            if query_vector and vector and row["vector_model"]:
                similarity = _cosine(query_vector, vector)
                mode = "vector"
            else:
                similarity = overlap(query, fact.content + " " + fact.slot)
                mode = "text"
            freshness = recency(fact.updated_at, fact.kind, stamp)
            score = (
                W_SIMILARITY * similarity
                + W_RECENCY * freshness
                + W_IMPORTANCE * (fact.importance / 5.0)
                + W_CONFIDENCE * fact.confidence
            )
            if fact.confidence < cfg.min_confidence and fact.importance < 5:
                continue
            fact.score = round(score, 4)
            fact.why = {
                "similarity": round(similarity, 3),
                "mode": mode,
                "recency": round(freshness, 3),
                "importance": fact.importance,
                "confidence": round(fact.confidence, 2),
            }
            scored.append(fact)

        scored.sort(key=lambda f: f.score, reverse=True)
        kept: list[Fact] = []
        per_slot: dict[str, int] = {}
        for fact in scored:
            if per_slot.get(fact.slot, 0) >= cfg.max_per_slot:
                continue
            per_slot[fact.slot] = per_slot.get(fact.slot, 0) + 1
            kept.append(fact)
            if len(kept) >= cfg.top_k:
                break

        if kept:
            self.conn.executemany(
                "UPDATE facts SET hits=hits+1, last_hit_at=? WHERE id=?",
                [(stamp, f.id) for f in kept],
            )
            self.conn.commit()
        return kept

    def orphan_vectors(self, scope: str, limit: int = 16) -> list[Fact]:
        rows = self.conn.execute(
            """SELECT * FROM facts WHERE scope=? AND status='active'
               AND (vector IS NULL OR vector_model='') LIMIT ?""",
            (scope, limit),
        ).fetchall()
        return [self._row(r) for r in rows]

    # ---------- 修改 ----------

    def delete(self, scope: str, ids: list[int]) -> int:
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        cursor = self.conn.execute(
            f"DELETE FROM facts WHERE scope=? AND id IN ({marks})", [scope, *ids]
        )
        self.conn.commit()
        return int(cursor.rowcount or 0)

    def confirm(self, scope: str, ids: list[int]) -> int:
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        stamp = db.now()
        cursor = self.conn.execute(
            f"""UPDATE facts SET status='active', confidence=1.0, updated_at=?
                WHERE scope=? AND status='suspect' AND id IN ({marks})""",
            [stamp, scope, *ids],
        )
        self.conn.commit()
        return int(cursor.rowcount or 0)

    def stats(self, scope: str) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) c FROM facts WHERE scope=? GROUP BY status", (scope,)
        ).fetchall()
        by_status = {r["status"]: r["c"] for r in rows}
        total = sum(by_status.values())
        kinds = {
            r["kind"]: r["c"]
            for r in self.conn.execute(
                "SELECT kind, COUNT(*) c FROM facts WHERE scope=? GROUP BY kind", (scope,)
            ).fetchall()
        }
        slots = [
            {"slot": r["slot"], "count": r["c"]}
            for r in self.conn.execute(
                """SELECT slot, COUNT(*) c FROM facts WHERE scope=? AND status='active'
                   GROUP BY slot ORDER BY c DESC LIMIT 8""",
                (scope,),
            ).fetchall()
        ]
        agg = self.conn.execute(
            """SELECT AVG(confidence) ac, SUM(hits=0) never,
                      SUM(vector IS NOT NULL AND vector_model!='') with_vec
               FROM facts WHERE scope=? AND status='active'""",
            (scope,),
        ).fetchone()
        return {
            "total": total,
            "active": by_status.get("active", 0),
            "suspect": by_status.get("suspect", 0),
            "superseded": by_status.get("superseded", 0),
            "by_kind": kinds,
            "slots": slots,
            "avg_confidence": round(agg["ac"] or 0.0, 3),
            "never_hit": int(agg["never"] or 0),
            "with_vector": int(agg["with_vec"] or 0),
        }

    def _row(self, row: sqlite3.Row) -> Fact:
        return Fact(
            id=row["id"],
            scope=row["scope"],
            slot=row["slot"],
            content=row["content"],
            kind=row["kind"],
            importance=row["importance"],
            confidence=row["confidence"],
            polarity=row["polarity"],
            source=row["source"],
            status=row["status"],
            state=TASK_STATE_OF.get(row["status"], "doing") if row["kind"] == "task" else "doing",
            closed_at=row["closed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            hits=row["hits"],
            last_hit_at=row["last_hit_at"],
            vector_model=row["vector_model"],
        )


def render_block(facts: list[Fact], max_chars: int) -> str:
    if not facts:
        return ""
    lines = []
    used = 0
    for fact in facts:
        text = fact.content.strip().replace("\n", " ")
        if len(text) > 80:
            text = text[:78] + "…"
        line = f"- [{fact.slot}] {text}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def decode_vectors(facts: list[Fact], conn: sqlite3.Connection) -> dict[int, list[float]]:
    marks = ",".join("?" for _ in facts)
    rows = conn.execute(
        f"SELECT id, vector FROM facts WHERE id IN ({marks})", [f.id for f in facts]
    ).fetchall()
    return {r["id"]: unpack_vector(r["vector"]) for r in rows}


def to_json(fact: Fact) -> str:
    return json.dumps({"slot": fact.slot, "content": fact.content}, ensure_ascii=False)
