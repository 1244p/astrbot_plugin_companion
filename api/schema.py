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
    "enabled": "总开关",
    "db_path": "记忆库路径",
    "max_buffer": "待抽取缓冲上限",
    "include_suspect": "注入待确认条目",
    "embedding_timeout": "嵌入超时（秒）",
    "llm_timeout": "抽取超时（秒）",
    "log_level": "日志级别",
    "log_days": "日志保留天数",
    "log_mirror": "同步到框架日志",
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


# 改了这些要重启插件才生效（换库存路径等于换一个库）
RESTART_FIELDS = {"db_path"}

TYPE_NAMES = {"int": "整数", "float": "小数", "bool": "开或关", "string": "文本"}


def _coerce(kind: str, raw):
    """把表单来的值转成 schema 声明的类型；转不动就抛 ValueError。"""
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        text = str(raw).strip().lower()
        if text in ("1", "true", "on", "yes"):
            return True
        if text in ("0", "false", "off", "no", ""):
            return False
        raise ValueError("不是开关值")
    if kind == "int":
        if isinstance(raw, bool):
            raise ValueError("不是整数")
        return int(str(raw).strip()) if isinstance(raw, str) else int(raw)
    if kind == "float":
        if isinstance(raw, bool):
            raise ValueError("不是小数")
        return float(str(raw).strip()) if isinstance(raw, str) else float(raw)
    if raw is None:
        return ""
    return str(raw)


def validate(schema: dict, payload: dict) -> tuple[dict, dict]:
    """只校验表单里带来的键；返回 (要写的值, 每个字段的错误)。"""
    values: dict = {}
    errors: dict = {}
    for key, meta in schema.items():
        if key == "baseline" or key not in payload:
            continue
        kind = meta.get("type", "string")
        try:
            values[key] = _coerce(kind, payload[key])
        except (TypeError, ValueError):
            errors[key] = f"需要{TYPE_NAMES.get(kind, '有效值')}"
    return values, errors


EMPTY_LABELS = {
    "extract_provider_id": "不指定（跟当前会话用同一个模型）",
    "embedding_provider_id": "不指定（不存向量）",
}

# 枚举型配置项：选项写在这里，前端就当 select 渲染
STATIC_OPTIONS = {
    "log_level": [{"value": "DEBUG", "label": "DEBUG"}, {"value": "INFO", "label": "INFO"},
                  {"value": "WARN", "label": "WARN"}, {"value": "ERROR", "label": "ERROR"}],
}

CHOICE_KEYS = {
    "extract_provider_id": "provider_choices",
    "embedding_provider_id": "embedding_choices",
}


def known_values(plugin, key: str) -> list[str]:
    """真实存在的提供商 ID 列表（不含「当前值」这种为了显示而塞进去的项）。"""
    getter = getattr(plugin, CHOICE_KEYS.get(key, ""), None)
    if not callable(getter):
        return []
    try:
        return [str(item.get("value", "")) for item in (getter() or []) if item.get("value")]
    except Exception:  # noqa: BLE001
        return []


def field_choices(plugin, key: str, value) -> list[dict] | None:
    """给「提供商 ID」这类字段补上下拉选项；插件拿不到列表时返回 None（退回输入框）。"""
    getter = getattr(plugin, CHOICE_KEYS.get(key, ""), None)
    if not callable(getter):
        return None
    try:
        items = getter() or []
    except Exception:  # noqa: BLE001
        return None
    if not items:
        return None
    options = [{"value": "", "label": EMPTY_LABELS.get(key, "不指定")}]
    options.extend(items)
    current = str(value or "")
    if current and current not in {item["value"] for item in items}:
        # 已经存了错值：原样列出来并标红提示，别让下拉静默显示成「不指定」
        options.insert(1, {"value": current, "label": f"{current}（没有这个提供商）"})
    return options


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
        if key in CHOICE_KEYS:
            options = field_choices(plugin, key, field["value"])
            if options:
                field["options"] = options
        elif key in STATIC_OPTIONS:
            field["options"] = STATIC_OPTIONS[key]
        groups.setdefault(group_of(key), []).append(field)
    return {
        "groups": [
            {"id": gid, "title": title, "desc": desc, "fields": groups.get(gid, [])}
            for gid, title, desc in GROUPS
            if groups.get(gid)
        ],
        "baseline": cfg.baseline,
    }
