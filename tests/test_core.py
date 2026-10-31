import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import care, db, extract, facts, persona  # noqa: E402
from core.config import Config  # noqa: E402
from core.journal import Journal  # noqa: E402


def temp_db() -> db.sqlite3.Connection:
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    conn = db.connect(path)
    return conn


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = Config.load({})
        self.assertEqual(cfg.top_k, 6)
        self.assertEqual(cfg.max_step, 0.03)
        self.assertEqual(cfg.persona_daily_budget, 0.15)
        self.assertFalse(cfg.care_enable)

    def test_clamps_out_of_range(self):
        cfg = Config.load({"top_k": 999, "max_step": 9, "care_probability": -2, "flush_turns": 0})
        self.assertEqual(cfg.top_k, 20)
        self.assertEqual(cfg.max_step, 0.2)
        self.assertEqual(cfg.care_probability, 0.0)
        self.assertEqual(cfg.flush_turns, 1)

    def test_bad_types_fall_back(self):
        cfg = Config.load({"idle_seconds": "abc", "care_enable": "maybe", "max_step": "NaN"})
        self.assertEqual(cfg.idle_seconds, 90)
        self.assertFalse(cfg.care_enable)
        self.assertEqual(cfg.max_step, 0.03)

    def test_baseline_merged(self):
        cfg = Config.load({"baseline": {"intimacy": 0.7, "unknown": 1}})
        self.assertEqual(cfg.baseline["intimacy"], 0.7)
        self.assertEqual(cfg.baseline["warmth"], 0.5)
        self.assertNotIn("unknown", cfg.baseline)


class MigrateTest(unittest.TestCase):
    def test_idempotent(self):
        conn = temp_db()
        version = db.migrate(conn)
        self.assertEqual(db.migrate(conn), version)
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        self.assertTrue({"facts", "persona", "persona_log", "care"} <= tables)


class FactTest(unittest.TestCase):
    def setUp(self):
        self.conn = temp_db()
        self.cfg = Config.load({})
        self.store = facts.FactStore(self.conn, self.cfg)

    def write(self, slot, content, **kwargs):
        fact = facts.Fact(scope="s", slot=slot, content=content, **kwargs)
        return self.store.write(fact)

    def test_create_then_replace_keeps_history(self):
        first, action = self.write("饮食", "不吃香菜", polarity=-1)
        self.assertEqual(action, "create")
        second, action = self.write("饮食", "现在吃香菜了", polarity=1)
        self.assertEqual(action, "replace")
        self.assertNotEqual(first.id, second.id)
        old = self.conn.execute("SELECT status FROM facts WHERE id=?", (first.id,)).fetchone()
        self.assertEqual(old["status"], "superseded")

    def test_duplicate_does_not_add_row(self):
        self.write("饮食", "不吃香菜")
        fact, action = self.write("饮食", "不吃香菜。")
        self.assertEqual(action, "duplicate")
        count = self.conn.execute("SELECT COUNT(*) c FROM facts").fetchone()["c"]
        self.assertEqual(count, 1)

    def test_opposite_polarity_marked_suspect(self):
        self.write("饮食", "爱吃香菜", polarity=1)
        fact, action = self.write("饮食", "不吃香菜", polarity=-1)
        self.assertEqual(action, "suspect")
        self.assertEqual(fact.status, "suspect")

    def test_neutral_polarity_does_not_conflict(self):
        self.write("饮食", "爱吃香菜", polarity=1)
        _, action = self.write("饮食", "香菜当配菜", polarity=0)
        self.assertEqual(action, "replace")

    def test_recall_orders_by_score_and_caps_slot(self):
        self.write("饮食", "不吃香菜", importance=5, confidence=0.9)
        self.write("饮食", "喜欢喝冰美式", importance=3)
        self.write("工作", "在改 astrbot 的 bug", importance=4)
        rows = self.store.recall("s", "冰美式", None)
        self.assertTrue(rows)
        self.assertEqual(rows[0].slot, "饮食")
        self.assertEqual(len({r.slot for r in rows}), len(rows))
        self.assertEqual(rows[0].why["mode"], "text")

    def test_confidence_gate_skips_low_items(self):
        self.write("事实", "可能是程序员", confidence=0.2, importance=2)
        self.assertEqual(self.store.recall("s", "程序员", None), [])

    def test_vector_path_used_when_present(self):
        fact, _ = self.write("饮食", "不吃香菜")
        self.store.set_vector(fact.id, [1.0, 0.0], "embed-1")
        rows = self.store.recall("s", "无关的话", [1.0, 0.0])
        self.assertEqual(rows[0].why["mode"], "vector")
        self.assertAlmostEqual(rows[0].why["similarity"], 1.0, places=3)

    def test_stats_and_delete(self):
        fact, _ = self.write("饮食", "不吃香菜")
        self.assertEqual(self.store.stats("s")["total"], 1)
        self.assertEqual(self.store.delete("s", [fact.id]), 1)
        self.assertEqual(self.store.delete("other", [fact.id]), 0)

    def test_confirm_only_touches_suspect(self):
        self.write("饮食", "爱吃香菜", polarity=1)
        suspect, _ = self.write("饮食", "不吃香菜", polarity=-1)
        self.assertEqual(self.store.confirm("s", [suspect.id]), 1)
        self.assertEqual(self.store.confirm("s", [suspect.id]), 0)


class PersonaTest(unittest.TestCase):
    def test_move_step_takes_smallest_cap(self):
        self.assertAlmostEqual(persona.move_step(0.03, hours=24), 0.03)
        self.assertAlmostEqual(persona.move_step(0.03, hours=0.1), 0.000125)
        self.assertAlmostEqual(persona.move_step(0.03, hours=24, daily_used=0.14), 0.01)
        self.assertAlmostEqual(persona.move_step(0.03, hours=24, daily_used=0.15), 0.0)

    def test_half_life_relax_factor(self):
        self.assertAlmostEqual(persona.relax_factor(7 * 24, 7), 0.5)
        self.assertAlmostEqual(persona.relax_factor(0, 7), 0.0)
        self.assertGreater(persona.relax_factor(24, 7), 0.09)

    def test_apply_deltas_reports_spend(self):
        cfg = Config.load({})
        traits = {k: 0.5 for k in cfg.baseline}
        new, spent, applied = persona.apply_deltas(
            traits, {"warmth": 0.06, "intimacy": 0.06}, cfg, cfg.baseline, hours=24
        )
        # 两维都要 +0.03（单次上限），合计 0.06 会超过单次额度（预算的 1/3 = 0.05），
        # 所以等比缩到各 +0.025。
        self.assertAlmostEqual(applied["warmth"], 0.025, places=3)
        self.assertAlmostEqual(new["warmth"], 0.525, places=3)
        self.assertAlmostEqual(spent, 0.05, places=3)

    def test_frequent_flushes_barely_move(self):
        cfg = Config.load({})
        traits = {k: 0.5 for k in cfg.baseline}
        for _ in range(20):
            traits, _, _ = persona.apply_deltas(
                traits, {"warmth": 0.06}, cfg, cfg.baseline, hours=1 / 60
            )
        self.assertLess(traits["warmth"] - 0.5, 0.01)

    def test_daily_budget_caps_total(self):
        cfg = Config.load({})
        traits = {k: 0.5 for k in cfg.baseline}
        used = 0.0
        for _ in range(50):
            traits, spent, _ = persona.apply_deltas(
                traits, {"warmth": 0.06}, cfg, cfg.baseline, hours=24, daily_used=used
            )
            used += spent
        self.assertLessEqual(used, cfg.persona_daily_budget + 1e-6)


    def test_budget_is_shared_across_traits(self):
        """五个维度一起推，一天总计也不能超过预算（旧版每维各花一份剩余额度）。"""
        cfg = Config.load({})
        traits = {k: 0.5 for k in cfg.baseline}
        used = 0.0
        for _ in range(30):
            traits, spent, _ = persona.apply_deltas(
                traits, {k: 0.2 for k in cfg.baseline}, cfg, cfg.baseline,
                hours=24, daily_used=used,
            )
            used += spent
        self.assertLessEqual(used, cfg.persona_daily_budget + 1e-6)
        total_move = sum(traits[k] - cfg.baseline[k] for k in cfg.baseline)
        self.assertLessEqual(total_move, cfg.persona_daily_budget + 1e-3)

    def test_single_evolve_spends_at_most_third_of_budget(self):
        cfg = Config.load({})
        traits = {k: 0.5 for k in cfg.baseline}
        _, spent, _ = persona.apply_deltas(
            traits, {k: 0.2 for k in cfg.baseline}, cfg, cfg.baseline, hours=24
        )
        self.assertLessEqual(spent, cfg.persona_daily_budget / 3 + 1e-6)

    def test_applied_deltas_match_trait_movement(self):
        cfg = Config.load({})
        traits = {k: 0.5 for k in cfg.baseline}
        new, spent, applied = persona.apply_deltas(
            traits, {"warmth": 0.06}, cfg, cfg.baseline, hours=1 / 60
        )
        self.assertAlmostEqual(new["warmth"] - traits["warmth"], applied["warmth"], places=4)
        self.assertAlmostEqual(abs(applied["warmth"]), spent, places=4)

    def test_render_block_skips_small_deviation(self):
        baseline = {k: 0.5 for k in ("intimacy", "playfulness", "directness", "initiative", "warmth")}
        self.assertEqual(persona.render_block(dict(baseline), baseline, 80), "")
        loud = dict(baseline)
        loud["warmth"] = 0.9
        self.assertIn("温度", persona.render_block(loud, baseline, 80))


class CareTest(unittest.TestCase):
    cfg = Config.load({"care_enable": True, "care_min_idle_minutes": 60}).care_block()

    def state(self, **kwargs):
        base = {"enabled": 1, "last_active": 0, "last_sent": 0, "sent_day": "",
                "sent_today": 0, "unanswered": 0, "next_gap": 0}
        base.update(kwargs)
        return base

    def now(self, hour=15, minute=0):
        from datetime import datetime

        return datetime(2026, 10, 31, hour, minute)

    def test_disabled(self):
        cfg = dict(self.cfg, enabled=False)
        self.assertEqual(care.decide(self.now(), self.state(), cfg), (False, "disabled"))

    def test_muted_session(self):
        self.assertEqual(care.decide(self.now(), self.state(enabled=0), self.cfg),
                         (False, "muted"))

    def test_quiet_hours_across_midnight(self):
        self.assertEqual(care.decide(self.now(1, 0), self.state(), self.cfg),
                         (False, "quiet_hours"))
        self.assertTrue(care.in_quiet_hours(self.now(23, 45), "23:30", "08:00"))
        self.assertFalse(care.in_quiet_hours(self.now(12, 0), "23:30", "08:00"))

    def test_schedule_whitelist(self):
        cfg = dict(self.cfg, schedule="09:00-12:00,19:00-23:00")
        self.assertEqual(care.decide(self.now(15, 0), self.state(), cfg),
                         (False, "outside_schedule"))
        self.assertTrue(care.in_schedule(self.now(10, 0), care.parse_schedule("09:00-12:00")))
        self.assertTrue(care.in_schedule(self.now(1, 0), care.parse_schedule("22:00-02:00")))
        self.assertTrue(care.in_schedule(self.now(3, 0), care.parse_schedule("")))

    def test_gap_does_not_inflate_idle_requirement(self):
        from datetime import datetime

        now = datetime(2026, 10, 31, 15, 0)
        state = {"enabled": 1, "last_active": now.timestamp() - 77 * 60,
                 "last_sent": now.timestamp() - 132 * 60, "next_gap": 120,
                 "sent_day": "", "sent_today": 0, "unanswered": 0}
        ok, reason = care.decide(now, state, dict(self.cfg, probability=1.0))
        self.assertTrue(ok, reason)

    def test_never_sent_ignores_gap(self):
        from datetime import datetime

        now = datetime(2026, 10, 31, 15, 0)
        state = {"enabled": 1, "last_active": now.timestamp() - 200 * 60,
                 "last_sent": 0, "next_gap": 240, "sent_day": "", "sent_today": 0,
                 "unanswered": 0}
        self.assertTrue(care.decide(now, state, dict(self.cfg, probability=1.0))[0])

    def test_daily_cap_and_unanswered(self):
        from datetime import datetime

        now = datetime(2026, 10, 31, 15, 0)
        state = {"enabled": 1, "last_active": now.timestamp() - 500 * 60,
                 "last_sent": now.timestamp() - 400 * 60, "next_gap": 0,
                 "sent_day": "2026-10-31", "sent_today": 3, "unanswered": 0}
        self.assertEqual(care.decide(now, state, dict(self.cfg, probability=1.0)),
                         (False, "daily_cap"))
        state["unanswered"] = 2
        self.assertEqual(care.decide(now, state, dict(self.cfg, probability=1.0)),
                         (False, "unanswered_cap"))

    def test_force_still_respects_quiet_and_schedule(self):
        state = self.state()
        self.assertTrue(care.decide(self.now(15), state, self.cfg, force=True)[0])
        self.assertEqual(care.decide(self.now(1), state, self.cfg, force=True),
                         (False, "quiet_hours"))

    def test_split_sentences_by_blank_line(self):
        text = "又举牌了 你这牌子是常备的吧" + chr(10) * 2 + "行了 说明好了"
        self.assertEqual(len(care.split_sentences(text, 2)), 2)
        self.assertEqual(len(care.split_sentences("甲。乙。丙。", 2)), 2)
        self.assertEqual(care.split_sentences("只有一句", 2), ["只有一句"])

    def test_sanitize_reply(self):
        self.assertEqual(care.sanitize_reply({"send": False, "text": "x"}), None)
        self.assertEqual(care.sanitize_reply({"text": ""}), None)
        text, gap = care.sanitize_reply({"text": "在忙吗", "gap": 999})
        self.assertEqual(text, "在忙吗")
        self.assertEqual(gap, 240)

    def test_next_at_picks_earlier_gate(self):
        from datetime import datetime

        now = datetime(2026, 10, 31, 15, 0)
        state = {"last_sent": now.timestamp(), "next_gap": 120,
                 "last_active": now.timestamp() - 600}
        stamp, basis = care.next_at(state, self.cfg)
        self.assertEqual(basis, "gap")
        self.assertAlmostEqual(stamp, now.timestamp() + 120 * 60, delta=1)


class ExtractTest(unittest.TestCase):
    def test_json_from_fence(self):
        raw = '好的```json\n{"facts": []}\n```'
        self.assertEqual(extract.extract_json(raw), {"facts": []})

    def test_json_with_noise(self):
        self.assertEqual(extract.extract_json('先说明… {"a": {"b": 1}} 结束'), {"a": {"b": 1}})

    def test_json_invalid(self):
        self.assertIsNone(extract.extract_json("没有 json"))

    def test_sanitize_reports_reasons(self):
        items, dropped = extract.sanitize_facts({"facts": [{"slot": "", "content": "x"},
                                                          {"slot": "饮食", "content": "不吃香菜"}]})
        self.assertEqual(len(items), 1)
        self.assertTrue(any("缺 slot" in d for d in dropped))

    def test_sanitize_empty_and_wrong_shape(self):
        self.assertEqual(extract.sanitize_facts({"facts": []}), ([], ["模型返回空数组"]))
        _, dropped = extract.sanitize_facts({"facts": "nope"})
        self.assertIn("facts 不是数组", dropped)

    def test_sanitize_clamps_numbers(self):
        items, _ = extract.sanitize_facts(
            {"facts": [{"slot": "a", "content": "b", "importance": 99, "confidence": 5,
                        "kind": "weird"}]}
        )
        self.assertEqual(items[0]["importance"], 5)
        self.assertEqual(items[0]["confidence"], 1.0)
        self.assertEqual(items[0]["kind"], "fact")

    def test_persona_parse_and_clamp(self):
        deltas, why = extract.sanitize_persona(
            {"deltas": {"warmth": 0.5, "x": 1}, "reason": " 因为开心 "}
        )
        self.assertEqual(deltas["warmth"], 0.06)
        self.assertNotIn("x", deltas)
        self.assertEqual(why, "因为开心")


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.log = Journal(self.dir, level="INFO", days=7)

    def test_write_and_tail(self):
        self.log.write("INFO", "care.sent", "发了", scope="s", gap=30)
        self.log.write("DEBUG", "debug.only", "看不见")
        rows = self.log.tail(10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "care.sent")
        self.assertEqual(rows[0]["gap"], 30)

    def test_level_filter_and_query(self):
        self.log.write("INFO", "flush.ok", "写入 2 条")
        self.log.write("WARN", "llm.nojson", "没有 JSON")
        self.assertEqual(len(self.log.tail(10, level="WARN")), 1)
        self.assertEqual(len(self.log.tail(10, query="写入")), 1)
        self.assertEqual(len(self.log.tail(10, event="llm")), 1)

    def test_stats_and_clear(self):
        self.log.write("ERROR", "care.fail", "挂了")
        stats = self.log.stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["counts"]["ERROR"], 1)
        self.assertEqual(self.log.clear(), 1)
        self.assertEqual(self.log.stats()["total"], 0)

    def test_rotate_keeps_old_file(self):
        log = Journal(self.dir, level="INFO", max_bytes=64 * 1024)
        for index in range(600):
            log.write("INFO", "x", "y" * 200, i=index)
        self.assertTrue(any(name.endswith(".log.1") for name in os.listdir(self.dir)))

    def test_text_line(self):
        record = {"time": "2026-10-31 15:00:00", "level": "INFO", "event": "a.b",
                  "msg": "hi", "k": 1}
        self.assertIn("a.b", Journal.text_line(record))


if __name__ == "__main__":
    unittest.main()
