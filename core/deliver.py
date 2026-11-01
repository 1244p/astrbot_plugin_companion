"""把一段话拆成多气泡发出去。

这层单独拎出来，是因为「发了但没发出去」这种错最难看出来：
框架的 `StarContext.send_message` 是 async 的，忘了 await 会拿到一个
永远为真的 coroutine 对象，于是调用方以为发成功、日志写 care.sent，
用户那边一条都没有。这里集中 await，并由 tests/test_deliver.py 盯着。
"""

import asyncio
import inspect

from . import care


async def deliver(sender, scope: str, text: str, *, split: bool = True,
                  max_parts: int = 2, delay: float = 0.0, sleep=None) -> dict:
    """逐段调用 sender(scope, part)，返回 {delivered, parts, error, stopped_at}。

    sender 可以是 async 也可以是同步函数，返回值按 bool 解释；抛异常则中止，
    已经发出去的那几段算数。
    """
    waiter = sleep or asyncio.sleep
    parts = care.split_sentences(text, max_parts) if split else [text]
    delivered = 0
    for index, part in enumerate(parts):
        if index and delay:
            await waiter(delay)
        try:
            ok = sender(scope, part)
            if inspect.isawaitable(ok):
                ok = await ok
        except Exception as error:  # noqa: BLE001 - 发送失败要把原因带回去
            return {"delivered": delivered, "parts": parts,
                    "error": f"{type(error).__name__}: {error}", "stopped_at": index}
        if not ok:
            return {"delivered": delivered, "parts": parts,
                    "error": "", "stopped_at": index}
        delivered += 1
    return {"delivered": delivered, "parts": parts, "error": "", "stopped_at": -1}
