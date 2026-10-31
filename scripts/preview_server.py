"""本地预览面板：给 views/companion 注入一个假的 bridge，用样例数据渲染。

只用于开发截图，不进包（build_zip.py 排除了它）。

    python scripts/preview_server.py --port 8140
    # 浏览器打开 http://127.0.0.1:8140/index.html?tab=facts&dark=1
"""

from __future__ import annotations

import argparse
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEW_DIR = os.path.join(ROOT, "views", "companion")

# 面板上要显示「改过的值」才看得出草稿效果，挑几项换掉默认值
VALUES = {
    "care_min_idle_minutes": 60,
    "care_schedule": "09:00-12:00, 19:00-23:00",
    "persona_daily_budget": 0.15,
    "top_k": 6,
}

SAMPLE_PROVIDERS = {
    "extract_provider_id": [
        {"value": "siliconflow-memory-tidy", "label": "siliconflow-memory-tidy · Qwen/Qwen3-30B-A3B-Instruct-2507"},
    ],
    "embedding_provider_id": [
        {"value": "siliconflow-embed", "label": "siliconflow-embed · BAAI/bge-m3"},
    ],
}

SCOPE = "qq_official_webhook:FriendMessage:3587BDB1966C264A3A00B33E632BADF5"

FACTS = [
    {"id": 12, "slot": "饮食", "content": "不吃香菜，但能接受一点点葱花", "kind": "preference",
     "importance": 4, "confidence": 0.92, "polarity": -1, "status": "active", "hits": 6,
     "updated_at": 1761900000, "vector_model": "Qwen/Qwen3-Embedding-0.6B", "has_vector": True},
    {"id": 11, "slot": "在办的事", "content": "在给 AstrBot 写陪伴插件，想加主动开口", "kind": "task",
     "importance": 5, "confidence": 0.88, "polarity": 0, "status": "active", "hits": 3,
     "updated_at": 1761890000, "vector_model": "Qwen/Qwen3-Embedding-0.6B", "has_vector": True},
    {"id": 9, "slot": "作息", "content": "一般凌晨两点睡，早上十点起", "kind": "profile",
     "importance": 4, "confidence": 0.81, "polarity": 0, "status": "active", "hits": 11,
     "updated_at": 1761880000, "vector_model": "", "has_vector": False},
    {"id": 8, "slot": "称呼", "content": "希望被叫「老板」", "kind": "profile",
     "importance": 3, "confidence": 0.76, "polarity": 0, "status": "active", "hits": 2,
     "updated_at": 1761870000, "vector_model": "Qwen/Qwen3-Embedding-0.6B", "has_vector": True},
    {"id": 7, "slot": "口味", "content": "爱吃辣，但最近在忌口", "kind": "preference",
     "importance": 3, "confidence": 0.44, "polarity": 1, "status": "suspect", "hits": 0,
     "updated_at": 1761860000, "vector_model": "", "has_vector": False},
    {"id": 6, "slot": "情绪", "content": "上周因为加班连着几天很烦", "kind": "event",
     "importance": 2, "confidence": 0.70, "polarity": 0, "status": "superseded", "hits": 1,
     "updated_at": 1761800000, "vector_model": "", "has_vector": False},
]

PERSONA = {
    "scope": SCOPE,
    "version": 14,
    "traits": {"intimacy": 0.63, "playfulness": 0.58, "directness": 0.41,
               "initiative": 0.55, "warmth": 0.66},
    "baseline": {"intimacy": 0.5, "playfulness": 0.5, "directness": 0.55,
                 "initiative": 0.45, "warmth": 0.5},
    "day_used": 0.062,
    "day_budget": 0.15,
    "last_evolve_at": 1761894000,
    "log": [
        {"version": 14, "reason": "聊到加班的烦，语气更近了一点", "created_at": 1761894000,
         "deltas": {"intimacy": 0.012, "warmth": 0.008}},
        {"version": 13, "reason": "被逗笑了两次，玩心上来", "created_at": 1761800000,
         "deltas": {"playfulness": 0.02, "directness": -0.004}},
        {"version": 12, "reason": "用户明确说别绕弯子", "created_at": 1761700000,
         "deltas": {"directness": 0.018}},
    ],
}

CARE = {
    "scope": SCOPE,
    "enabled": True,
    "global": True,
    "reason": "probability",
    "sent_today": 1,
    "sent_day": "2026-10-31",
    "unanswered": 0,
    "last_text": "你昨天说的那个 bug 修好了没？",
    "last_sent": 1761890000,
    "gates": [
        {"key": "idle", "ok": True, "detail": "已安静 132 分钟 / 要求 90 分钟"},
        {"key": "gap", "ok": True, "detail": "距上次主动 96 分钟 / 间隔 60 分钟"},
    ],
}

LOGS = [
    {"ts": 1761900000, "level": "INFO", "event": "flush.ok", "msg": "抽取完成",
     "facts": 2, "ms": 840},
    {"ts": 1761899000, "level": "INFO", "event": "care.skip", "msg": "这轮没发",
     "reason": "probability", "scope": "…BADF5"},
    {"ts": 1761898000, "level": "DEBUG", "event": "llm.ok", "msg": "模型返回",
     "ms": 812, "chars": 611, "parsed": True},
    {"ts": 1761897000, "level": "WARN", "event": "llm.timeout", "msg": "抽取超时，已跳过"},
    {"ts": 1761896000, "level": "INFO", "event": "care.sent", "msg": "主动发了一条",
     "chars": 38},
    {"ts": 1761895000, "level": "INFO", "event": "plugin.start", "msg": "插件已加载",
     "version": "v0.1.2"},
    {"ts": 1761894000, "level": "ERROR", "event": "janitor.fail", "msg": "循环异常：database is locked"},
]


def settings_payload() -> dict:
    """直接用真实 schema + 默认值造一份 describe 结果，别手写一份假的。"""
    import sys

    sys.path.insert(0, os.path.dirname(ROOT))
    from astrbot_plugin_companion.api import schema as schema_mod

    schema = schema_mod.load_schema(os.path.join(ROOT, "_conf_schema.json"))
    groups: dict[str, list] = {}
    for key, meta in schema.items():
        if key == "baseline":
            continue
        field = {
            "key": key,
            "title": schema_mod.TITLES.get(key, key),
            "type": meta.get("type", "string"),
            "hint": meta.get("hint", ""),
            "default": meta.get("default"),
            "value": VALUES.get(key, meta.get("default")),
        }
        if key in schema_mod.CHOICE_KEYS:
            field["options"] = [{"value": "", "label": "不指定"}] + SAMPLE_PROVIDERS.get(key, [])
        elif key in schema_mod.STATIC_OPTIONS:
            field["options"] = schema_mod.STATIC_OPTIONS[key]
        groups.setdefault(schema_mod.group_of(key), []).append(field)
    return {
        "diagnostics": {"version": "0.1.8", "providers": 14, "embeddings": 1},
        "groups": [
            {"id": gid, "title": title, "desc": desc, "fields": groups.get(gid, [])}
            for gid, title, desc in schema_mod.GROUPS
            if groups.get(gid)
        ]
    }


def payload(kind: str, body: dict) -> dict:
    if kind == "scopes":
        return {"items": [{"scope": SCOPE, "label": "私聊 · 3587BDB1966C", "total": 4,
                           "persona_version": PERSONA["version"]}]}
    if kind == "overview":
        return {"scope": SCOPE, "items": [
            {"label": "有效记忆", "value": 4},
            {"label": "待确认", "value": 1, "warn": True},
            {"label": "已取代", "value": 1},
            {"label": "平均置信度", "value": "84%"},
            {"label": "向量覆盖", "value": "67%"},
            {"label": "从未召回", "value": 1},
        ]}
    if kind == "facts":
        return {"items": FACTS, "total": len(FACTS)}
    if kind == "persona":
        return PERSONA
    if kind == "care":
        if body.get("enabled") is not None:
            CARE["enabled"] = bool(body["enabled"])
        return CARE
    if kind == "settings":
        return settings_payload()
    if kind == "logs":
        return {"items": LOGS, "stats": {"total": 7, "counts": {"INFO": 5, "WARN": 1, "ERROR": 1},
                                         "bytes": 4211, "days": 7}}
    return {}


MOCK = """<script>
(function () {
  var DATA = %DATA%;
  var listeners = {};
  window.AstrBotPluginView = window.AstrBotPluginPage = {
    ready: function () {
      return Promise.resolve({pluginName: "astrbot_plugin_companion",
        displayName: "陪伴记忆", pageName: "companion", pageTitle: "陪伴记忆",
        locale: "zh-CN", i18n: {}, isDark: %DARK%});
    },
    apiGet: function (endpoint, params) {
      var key = (endpoint || "").replace(/^\\/+/, "").split("?")[0];
      return Promise.resolve(DATA[key] || {});
    },
    apiPost: function (endpoint, body) {
      var key = (endpoint || "").replace(/^\\/+/, "");
      if (key === "care") return Promise.resolve(DATA.care_after_toggle || DATA.care);
      return Promise.resolve({ok: true});
    },
    download: function () { return Promise.resolve(); },
    upload: function () { return Promise.resolve({}); },
    subscribeSSE: function () { return Promise.resolve(); },
    onContext: function () { return function () {}; },
    getContext: function () { return {}; },
    getLocale: function () { return "zh-CN"; },
    getI18n: function () { return {}; },
    t: function (k, f) { return f; }
  };
})();
</script>
"""


class Handler(SimpleHTTPRequestHandler):
    dark = False
    tab = "facts"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=VIEW_DIR, **kwargs)

    def log_message(self, fmt, *args):  # noqa: A003 - 安静点
        pass

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        dark = "dark=1" in (parsed.query or "") or self.dark
        if path in ("/", "/index.html"):
            with open(os.path.join(VIEW_DIR, "index.html"), encoding="utf-8") as handle:
                html = handle.read()
            # 用 replace 而不是 % 格式化：MOCK 是一段 JS，里面迟早会出现 % 字符。
            mock = MOCK.replace("%DATA%", json.dumps({
                "scopes": payload("scopes", {}),
                "overview": payload("overview", {}),
                "facts": payload("facts", {}),
                "persona": payload("persona", {}),
                "care": payload("care", {}),
                "logs": payload("logs", {}),
                "settings": payload("settings", {}),
            }, ensure_ascii=False)).replace("%DARK%", "true" if dark else "false")
            html = html.replace("</head>", mock + "</head>")
            raw = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        super().do_GET()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8140)
    parser.add_argument("--dark", action="store_true")
    args = parser.parse_args()
    Handler.dark = args.dark
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"预览: http://127.0.0.1:{args.port}/index.html?tab=facts"
          f"{'&dark=1' if args.dark else ''}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
