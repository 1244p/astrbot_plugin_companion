"""接口层测试：路由注册路径 + 每个 handler 都能真的跑通。

面板「约等于没有」的两次根因都在这一层：
1. `Routes.register()` 没人调用 → 框架里一条路由都没有，页面每个请求都回
   `{"status":"error","message":"未找到该路由"}`。
2. 注册前缀写成 `/<插件名>/api/...`，而框架是用 `/<插件名>/<子路径>` 去
   `re.fullmatch`（dashboard/api/plugins.py:209），永远匹配不上。

所以这里既断言注册出来的字符串，也断言前端 app.js 里出现的每个 endpoint
都在注册表里——两边的名字一旦漂掉就红。
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 插件在宿主里是以包的形式加载的（main.py 用 `from .core import ...`），
# 所以测试也要按包名导入，否则 api/routes.py 的相对导入会越界。
sys.path.insert(0, os.path.dirname(ROOT))

from astrbot_plugin_companion.api import routes as routes_mod  # noqa: E402
from astrbot_plugin_companion.api.routes import Routes  # noqa: E402
from astrbot_plugin_companion.core import db  # noqa: E402
from astrbot_plugin_companion.core.config import Config  # noqa: E402
from astrbot_plugin_companion.core.journal import Journal  # noqa: E402
from astrbot_plugin_companion.core.service import Service  # noqa: E402

PLUGIN = "astrbot_plugin_companion"


class FakeQuery:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=""):
        return self.values.get(key, default)


class FakeRequest:
    """最小替身：只提供 routes 真正用到的三个成员。"""

    def __init__(self, values=None, body=None, method="GET"):
        self.query = FakeQuery(values or {})
        self._body = body or {}
        self.method = method

    async def json(self, default=None):
        return self._body or (default or {})


class FakeContext:
    def __init__(self):
        self.registered: list[tuple[str, object, list[str], str]] = []

    def register_web_api(self, route, handler, methods, desc):
        self.registered.append((route, handler, methods, desc))


class FakePlugin:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config.load({})
        self.conn = db.connect(os.path.join(self.tmp.name, "memory.db"))
        self.journal = Journal(os.path.join(self.tmp.name, "logs"))
        self.service = Service(self.conn, self.cfg, self.journal, self._ask)
        self.context = FakeContext()

    @staticmethod
    async def _ask(prompt, timeout=45):
        return {}

    def close(self):
        self.conn.close()
        self.tmp.cleanup()


def seed(plugin: FakePlugin, scope: str = "qq:FriendMessage:abc123"):
    from astrbot_plugin_companion.core.facts import Fact

    plugin.service.facts.write(
        Fact(scope=scope, slot="饮食", content="不吃香菜", kind="preference",
             importance=4, confidence=0.9, polarity=-1)
    )
    plugin.service.facts.write(
        Fact(scope=scope, slot="任务", content="在修 AstrBot 的 bug", kind="task",
             importance=3, confidence=0.8)
    )
    plugin.service.care.touch_active(scope, 1_700_000_000.0)
    return scope


class RegisterTest(unittest.TestCase):
    def setUp(self):
        self.plugin = FakePlugin()
        self.routes = Routes(self.plugin)
        self.count = self.routes.register()

    def tearDown(self):
        self.plugin.close()

    def test_registers_every_endpoint(self):
        self.assertEqual(self.count, 10)
        paths = sorted(route for route, *_ in self.plugin.context.registered)
        self.assertEqual(
            paths,
            sorted(
                f"/{PLUGIN}/{name}"
                for name in (
                    "scopes", "overview", "facts", "facts/delete", "facts/confirm",
                    "persona", "care", "settings", "logs", "logs/clear",
                )
            ),
        )

    def test_prefix_has_no_api_segment(self):
        """框架把 /<插件名>/<子路径> 整段拿来匹配，多一层 api 就永远匹配不上。"""
        for route, *_ in self.plugin.context.registered:
            self.assertNotIn("/api/", route)
            self.assertTrue(route.startswith(f"/{PLUGIN}/"), route)

    def test_frontend_endpoints_all_registered(self):
        with open(os.path.join(ROOT, "views/companion/app.js"), encoding="utf-8") as f:
            js = f.read()
        used = set(re.findall(r'(?:api|post)\("([a-z/]+)"', js))
        self.assertTrue(used, "app.js 里没找到接口调用，正则或前端结构变了")
        registered = {route.split("/", 2)[2] for route, *_ in self.plugin.context.registered}
        self.assertEqual(used - registered, set(), "前端调用了没注册的接口")

    def test_methods_declared(self):
        methods = {route.split("/", 2)[2]: m for route, _h, m, _d in self.plugin.context.registered}
        self.assertEqual(methods["facts/delete"], ["POST"])
        self.assertEqual(methods["logs/clear"], ["POST"])
        self.assertEqual(sorted(methods["care"]), ["GET", "POST"])


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.plugin = FakePlugin()
        self.routes = Routes(self.plugin)
        self.routes.register()
        self.scope = seed(self.plugin)
        self._orig = routes_mod.request
        # 没有真正的框架请求上下文时，_query / _body 会走默认值；这里塞一个替身
        routes_mod.request = FakeRequest({"scope": self.scope, "limit": "20"})

    def tearDown(self):
        routes_mod.request = self._orig
        self.plugin.close()

    def run_async(self, coro):
        return asyncio.run(coro)

    def payload(self, value):
        self.assertEqual(value["status"], "ok", value)
        return value["data"]

    def test_scopes(self):
        data = self.payload(self.run_async(self.routes.scopes()))
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["scope"], self.scope)
        self.assertEqual(data["items"][0]["total"], 2)
        self.assertTrue(data["items"][0]["label"].startswith("私聊 · "))

    def test_overview(self):
        data = self.payload(self.run_async(self.routes.overview()))
        labels = [item["label"] for item in data["items"]]
        self.assertEqual(
            labels,
            ["有效记忆", "待确认", "已取代", "平均置信度", "向量覆盖", "从未召回"],
        )
        self.assertEqual(data["scope"], self.scope)
        self.assertIn("traits", data["persona"])
        self.assertIn("care", data)

    def test_facts(self):
        data = self.payload(self.run_async(self.routes.facts()))
        self.assertEqual(data["total"], 2)
        row = data["items"][0]
        for key in ("id", "slot", "content", "kind", "importance", "confidence",
                    "status", "hits", "updated_at", "has_vector"):
            self.assertIn(key, row)

    def test_facts_filter_by_query(self):
        routes_mod.request = FakeRequest({"scope": self.scope, "q": "香菜"})
        data = self.payload(self.run_async(self.routes.facts()))
        self.assertEqual(data["total"], 1)
        self.assertIn("香菜", data["items"][0]["content"])

    def test_persona(self):
        data = self.payload(self.run_async(self.routes.persona()))
        self.assertEqual(set(data["traits"]), {"intimacy", "playfulness", "directness",
                                               "initiative", "warmth"})
        self.assertIn("day_budget", data)
        self.assertIsInstance(data["log"], list)

    def test_care_get(self):
        data = self.payload(self.run_async(self.routes.care()))
        self.assertIn("gates", data)
        self.assertEqual(len(data["gates"]), 2)
        self.assertTrue(data["global"] in (True, False))

    def test_care_post_toggles(self):
        routes_mod.request = FakeRequest(body={"scope": self.scope, "enabled": False},
                                         method="POST")
        data = self.payload(self.run_async(self.routes.care()))
        self.assertFalse(data["enabled"])
        routes_mod.request = FakeRequest(body={"scope": self.scope, "enabled": True},
                                         method="POST")
        data = self.payload(self.run_async(self.routes.care()))
        self.assertTrue(data["enabled"])

    def test_settings_describe(self):
        data = self.payload(self.run_async(self.routes.settings()))
        ids = [group["id"] for group in data["groups"]]
        self.assertIn("care", ids)
        for group in data["groups"]:
            self.assertTrue(group["fields"], f"分组 {group['id']} 没有字段")

    def test_logs_and_clear(self):
        self.plugin.journal.write("INFO", "test.event", "hello", scope=self.scope)
        data = self.payload(self.run_async(self.routes.logs()))
        self.assertTrue(any(item["event"] == "test.event" for item in data["items"]))
        self.assertIn("stats", data)
        cleared = self.payload(self.run_async(self.routes.logs_clear()))
        self.assertGreaterEqual(cleared["removed"], 1)

    def test_delete_and_confirm(self):
        rows = self.plugin.service.facts.list_facts(self.scope)
        target = rows[-1]
        routes_mod.request = FakeRequest(
            body={"scope": self.scope, "ids": [target.id]}, method="POST"
        )
        data = self.payload(self.run_async(self.routes.delete()))
        self.assertEqual(data["removed"], 1)
        self.assertEqual(len(self.plugin.service.facts.list_facts(self.scope)), 1)

    def test_delete_without_ids_is_rejected(self):
        routes_mod.request = FakeRequest(body={"scope": self.scope}, method="POST")
        data = self.payload(self.run_async(self.routes.delete()))
        self.assertFalse(data["ok"])
        self.assertIn("error", data)


if __name__ == "__main__":
    unittest.main()
