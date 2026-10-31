"""面板用的 HTTP 接口。

一条路由一个函数，返回纯 dict / list；`scopes` 这种聚合查询直接在这里拼，
不回头叫 core 里的业务方法，避免面板需求把业务层越改越耦合。
"""

from __future__ import annotations

import json
import os
import time

try:
    from astrbot.api.web import json_response, request
except Exception:  # pragma: no cover - 老版本框架没有 web 模块
    json_response = None
    request = None

FACT_FIELDS = (
    "id", "slot", "content", "kind", "importance", "confidence", "polarity",
    "status", "hits", "updated_at", "vector_model",
)


def _ok(data):
    payload = {"status": "ok", "data": data}
    if json_response is None:
        return payload
    return json_response(payload)


def _scope_label(scope: str) -> str:
    parts = str(scope).split(":")
    if len(parts) > 2 and parts[1] == "FriendMessage":
        return f"私聊 · {parts[2][:12]}"
    if len(parts) > 2 and parts[1] == "GroupMessage":
        return f"群聊 · {parts[2][:12]}"
    return scope


class Routes:
    def __init__(self, plugin):
        self.plugin = plugin
        self.prefix = f"/{plugin.__class__.__module__.split('.')[0]}/api"

    # ---------- 注册 ----------

    def register(self) -> int:
        register = getattr(self.plugin.context, "register_web_api", None)
        if register is None:
            return 0
        table = {
            "scopes": (self.scopes, ["GET"]),
            "overview": (self.overview, ["GET"]),
            "facts": (self.facts, ["GET"]),
            "facts/delete": (self.delete, ["POST"]),
            "facts/confirm": (self.confirm, ["POST"]),
            "persona": (self.persona, ["GET"]),
            "care": (self.care, ["GET", "POST"]),
            "settings": (self.settings, ["GET"]),
            "logs": (self.logs, ["GET"]),
            "logs/clear": (self.logs_clear, ["POST"]),
        }
        count = 0
        for path, (handler, methods) in table.items():
            try:
                register(f"{self.prefix}/{path}", handler, methods, f"companion {path}")
                count += 1
            except Exception as error:  # noqa: BLE001
                self.plugin.journal.write("WARN", "api.fail", f"路由 {path} 注册失败：{error}")
        return count

    # ---------- 工具 ----------

    @staticmethod
    def _query(key: str, default: str = "") -> str:
        if request is None:
            return default
        try:
            return request.query.get(key, default)
        except Exception:  # noqa: BLE001
            return default

    async def _body(self) -> dict:
        if request is None:
            return {}
        try:
            data = await request.json(default={})
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _scope(self) -> str:
        scope = self._query("scope")
        if scope:
            return scope
        scopes = self.plugin.service.known_scopes()
        return scopes[0] if scopes else ""

    def _fact_json(self, fact) -> dict:
        data = {key: getattr(fact, key) for key in FACT_FIELDS}
        data["has_vector"] = bool(fact.vector_model)
        return data

    # ---------- 处理 ----------

    async def scopes(self):
        service = self.plugin.service
        items = []
        for scope in service.known_scopes():
            stats = service.facts.stats(scope)
            state = service.people.get(scope)
            items.append(
                {
                    "scope": scope,
                    "label": _scope_label(scope),
                    "total": stats["active"],
                    "persona_version": state["version"],
                }
            )
        items.sort(key=lambda row: row["total"], reverse=True)
        return _ok({"items": items})

    async def overview(self):
        service = self.plugin.service
        scope = self._scope()
        if not scope:
            return _ok({"items": []})
        stats = service.facts.stats(scope)
        persona = service.people.get(scope)
        care_state = service.care_state(scope)
        items = [
            {"label": "有效记忆", "value": stats["active"]},
            {"label": "待确认", "value": stats["suspect"], "warn": stats["suspect"] > 0},
            {"label": "已取代", "value": stats["superseded"]},
            {"label": "平均置信度", "value": f"{stats['avg_confidence'] * 100:.0f}%"},
            {"label": "向量覆盖", "value": f"{int(stats['with_vector'] / max(1, stats['active']) * 100)}%"},
            {"label": "从未召回", "value": stats["never_hit"]},
        ]
        return _ok(
            {
                "scope": scope,
                "items": items,
                "persona": {"version": persona["version"], "traits": persona["traits"],
                            "day_used": persona["day_used"]},
                "care": {"sent_today": care_state["sent_today"], "enabled": bool(care_state["enabled"]),
                         "check_reason": care_state["check_reason"]},
            }
        )

    async def facts(self):
        service = self.plugin.service
        scope = self._scope()
        limit = int(self._query("limit", "50") or 50)
        rows = service.facts.list_facts(
            scope,
            slot=self._query("slot"),
            kind=self._query("kind"),
            status=self._query("status"),
            query=self._query("q"),
            limit=limit,
        )
        return _ok({"items": [self._fact_json(f) for f in rows], "total": len(rows)})

    async def delete(self):
        body = await self._body()
        scope = str(body.get("scope") or self._scope())
        ids = [int(x) for x in body.get("ids") or [] if str(x).isdigit()]
        if not scope or not ids:
            return _ok({"ok": False, "error": "缺少 scope 或 ids"})
        removed = self.plugin.service.facts.delete(scope, ids)
        self.plugin.journal.write("INFO", "fact.delete", f"删除 {removed} 条",
                                  scope=scope, ids=",".join(map(str, ids)))
        return _ok({"ok": True, "removed": removed})

    async def confirm(self):
        body = await self._body()
        scope = str(body.get("scope") or self._scope())
        ids = [int(x) for x in body.get("ids") or [] if str(x).isdigit()]
        if not scope or not ids:
            return _ok({"ok": False, "error": "缺少 scope 或 ids"})
        changed = self.plugin.service.facts.confirm(scope, ids)
        return _ok({"ok": True, "changed": changed})

    async def persona(self):
        service = self.plugin.service
        scope = self._scope()
        state = service.people.get(scope)
        return _ok(
            {
                "scope": scope,
                "traits": state["traits"],
                "baseline": self.plugin.cfg.baseline,
                "version": state["version"],
                "day_used": state["day_used"],
                "day_budget": self.plugin.cfg.persona_daily_budget,
                "last_evolve_at": state["last_evolve_at"],
                "log": service.people.history(scope, 12),
            }
        )

    async def care(self):
        if request is not None and getattr(request, "method", "GET") == "POST":
            body = await self._body()
            scope = str(body.get("scope") or self._scope())
            if "enabled" in body:
                self.plugin.service.care.set_enabled(scope, bool(body["enabled"]))
        scope = self._scope()
        state = self.plugin.service.care_state(scope)
        (ok, reason), now = self.plugin.service.care_decide(scope)
        return _ok(
            {
                "scope": scope,
                "enabled": bool(state["enabled"]),
                "global": self.plugin.cfg.care_enable,
                "reason": reason,
                "sent_today": state["sent_today"],
                "sent_day": state["sent_day"],
                "unanswered": state["unanswered"],
                "last_text": state["last_text"],
                "last_sent": state["last_sent"],
                "gates": __import__("core.care", fromlist=["care"]).gate_detail(
                    now, state, self.plugin.cfg.care_block()
                ),
            }
        )

    async def settings(self):
        from . import schema

        return _ok(schema.describe(self.plugin))

    async def logs(self):
        journal = self.plugin.journal
        limit = int(self._query("limit", "50") or 50)
        return _ok(
            {
                "items": journal.tail(
                    limit,
                    level=self._query("level"),
                    query=self._query("q"),
                    event=self._query("evt"),
                ),
                "stats": journal.stats(),
            }
        )

    async def logs_clear(self):
        removed = self.plugin.journal.clear()
        return _ok({"ok": True, "removed": removed})
