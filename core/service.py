"""编排层：把抽取、落库、人格、召回串成一条线。

这一层不 import astrbot —— 所有外部能力（调模型、算向量、发消息）都以
回调注入，所以单测不需要起框架。
"""

from __future__ import annotations

import time
from typing import Awaitable, Callable

from . import care, extract, persona
from .config import Config
from .facts import Fact, FactStore, normalize_task_kind, render_block
from .journal import Journal
from .state import CareRepo, PersonaRepo, known_scopes

AskFn = Callable[[str, str], Awaitable[str]]
EmbedFn = Callable[[str], Awaitable[list[float]]]


class Service:
    def __init__(self, conn, cfg: Config, journal: Journal, ask: AskFn,
                 embed: EmbedFn | None = None):
        self.conn = conn
        self.cfg = cfg
        self.journal = journal
        self.ask = ask
        self.embed = embed
        normalize_task_kind(conn)  # 老库里 kind 记成 fact 的任务，就地纠正成 task
        self.facts = FactStore(conn, cfg)
        self.people = PersonaRepo(conn, cfg)
        self.care = CareRepo(conn, cfg)
        self.buffers: dict[str, list[str]] = {}

    # ---------- 缓冲 ----------

    def push(self, scope: str, text: str) -> int:
        text = (text or "").strip()
        if not text:
            return self.buffer_len(scope)
        buffer = self.buffers.setdefault(scope, [])
        buffer.append(text[:2000])
        if len(buffer) > self.cfg.max_buffer:
            del buffer[: len(buffer) - self.cfg.max_buffer]
        return len(buffer)

    def buffer_len(self, scope: str) -> int:
        return len(self.buffers.get(scope, []))

    def peek(self, scope: str) -> list[str]:
        return list(self.buffers.get(scope, []))

    def known_scopes(self) -> list[str]:
        return known_scopes(self.conn)

    # ---------- 抽取 ----------

    async def flush(self, scope: str, reason: str = "idle", dry_run: bool = False) -> dict:
        buffer = list(self.buffers.get(scope, []))
        result = {
            "reason": reason,
            "turns": len(buffer),
            "written": 0,
            "replaced": 0,
            "suspect": 0,
            "closed": 0,
            "dropped": [],
            "persona": {},
            "raw": "",
            "dry_run": dry_run,
        }
        if not buffer:
            self.journal.write("DEBUG", "flush.empty", reason=reason)
            return result

        conversation = "\n".join(buffer)[-6000:]
        known = [(f.slot, f.content) for f in self.facts.known(scope, 8)]
        prompt = extract.fact_prompt(conversation, known)
        started = time.time()
        try:
            reply = await self.ask(extract.FACT_SYSTEM, prompt)
        except Exception as error:  # noqa: BLE001 - 抽取失败不能拖垮对话
            self.journal.write("ERROR", "llm.fail", f"抽取失败：{error}", stage="facts")
            result["error"] = str(error)
            return result
        result["raw"] = (reply or "")[:600]
        result["ms"] = int((time.time() - started) * 1000)

        raw = extract.extract_json(reply)
        if raw is None:
            self.journal.write("WARN", "llm.nojson", "抽取返回里没有 JSON",
                               head=(reply or "")[:160])
        items, dropped = extract.sanitize_facts(raw)
        result["dropped"] = dropped

        if dry_run:
            result["preview"] = items
            return result

        for item in items:
            fact = Fact(
                scope=scope,
                slot=item["slot"],
                content=item["content"],
                kind=item["kind"],
                importance=item["importance"],
                confidence=item["confidence"],
                polarity=item["polarity"],
                state=item.get("state", "doing"),
                source=reason,
            )
            try:
                fact, action = self.facts.write(fact)
            except ValueError:
                continue
            if action == "duplicate":
                continue
            result["written"] += 1
            if action == "replace":
                result["replaced"] += 1
            if action == "suspect":
                result["suspect"] += 1
            if action == "closed":
                result["closed"] = result.get("closed", 0) + 1
            self.journal.write("INFO", "fact.write", fact.content[:60],
                               slot=fact.slot, action=action, id=fact.id)

        if not items:
            self.journal.write("INFO", "flush.nofacts", "这轮没抽出可存内容",
                               dropped=";".join(dropped)[:200], known=len(known))

        if self.cfg.persona_evolve:
            result["persona"] = await self._evolve(scope, conversation, reason)

        self.buffers[scope] = []
        self.journal.write(
            "INFO", "flush.ok",
            f"写入 {result['written']} 条",
            reason=reason, written=result["written"], dropped=len(dropped),
            turns=len(buffer), ms=result["ms"],
        )
        return result

    async def _evolve(self, scope: str, conversation: str, reason: str) -> dict:
        started = time.time()
        try:
            reply = await self.ask(extract.PERSONA_SYSTEM, extract.persona_prompt(conversation))
        except Exception as error:  # noqa: BLE001
            self.journal.write("ERROR", "llm.fail", f"人格评估失败：{error}", stage="persona")
            return {}
        deltas, why = extract.sanitize_persona(extract.extract_json(reply))
        if not deltas:
            self.journal.write("DEBUG", "persona.empty", "没有明显情绪信号")
            return {}
        state = self.people.get(scope)
        now = care.local_now(self.cfg.care_timezone)
        today = care.day_key(now)
        used = state["day_used"] if state["day_key"] == today else 0.0
        hours = 24.0
        if state["last_evolve_at"]:
            hours = min(24.0, max(0.0, (now.timestamp() - state["last_evolve_at"]) / 3600))
        traits, spent, applied = persona.apply_deltas(
            state["traits"], deltas, self.cfg, self.cfg.baseline,
            hours=hours, daily_used=used,
        )
        if spent <= 0:
            self.journal.write("DEBUG", "persona.budget", "今日预算用完，只回归不前进")
            return {}
        # 记实际位移，不记模型要的位移：时间线上的胶囊必须能和 traits 对上
        saved = self.people.save(
            scope, traits, applied, why or reason, today, round(used + spent, 4)
        )
        self.journal.write(
            "INFO", "persona.evolve", why or reason,
            spent=round(spent, 4), used=round(used + spent, 4),
            ms=int((time.time() - started) * 1000), **{k: round(v, 3) for k, v in traits.items()},
        )
        return {"traits": traits, "spent": round(spent, 4), "version": saved["version"]}

    # ---------- 召回 ----------

    async def injection(self, scope: str, user_text: str) -> str:
        vector: list[float] = []
        if self.cfg.semantic_recall and self.embed:
            try:
                vector = await self.embed(user_text[:500])
            except Exception as error:  # noqa: BLE001
                self.journal.write("WARN", "embed.fail", f"向量失败：{error}")
                vector = []
        facts = self.facts.recall(scope, user_text, vector or None)
        memory = render_block(facts, self.cfg.max_inject_chars)
        blocks = []
        if memory:
            blocks.append("<memory>\n" + memory + "\n</memory>")
        if self.cfg.persona_inject:
            state = self.people.get(scope)
            tone = persona.render_block(state["traits"], self.cfg.baseline,
                                        self.cfg.persona_max_chars)
            if tone:
                blocks.append("<self_state>\n" + tone + "\n</self_state>")
        if facts:
            self.journal.write("DEBUG", "recall.hit", f"注入 {len(facts)} 条",
                               ids=",".join(str(f.id) for f in facts))
        return "\n".join(blocks)

    def explain(self, scope: str, user_text: str) -> list[dict]:
        facts = self.facts.recall(scope, user_text, None)
        return [
            {
                "id": f.id,
                "slot": f.slot,
                "content": f.content,
                "score": f.score,
                "why": f.why,
            }
            for f in facts
        ]

    # ---------- 向量回填 ----------

    async def backfill(self, scope: str, limit: int = 16) -> int:
        if not self.embed:
            return 0
        model = self._embed_model()
        done = 0
        for fact in self.facts.orphan_vectors(scope, limit):
            try:
                vector = await self.embed(fact.content)
            except Exception as error:  # noqa: BLE001
                self.journal.write("WARN", "embed.fail", f"回填失败：{error}")
                break
            if vector:
                self.facts.set_vector(fact.id, vector, model)
                done += 1
        if done:
            self.journal.write("DEBUG", "vector.backfill", f"补齐 {done} 条", scope=scope)
        return done

    def _embed_model(self) -> str:
        return self.cfg.embedding_provider_id or "default"

    # ---------- 主动消息 ----------

    def care_state(self, scope: str) -> dict:
        return self.care.get(scope)

    def care_decide(self, scope: str, force: bool = False):
        state = self.care.get(scope)
        now = care.local_now(self.cfg.care_timezone)
        return care.decide(now, state, self.cfg.care_block(), force=force), now

    def care_prompt(self, scope: str) -> str:
        state = self.people.get(scope)
        facts = self.facts.recall(scope, "", None) or self.facts.known(scope, 6)
        memory = render_block(facts, self.cfg.max_inject_chars)
        tone = persona.render_block(state["traits"], self.cfg.baseline, 120)
        return (
            "你要主动给对方发一条消息。\n"
            f"你们的关系记录：\n{memory or '（还没什么记录）'}\n"
            f"你此刻的说话倾向：{tone or '平常'}\n"
            f"要求：一句到两句，不超过 {self.cfg.care_max_chars} 字，像人随口说的，"
            "不要提问式寒暄，不要复述上面的记录。\n"
            '输出 JSON：{"send": true, "text": "要说的话", "gap": 下次间隔分钟数(15-240)}'
        )
