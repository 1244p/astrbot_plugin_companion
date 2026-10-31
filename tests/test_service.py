import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db  # noqa: E402
from core.config import Config  # noqa: E402
from core.journal import Journal  # noqa: E402
from core.service import Service  # noqa: E402

FACTS_REPLY = json.dumps(
    {"facts": [{"slot": "饮食", "content": "不吃香菜", "kind": "preference",
                "polarity": -1, "importance": 4, "confidence": 0.9}]},
    ensure_ascii=False,
)
PERSONA_REPLY = json.dumps(
    {"deltas": {"warmth": 0.05}, "reason": "用户在诉苦"}, ensure_ascii=False
)


class FakeLLM:
    def __init__(self, facts_reply=FACTS_REPLY, persona_reply=PERSONA_REPLY, fail=False):
        self.facts_reply = facts_reply
        self.persona_reply = persona_reply
        self.fail = fail
        self.calls = []

    async def __call__(self, system: str, prompt: str) -> str:
        self.calls.append((system[:12], prompt[:40]))
        if self.fail:
            raise RuntimeError("model down")
        return self.persona_reply if "角色状态" in system else self.facts_reply


class ServiceTest(unittest.TestCase):
    def setUp(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.conn = db.connect(path)
        self.cfg = Config.load({"care_enable": True, "care_min_idle_minutes": 30})
        self.journal = Journal(tempfile.mkdtemp(), level="DEBUG")
        self.llm = FakeLLM()
        self.service = Service(self.conn, self.cfg, self.journal, self.llm)

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_buffer_push_and_len(self):
        self.service.push("s", "用户：你好")
        self.service.push("s", "  ")
        self.assertEqual(self.service.buffer_len("s"), 1)

    def test_buffer_capped(self):
        for index in range(self.cfg.max_buffer + 5):
            self.service.push("s", f"用户：{index}")
        self.assertEqual(self.service.buffer_len("s"), self.cfg.max_buffer)

    def test_flush_writes_facts_and_clears_buffer(self):
        self.service.push("s", "用户：香菜别放")
        result = self.run_async(self.service.flush("s", "turns"))
        self.assertEqual(result["written"], 1)
        self.assertEqual(self.service.buffer_len("s"), 0)
        facts = self.service.facts.list_facts("s")
        self.assertEqual(facts[0].slot, "饮食")

    def test_flush_dry_run_does_not_write(self):
        self.service.push("s", "用户：香菜别放")
        result = self.run_async(self.service.flush("s", "manual", dry_run=True))
        self.assertEqual(len(result["preview"]), 1)
        self.assertEqual(self.service.facts.list_facts("s"), [])
        self.assertEqual(self.service.buffer_len("s"), 1)

    def test_flush_handles_model_failure(self):
        service = Service(self.conn, self.cfg, self.journal, FakeLLM(fail=True))
        service.push("s", "用户：香菜别放")
        result = self.run_async(service.flush("s"))
        self.assertEqual(result["written"], 0)
        self.assertIn("error", result)
        self.assertEqual(service.buffer_len("s"), 1)

    def test_flush_without_json_reports_reason(self):
        service = Service(self.conn, self.cfg, self.journal, FakeLLM(facts_reply="我看不出来"))
        service.push("s", "用户：嗯")
        result = self.run_async(service.flush("s"))
        self.assertEqual(result["written"], 0)
        self.assertTrue(any("JSON" in d for d in result["dropped"]))

    def test_persona_evolve_uses_budget(self):
        self.service.push("s", "用户：今天很累")
        result = self.run_async(self.service.flush("s"))
        self.assertIn("persona", result)
        state = self.service.people.get("s")
        self.assertGreater(state["traits"]["warmth"], 0.5)
        self.assertLessEqual(state["day_used"], self.cfg.persona_daily_budget)

    def test_injection_contains_memory_and_state(self):
        self.service.push("s", "用户：香菜别放")
        self.run_async(self.service.flush("s"))
        block = self.run_async(self.service.injection("s", "今天吃火锅"))
        self.assertIn("<memory>", block)
        self.assertIn("不吃香菜", block)
        self.assertIn("<self_state>", block)

    def test_injection_empty_when_nothing_known(self):
        self.assertEqual(self.run_async(self.service.injection("s", "你好")), "")

    def test_backfill_sets_vectors(self):
        async def embed(text: str):
            return [1.0, 0.0, 0.0] if "香菜" in text else [0.0, 1.0, 0.0]

        service = Service(self.conn, self.cfg, self.journal, FakeLLM(), embed)
        service.push("s", "用户：香菜别放")
        self.run_async(service.flush("s"))
        done = self.run_async(service.backfill("s"))
        self.assertEqual(done, 1)
        fact = service.facts.list_facts("s")[0]
        self.assertTrue(fact.vector_model)
        block = self.run_async(service.injection("s", "香菜"))
        self.assertIn("不吃香菜", block)

    def test_fact_without_embedding_has_no_vector_flag(self):
        self.service.push("s", "用户：香菜别放")
        self.run_async(self.service.flush("s"))
        fact = self.service.facts.list_facts("s")[0]
        self.assertFalse(fact.vector)
        self.assertEqual(fact.vector_model, "")

    def test_explain_returns_scores(self):
        self.service.push("s", "用户：香菜别放")
        self.run_async(self.service.flush("s"))
        rows = self.service.explain("s", "香菜")
        self.assertEqual(rows[0]["slot"], "饮食")
        self.assertGreater(rows[0]["score"], 0)

    def test_known_scopes_survives_restart(self):
        self.service.push("s", "用户：香菜别放")
        self.run_async(self.service.flush("s"))
        fresh = Service(self.conn, self.cfg, self.journal, FakeLLM())
        self.assertIn("s", fresh.known_scopes())

    def test_care_prompt_mentions_memory(self):
        self.service.push("s", "用户：香菜别放")
        self.run_async(self.service.flush("s"))
        prompt = self.service.care_prompt("s")
        self.assertIn("不吃香菜", prompt)
        self.assertIn("JSON", prompt)

    def test_care_decide_respects_global_switch(self):
        cfg = Config.load({})
        service = Service(self.conn, cfg, self.journal, FakeLLM())
        (ok, reason), _ = service.care_decide("s")
        self.assertFalse(ok)
        self.assertEqual(reason, "disabled")


class SchemaTest(unittest.TestCase):
    def test_schema_loads_and_describes(self):
        from api import schema

        class Fake:
            cfg = Config.load({})

        data = schema.describe(Fake())
        self.assertTrue(data["groups"])
        keys = [f["key"] for group in data["groups"] for f in group["fields"]]
        self.assertIn("care_enable", keys)
        self.assertNotIn("baseline", keys)

    def test_every_group_has_description(self):
        from api import schema

        for gid, title, desc in schema.GROUPS:
            self.assertTrue(desc.strip(), gid)

    def test_group_of_unknown_key_falls_back(self):
        from api import schema

        self.assertEqual(schema.group_of("care_enable"), "care")
        self.assertEqual(schema.group_of("persona_max_chars"), "persona")
        self.assertEqual(schema.group_of("something_new"), "basic")


if __name__ == "__main__":
    unittest.main()
