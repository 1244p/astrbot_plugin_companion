"""发送层测试。

存在的理由：框架 `send_message` 是 async 的，忘了 await 会得到一个恒真的
coroutine，调用方以为发成功（日志写 care.sent），用户那边什么也没有。
v0.1.13 之前就是这么发的，v0.1.14 修掉并把这些断言留下。
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import deliver  # noqa: E402


class FakeSender:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.awaited = 0

    async def __call__(self, scope, text):
        self.calls.append((scope, text))
        self.awaited += 1
        value = self.results.pop(0) if self.results else True
        if isinstance(value, Exception):
            raise value
        return value


class DeliverTest(unittest.TestCase):
    def test_awaits_async_sender(self):
        sender = FakeSender([True])
        result = asyncio.run(deliver.deliver(sender, "s", "甲", split=False))
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(sender.awaited, 1, "async sender 必须被 await")

    def test_delivers_each_sentence(self):
        sender = FakeSender([True, True])
        result = asyncio.run(deliver.deliver(sender, "s", "甲。乙。", max_parts=2, delay=0))
        self.assertEqual(result["delivered"], 2)
        self.assertEqual(len(sender.calls), 2)

    def test_stops_when_platform_refuses(self):
        sender = FakeSender([True, False])
        result = asyncio.run(deliver.deliver(sender, "s", "甲。乙。", max_parts=2, delay=0))
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(result["stopped_at"], 1)
        self.assertTrue(sender.calls, "至少尝试过发送")

    def test_reports_exception(self):
        sender = FakeSender([RuntimeError("主动消息配额已用完")])
        result = asyncio.run(deliver.deliver(sender, "s", "甲", split=False))
        self.assertEqual(result["delivered"], 0)
        self.assertIn("配额", result["error"])

    def test_sync_sender_also_works(self):
        calls = []

        def sender(scope, text):
            calls.append(text)
            return True

        result = asyncio.run(deliver.deliver(sender, "s", "甲", split=False))
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(calls, ["甲"])

    def test_waits_between_parts(self):
        waited = []

        async def fake_sleep(seconds):
            waited.append(seconds)

        sender = FakeSender([True, True])
        asyncio.run(deliver.deliver(sender, "s", "甲。乙。", max_parts=2,
                                    delay=1.5, sleep=fake_sleep))
        self.assertEqual(waited, [1.5])


class NoUnawaitedTest(unittest.TestCase):
    def test_no_coroutine_warning(self):
        """用警告过滤器抓「coroutine was never awaited」——那正是发不出去的病根。"""
        import warnings

        sender = FakeSender([True, True])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            asyncio.run(deliver.deliver(sender, "s", "甲。乙。", max_parts=2, delay=0))
        messages = [str(item.message) for item in caught]
        self.assertFalse([m for m in messages if "never awaited" in m], messages)


class SourceGuardTest(unittest.TestCase):
    """源码级：任何 self.context.send_message(...) 都必须带 await。
    这是 v0.1.14 那个「日志说发了、用户没收到」的直接病因。"""

    def test_every_framework_send_is_awaited(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "main.py")
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        offenders = [f"main.py:{number}: {line.strip()}"
                     for number, line in enumerate(lines, 1)
                     if "context.send_message(" in line and "await" not in line]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
