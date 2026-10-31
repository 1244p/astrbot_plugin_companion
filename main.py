"""AstrBot 插件入口：只做框架接线，业务全在 core/。

上一版 main.py 有 1230 行，钩子、SQL、提示词、HTTP 路由混在一起，改一处要
读全文件。这里的分工是：main.py 认识 AstrBot，core/ 不认识。
"""

from __future__ import annotations

import asyncio
import os
import random

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star

from .api.routes import Routes
from .core import care, db, extract
from .core.config import Config
from .core.journal import Journal
from .core.service import Service

PLUGIN_NAME = "astrbot_plugin_companion"
Routes.PLUGIN_NAME = PLUGIN_NAME
TICK_SECONDS = 20
JANITOR_EVERY = 3  # 每 3 个 tick（60 秒）检查一次主动消息

HELP = (
    "记忆命令：\n"
    "/memory dump [n]      看最近记住的东西\n"
    "/memory why <文本>    看这次会注入什么、为什么\n"
    "/memory extract       干跑一次抽取，不写库\n"
    "/memory extract go    立刻抽一次并写库\n"
    "/memory forget <id>   删除一条\n"
    "/memory confirm <id>  把待确认的条目认下来\n"
    "/memory persona      看人格五维\n"
    "/memory care on|off|status|now|why\n"
    "/memory logs [n]      看插件自己的日志\n"
    "/memory settings      （面板里改更方便）"
)


def _data_dir() -> str:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return get_astrbot_plugin_data_path()
    except Exception:  # noqa: BLE001
        return os.path.join(os.path.expanduser("~"), ".astrbot", "plugin_data")


class CompanionPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.raw_config = config
        self.cfg = Config.load(dict(config) if config else {})
        root = _data_dir()
        self.root = os.path.join(root, PLUGIN_NAME)
        os.makedirs(self.root, exist_ok=True)
        self.journal = Journal(
            os.path.join(self.root, "logs"), level=self.cfg.log_level, days=self.cfg.log_days
        )
        self.conn = db.connect(os.path.join(self.root, "memory.db"))
        self.service = Service(
            self.conn, self.cfg, self.journal, self._ask, self._embed
        )
        self._warned: set[str] = set()
        self._tick = 0
        self._task: asyncio.Task | None = None
        self._seen: dict[str, dict] = {}
        self.routes = Routes(self)
        # 不调这一句，面板拿到的每个接口都会是「未找到该路由」——页面能开，但一片空白。
        self.routes.register()
        self.journal.write("INFO", "plugin.start", "插件已加载",
                           version=self._version(), scopes=len(self.service.known_scopes()))
        self._ensure_janitor()

    # ---------- 生命周期 ----------

    def _version(self) -> str:
        try:
            import io

            path = os.path.join(os.path.dirname(__file__), "metadata.yaml")
            with io.open(path, encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("version:"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
        return "?"

    def _ensure_janitor(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._janitor())

    async def _janitor(self) -> None:
        self.journal.write("DEBUG", "janitor.start", "后台循环启动")
        while True:
            try:
                await asyncio.sleep(TICK_SECONDS)
                self._tick += 1
                await self._tick_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - 一轮出错不能带走整个循环
                self.journal.write("ERROR", "janitor.fail", f"循环异常：{error}")
                logger.error(f"[companion] 后台循环异常：{error}")

    async def _tick_once(self) -> None:
        for scope in self.service.known_scopes():
            try:
                await self._flush_if_idle(scope)
                await self.service.backfill(scope, 8)
            except Exception as error:  # noqa: BLE001
                self.journal.write("WARN", "janitor.scope_fail", str(error), scope=scope)
        if self._tick % JANITOR_EVERY == 0:
            for scope in self.service.known_scopes():
                if self._is_private(scope):
                    try:
                        await self._maybe_care(scope)
                    except Exception as error:  # noqa: BLE001
                        self.journal.write("WARN", "care.fail", str(error), scope=scope)

    async def terminate(self) -> None:
        if self._task is not None:
            self._task.cancel()
        try:
            for scope in list(self.service.buffers):
                await self.service.flush(scope, reason="shutdown")
        except Exception as error:  # noqa: BLE001
            self.journal.write("WARN", "flush.fail", f"退出前落盘失败：{error}")
        self.journal.write("INFO", "plugin.stop", "插件已卸载")
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass

    # ---------- 对话钩子 ----------

    @filter.on_llm_request()
    async def on_request(self, event: AstrMessageEvent, req):
        if not self.cfg.enabled:
            return
        scope = event.unified_msg_origin
        if not self._is_private(scope):
            return
        text = (event.message_str or "").strip()
        if text:
            self.service.push(scope, f"用户：{text}")
            self.service.care.touch_active(scope, __import__("time").time())
        try:
            block = await self.service.injection(scope, text)
        except Exception as error:  # noqa: BLE001
            self.journal.write("WARN", "recall.fail", str(error), scope=scope)
            return
        if block:
            self._inject(req, block)
        if self.cfg.flush_turns and self.service.buffer_len(scope) >= self.cfg.flush_turns:
            asyncio.create_task(self.service.flush(scope, reason="turns"))

    @filter.on_llm_response()
    async def on_response(self, event: AstrMessageEvent, resp):
        if not self.cfg.enabled:
            return
        scope = event.unified_msg_origin
        if not self._is_private(scope):
            return
        text = (getattr(resp, "completion_text", "") or "").strip()
        if text:
            self.service.push(scope, f"助手：{text[:300]}")

    @staticmethod
    def _inject(req, block: str) -> None:
        try:
            from astrbot.core.agent.message import TextPart

            part = TextPart(text=block)
            if hasattr(part, "mark_as_temp"):
                part.mark_as_temp()
            req.extra_user_content_parts.append(part)
        except Exception as error:  # noqa: BLE001
            logger.debug(f"[companion] 注入失败：{error}")

    @staticmethod
    def _is_private(scope: str) -> bool:
        parts = str(scope).split(":")
        return len(parts) > 1 and parts[1] == "FriendMessage"

    # ---------- 模型 ----------

    async def _provider(self, provider_id: str, umo: str = ""):
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider is None:
                self._warn(provider_id)
            else:
                return provider
        return await self.context.get_using_provider_async(umo)

    def _warn(self, provider_id: str) -> None:
        if provider_id in self._warned:
            return
        self._warned.add(provider_id)
        ids = self._available_ids()
        self.journal.write("WARN", "provider.missing",
                           f"provider '{provider_id}' 不存在，已退回当前会话模型",
                           available=",".join(ids)[:200])
        logger.warning(f"[companion] provider '{provider_id}' 不存在，可用：{', '.join(ids)}")

    def _available_ids(self) -> list[str]:
        ids = []
        try:
            for item in self.context.get_all_providers():
                ids.append(str((item.provider_config or {}).get("id", "")))
        except Exception:  # noqa: BLE001
            pass
        return [i for i in ids if i]

    async def _ask(self, system: str, prompt: str) -> str:
        provider = await self._provider(self.cfg.extract_provider_id)
        if provider is None:
            raise RuntimeError("没有可用的模型提供商")
        resp = await provider.text_chat(prompt=prompt, system_prompt=system)
        text = getattr(resp, "completion_text", "") or ""
        self.journal.write("DEBUG", "llm.ok", f"{len(text)} 字",
                           model=getattr(provider, "get_model", lambda: "")())
        return text

    async def _embed(self, text: str) -> list[float]:
        if not self.cfg.embedding_provider_id:
            return []
        provider = None
        try:
            for item in self.context.get_all_embedding_providers():
                if (item.provider_config or {}).get("id") == self.cfg.embedding_provider_id:
                    provider = item
                    break
        except Exception:  # noqa: BLE001
            return []
        if provider is None:
            self._warn(self.cfg.embedding_provider_id)
            return []
        result = provider.get_embedding(text)
        if asyncio.iscoroutine(result):
            result = await result
        return list(result or [])

    # ---------- 空闲落盘 ----------

    async def _flush_if_idle(self, scope: str) -> None:
        count = self.service.buffer_len(scope)
        if not count or count >= self.cfg.flush_turns:
            return
        state = self.service.care_state(scope)
        idle = __import__("time").time() - float(state.get("last_active") or 0)
        if idle >= self.cfg.idle_seconds:
            await self.service.flush(scope, reason="idle")

    # ---------- 主动开口 ----------

    async def _maybe_care(self, scope: str, force: bool = False) -> bool:
        (ok, reason), now = self.service.care_decide(scope, force=force)
        self._seen[scope] = {"at": now.timestamp(), "reason": reason}
        if not ok:
            self.service.care.mark_skipped(scope, now.timestamp(), reason)
            self.journal.write("DEBUG", "care.skip", care.reason_text(reason), scope=scope)
            return False
        try:
            provider = await self._provider(self.cfg.care_provider_id or "", scope)
            if provider is None:
                return False
            resp = await provider.text_chat(
                prompt=self.service.care_prompt(scope), system_prompt="你正在主动发一条消息。"
            )
            data = extract.extract_json(getattr(resp, "completion_text", "") or "")
            parsed = care.sanitize_reply(data or {}, self.cfg.care_max_chars)
        except Exception as error:  # noqa: BLE001
            self.journal.write("ERROR", "care.fail", f"生成失败：{error}", scope=scope)
            return False
        if not parsed:
            self.service.care.mark_skipped(scope, now.timestamp(), "no_prompt")
            return False

        text, gap = parsed
        parts = care.split_sentences(text, self.cfg.care_split_max) if self.cfg.care_split else [text]
        delivered = 0
        for index, part in enumerate(parts):
            if index:
                await asyncio.sleep(self.cfg.care_split_delay)
            if self.context.send_message(scope, MessageChain([Plain(part)])):
                delivered += 1
                if self.cfg.care_split_delay and index:
                    await asyncio.sleep(self.cfg.care_split_delay)
            else:
                self.journal.write("WARN", "care.send_fail", "平台没接住", scope=scope, part=index)
                break
        if not delivered:
            return False
        self.service.care.mark_sent(scope, now.timestamp(), text, gap, care.day_key(now))
        self.journal.write("INFO", "care.sent", text[:40], scope=scope, gap=gap, parts=delivered)
        return True

    @property
    def care_provider_id(self) -> str:
        return ""

    # ---------- 命令 ----------

    @filter.command("memory")
    async def memory(self, event: AstrMessageEvent):
        scope = event.unified_msg_origin
        args = (event.message_str or "").split()[1:]
        head = (args[0] if args else "").lower()
        tail = args[1:]

        if head in {"", "help"}:
            yield event.plain_result(HELP)
            return
        if head == "dump":
            limit = int(tail[0]) if tail and tail[0].isdigit() else 20
            facts = self.service.facts.list_facts(scope, limit=limit)
            if not facts:
                yield event.plain_result("还没有记住任何东西。")
                return
            lines = [f"共 {len(facts)} 条："]
            for fact in facts:
                mark = "" if fact.status == "active" else f" ({fact.status})"
                lines.append(f"#{fact.id} [{fact.slot}] {fact.content}{mark}")
            yield event.plain_result("\n".join(lines))
            return
        if head == "why":
            query = " ".join(tail) or "刚才说了什么"
            rows = self.service.explain(scope, query)
            if not rows:
                yield event.plain_result("这次没有可注入的记忆。")
                return
            lines = [f"对「{query}」的召回："]
            for row in rows:
                why = row["why"]
                lines.append(
                    f"#{row['id']} [{row['slot']}] {row['content']}  "
                    f"分数 {row['score']}（相似 {why['similarity']}/{why['mode']}，"
                    f"新近 {why['recency']}，重要 {why['importance']}）"
                )
            yield event.plain_result("\n".join(lines))
            return
        if head == "extract":
            if tail and tail[0] == "go":
                before = len(self.service.facts.list_facts(scope, limit=500))
                result = await self.service.flush(scope, reason="manual")
                after = len(self.service.facts.list_facts(scope, limit=500))
                yield event.plain_result(
                    f"抽取完成：新增 {result['written']} 条，库 {before} → {after} 条。"
                )
                return
            result = await self.service.flush(scope, reason="manual", dry_run=True)
            items = result.get("preview") or []
            lines = [f"缓冲 {result['turns']} 条，模型返回 {len(items)} 条可存："]
            for item in items:
                lines.append(f"- [{item['slot']}] {item['content']}")
            if result["dropped"]:
                lines.append("丢弃：" + "；".join(result["dropped"]))
            if result.get("raw"):
                lines.append("模型原文：" + result["raw"][:200])
            yield event.plain_result("\n".join(lines))
            return
        if head in {"forget", "confirm"} and tail:
            try:
                ids = [int(x) for x in tail if x.isdigit()]
            except ValueError:
                ids = []
            if not ids:
                yield event.plain_result("用法：/memory forget <id> 或 /memory confirm <id>")
                return
            if head == "forget":
                removed = self.service.facts.delete(scope, ids)
                yield event.plain_result(f"删除 {removed} 条。")
            else:
                changed = self.service.facts.confirm(scope, ids)
                yield event.plain_result(f"确认 {changed} 条。")
            return
        if head == "persona":
            state = self.service.people.get(scope)
            lines = [f"人格 v{state['version']}："]
            for key, value in state["traits"].items():
                base = self.cfg.baseline.get(key, 0.5)
                lines.append(f"- {key}: {value:.2f}（基线 {base:.2f}）")
            lines.append(f"今日已用位移 {state['day_used'] * 100:.1f}%")
            yield event.plain_result("\n".join(lines))
            return
        if head == "care":
            action = (tail[0] if tail else "status").lower()
            if action in {"on", "off"}:
                self.service.care.set_enabled(scope, action == "on")
                yield event.plain_result(f"这个会话的主动消息已{'打开' if action == 'on' else '关闭'}。")
                return
            if action == "now":
                sent = await self._maybe_care(scope, force=True)
                yield event.plain_result("发出去了。" if sent else "这次没发，用 /memory care why 看原因。")
                return
            if action == "why":
                (ok, reason), now = self.service.care_decide(scope)
                state = self.service.care_state(scope)
                lines = [f"现在 {now:%Y-%m-%d %H:%M}（{self.cfg.care_timezone}）",
                         f"结论：{care.reason_text(reason)}"]
                for gate in care.gate_detail(now, state, self.cfg.care_block()):
                    lines.append(f"{'✓' if gate['ok'] else '✗'} {gate['detail']}")
                lines.append(f"今日 {state['sent_today']}/{self.cfg.care_daily_max}，"
                             f"未回应 {state['unanswered']}/{self.cfg.care_max_unanswered}")
                yield event.plain_result("\n".join(lines))
                return
            state = self.service.care_state(scope)
            (ok, reason), now = self.service.care_decide(scope)
            stamp, basis = care.next_at(state, self.cfg.care_block())
            when = f"{__import__('datetime').datetime.fromtimestamp(stamp):%H:%M}" if stamp else "—"
            yield event.plain_result(
                f"总开关 {'开' if self.cfg.care_enable else '关'}｜会话 "
                f"{'开' if state['enabled'] else '关'}｜结论 {care.reason_text(reason)}\n"
                f"下次最早 {when}（依据 {basis}），今日 {state['sent_today']}/"
                f"{self.cfg.care_daily_max}"
            )
            return
        if head == "logs":
            if tail and tail[0] == "clear":
                removed = self.journal.clear()
                yield event.plain_result(f"清掉 {removed} 个日志文件。")
                return
            limit = int(tail[0]) if tail and tail[0].isdigit() else 20
            records = self.journal.tail(limit)
            if not records:
                yield event.plain_result("没有日志。")
                return
            yield event.plain_result(
                "\n".join(self.journal.text_line(r) for r in records)
            )
            return
        yield event.plain_result(HELP)

    @filter.llm_tool(name="memory_search")
    async def tool_search(self, event: AstrMessageEvent, query: str = "") -> str:
        """按关键词找之前记住的事。用户问「你还记得…」时调用。

        Args:
            query(string): 要找的内容，用用户原话里的关键词
        """
        rows = self.service.explain(event.unified_msg_origin, query or " ")
        if not rows:
            return "没有相关记忆。"
        return "\n".join(f"[{r['slot']}] {r['content']}" for r in rows[:5])

    @filter.llm_tool(name="memory_write")
    async def tool_write(self, event: AstrMessageEvent, slot: str = "", content: str = "",
                         kind: str = "fact") -> str:
        """把用户明确要求记住的事写进长期记忆。

        Args:
            slot(string): 归一类槽位，如 饮食/作息/称呼/在办事项
            content(string): 要记住的内容
            kind(string): preference/profile/fact/event/task 之一
        """
        from .core.facts import Fact

        if not slot or not content:
            return "缺少 slot 或 content。"
        fact, action = self.service.facts.write(
            Fact(scope=event.unified_msg_origin, slot=slot, content=content,
                 kind=kind if kind in {"preference", "profile", "fact", "event", "task"} else "fact",
                 source="tool")
        )
        return f"{'已记住' if action != 'duplicate' else '本来就有'}：#{fact.id} [{fact.slot}] {fact.content}"
