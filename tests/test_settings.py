"""设置页接口：校验 → 写配置 → 回读。

面板保存失败时用户只会看到一句「保存失败」，所以这里的断言重点不是「能不能
存」，而是「存进去的值和面板显示的值是不是同一个」：`Config.load` 会做范围
钳制，越界值必须把钳制后的结果回写给前端，否则用户填 99 存成 20、界面还显示 99。
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(ROOT))

from astrbot_plugin_companion.api import routes as routes_mod  # noqa: E402
from astrbot_plugin_companion.api import schema  # noqa: E402
from astrbot_plugin_companion.core.config import Config  # noqa: E402

from tests.test_routes import FakePlugin, FakeRequest  # noqa: E402


class DictConfig(dict):
    """替身：行为对齐 AstrBotConfig（dict + save_config）。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = 0

    def save_config(self):
        self.saved += 1
        return True


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.schema = schema.load_schema()

    def test_coerces_declared_types(self):
        values, errors = schema.validate(
            self.schema, {"top_k": "9", "enabled": "false", "max_step": "0.05"}
        )
        self.assertEqual(errors, {})
        self.assertEqual(values["top_k"], 9)
        self.assertIs(values["enabled"], False)
        self.assertAlmostEqual(values["max_step"], 0.05)

    def test_reports_type_error_per_field(self):
        values, errors = schema.validate(self.schema, {"top_k": "abc", "max_step": "x"})
        self.assertEqual(values, {})
        self.assertIn("top_k", errors)
        self.assertIn("max_step", errors)

    def test_ignores_unknown_and_missing_keys(self):
        values, errors = schema.validate(self.schema, {"nope": 1})
        self.assertEqual((values, errors), ({}, {}))


class DescribeTest(unittest.TestCase):
    def test_every_config_key_lands_in_a_group(self):
        plugin = FakePlugin()
        plugin.raw_config = DictConfig()
        try:
            data = schema.describe(plugin)
            listed = {f["key"] for group in data["groups"] for f in group["fields"]}
            expected = {key for key in schema.load_schema() if key != "baseline"}
            self.assertEqual(listed, expected)
            for group in data["groups"]:
                self.assertTrue(group["desc"], group["id"])
        finally:
            plugin.close()

    def test_field_value_comes_from_running_config(self):
        plugin = FakePlugin()
        plugin.raw_config = DictConfig()
        plugin.cfg = Config.load({"top_k": 11})
        try:
            fields = {
                f["key"]: f
                for group in schema.describe(plugin)["groups"]
                for f in group["fields"]
            }
            self.assertEqual(fields["top_k"]["value"], 11)
        finally:
            plugin.close()


class SaveTest(unittest.TestCase):
    def setUp(self):
        self.plugin = FakePlugin()
        self.plugin.raw_config = DictConfig()
        self.routes = routes_mod.Routes(self.plugin)

    def tearDown(self):
        self.plugin.close()

    def save(self, body, method="POST"):
        routes_mod.request = FakeRequest(body=body, method=method)
        try:
            return asyncio.run(self.routes.settings())
        finally:
            routes_mod.request = None

    def test_saves_and_reloads_config(self):
        result = self.save({"top_k": "9", "care_enable": "true"})
        data = result["data"]
        self.assertTrue(data["ok"], data)
        self.assertEqual(self.plugin.cfg.top_k, 9)
        self.assertTrue(self.plugin.cfg.care_enable)
        self.assertIn("top_k", data["changed"])
        self.assertTrue(self.plugin.raw_config.saved)

    def test_clamped_value_is_written_back(self):
        data = self.save({"top_k": "999"})["data"]
        self.assertTrue(data["ok"])
        self.assertEqual(self.plugin.cfg.top_k, 20)
        self.assertEqual(self.plugin.raw_config["top_k"], 20)
        fields = {
            f["key"]: f for group in data["settings"]["groups"] for f in group["fields"]
        }
        self.assertEqual(fields["top_k"]["value"], 20)

    def test_invalid_value_keeps_config_untouched(self):
        data = self.save({"top_k": "abc"})["data"]
        self.assertFalse(data["ok"])
        self.assertIn("top_k", data["errors"])
        self.assertEqual(self.plugin.cfg.top_k, 6)
        self.assertFalse(self.plugin.raw_config.saved)

    def test_no_change_is_not_a_write(self):
        data = self.save({"top_k": "6"})["data"]
        self.assertTrue(data["ok"])
        self.assertEqual(data["changed"], [])
        self.assertFalse(self.plugin.raw_config.saved)

    def test_restart_field_is_reported(self):
        data = self.save({"db_path": "other.db"})["data"]
        self.assertTrue(data["ok"])
        self.assertIn("db_path", data["restart"])
        self.assertEqual(self.plugin.cfg.db_path, "other.db")

    def test_unknown_provider_id_is_rejected(self):
        self.plugin.provider_choices = lambda: [{"value": "siliconflow-memory-tidy",
                                                 "label": "siliconflow-memory-tidy · x"}]
        data = self.save({"extract_provider_id": "siliconflow/Qwen/Qwen3-30B"})["data"]
        self.assertFalse(data["ok"])
        self.assertIn("extract_provider_id", data["errors"])
        self.assertEqual(self.plugin.cfg.extract_provider_id, "")


class ProviderChoiceTest(unittest.TestCase):
    """「提供商 ID」填成模型名是真实犯过的错：选不到 → 静默退回会话模型 → 烧钱。"""

    def make(self, chat=None, embed=None, value=""):
        plugin = FakePlugin()
        plugin.raw_config = DictConfig()
        plugin.cfg = Config.load({"extract_provider_id": value,
                                  "embedding_provider_id": value})
        if chat is not None:
            plugin.provider_choices = lambda: chat
        if embed is not None:
            plugin.embedding_choices = lambda: embed
        return plugin

    def fields(self, plugin):
        return {f["key"]: f for group in schema.describe(plugin)["groups"] for f in group["fields"]}

    def test_options_include_empty_and_known_ids(self):
        plugin = self.make(chat=[{"value": "siliconflow-memory-tidy",
                                  "label": "siliconflow-memory-tidy · Qwen/Qwen3-30B"}])
        try:
            options = self.fields(plugin)["extract_provider_id"]["options"]
            values = [o["value"] for o in options]
            self.assertEqual(values[0], "")
            self.assertIn("siliconflow-memory-tidy", values)
        finally:
            plugin.close()

    def test_unknown_saved_value_is_flagged_not_hidden(self):
        plugin = self.make(chat=[{"value": "a", "label": "a"}],
                           value="siliconflow/Qwen/Qwen3-30B")
        try:
            options = self.fields(plugin)["extract_provider_id"]["options"]
            labels = {o["value"]: o["label"] for o in options}
            self.assertIn("siliconflow/Qwen/Qwen3-30B", labels)
            self.assertIn("没有这个提供商", labels["siliconflow/Qwen/Qwen3-30B"])
        finally:
            plugin.close()

    def test_no_provider_list_falls_back_to_text_input(self):
        plugin = self.make(chat=[])
        try:
            field = self.fields(plugin)["extract_provider_id"]
            self.assertNotIn("options", field)
        finally:
            plugin.close()


if __name__ == "__main__":
    unittest.main()
