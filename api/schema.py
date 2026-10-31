"""把 `_conf_schema.json` 读成面板能渲染的结构。

分组不写死在代码里，按 key 前缀推：`care_*` → care，`persona_*` → persona。
这样加一个配置项只改 schema 一处。
"""

from __future__ import annotations

import json
import os

GROUPS = [
    ("basic", "基础", "总开关、库位置、注入长度"),
    ("recall", "召回", "注入门槛、同槽位条数、单条长度"),
    ("persona", "人格", "演化速度、每日预算、半衰期"),
    ("semantic", "语义", "向量召回与嵌入模型"),
    ("care", "主动消息", "空闲多久、每天几次、免打扰与时段"),
    ("model", "模型", "抽取用的提供商与超时"),
    ("journal", "日志", "级别、保留天数、是否同步到框架日志"),
]

PREFIX = {
    "enabled": "basic",
    "db_path": "basic",
    "top_k": "recall",
    "max_inject_chars": "basic",
    "max_buffer": "basic",
    "flush_turns": "basic",
    "idle_seconds": "basic",
    "min_confidence": "recall",
    "max_per_slot": "recall",
    "max_item_chars": "recall",
    "include_suspect": "recall",
    "semantic_recall": "semantic",
    "embedding_provider_id": "semantic",
    "embedding_timeout": "semantic",
    "extract_provider_id": "model",
    "llm_timeout": "model",
    "log_level": "journal",
    "log_days": "journal",
    "log_mirror": "journal",
}

TITLES = {
    "care_enable": "主动消息总开关",
    "care_min_idle_minutes": "空闲多少分钟才发",
    "care_daily_max": "每天最多几条",
    "care_max_unanswered": "连发几条没回就停",
    "care_quiet_start": "免打扰开始",
    "care_quiet_end": "免打扰结束",
    "care_schedule": "允许时段",
    "care_probability": "每轮发的概率",
    "care_max_chars": "单条字数上限",
    "care_timezone": "时区",
    "care_split": "拆成多条发",
    "care_split_max": "最多拆几条",
    "care_split_delay": "两条之间间隔秒",
    "max_step": "单次人格位移上限",
    "persona_daily_budget": "每日位移预算",
    "persona_half_life_days": "回基线半衰期（天）",
    "persona_evolve": "允许人格演化",
    "persona_inject": "注入人格倾向",
    "persona_max_chars": "人格注入字数",
    "flush_turns": "攒够几轮就抽取",
    "idle_seconds": "空闲几秒就抽取",
    "top_k": "召回条数",
    "min_confidence": "最低置信度",
    "max_per_slot": "同槽位上限",
    "max_item_chars": "单条展示字数",
    "max_inject_chars": "注入总字数",
    "semantic_recall": "启用向量召回",
    "embedding_provider_id": "嵌入提供商 ID",
    "extract_provider_id": "抽取模型 ID",
}


def schema_path() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "_conf_schema.json")


def load_schema(path: str = "") -> dict:
    path = path or schema_path()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def group_of(key: str) -> str:
    if key in PREFIX:
        return PREFIX[key]
    head = key.split("_", 1)[0]
    return head if head in {g[0] for g in GROUPS} else "basic"


def describe(plugin, path: str = "") -> dict:
    schema = load_schema(path)
    cfg = plugin.cfg
    groups: dict[str, list] = {}
    for key, meta in schema.items():
        if key == "baseline":
            continue
        field = {
            "key": key,
            "title": TITLES.get(key, key),
            "type": meta.get("type", "string"),
            "hint": meta.get("hint", ""),
            "default": meta.get("default"),
            "value": getattr(cfg, key, meta.get("default")),
        }
        groups.setdefault(group_of(key), []).append(field)
    return {
        "groups": [
            {"id": gid, "title": title, "desc": desc, "fields": groups.get(gid, [])}
            for gid, title, desc in GROUPS
            if groups.get(gid)
        ],
        "baseline": cfg.baseline,
    }
