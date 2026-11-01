"""配置解析：整个插件只有这一处知道配置项的名字和默认值。

上一版把默认值散在 55 个 `self._get("key", default)` 调用里，改一个默认值要
grep 全仓库；这里一次性把 dict 收成 dataclass，其余模块只读字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

TRAIT_KEYS = ("intimacy", "playfulness", "directness", "initiative", "warmth")

DEFAULT_BASELINE = {k: 0.5 for k in TRAIT_KEYS}


def _bool(raw: Any, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in {"1", "true", "yes", "on"}:
            return True
        if low in {"0", "false", "no", "off"}:
            return False
    if isinstance(raw, (int, float)):
        return bool(raw)
    return default


def _int(raw: Any, default: int, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _float(raw: Any, default: float, low: float, high: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN
        return default
    return max(low, min(high, value))


def _str(raw: Any, default: str) -> str:
    if raw is None:
        return default
    text = str(raw).strip()
    return text if text else default


@dataclass
class Config:
    # basic
    enabled: bool = True
    db_path: str = ""
    top_k: int = 6
    max_inject_chars: int = 600
    max_buffer: int = 40

    # flush
    flush_turns: int = 6
    idle_seconds: int = 90

    # recall
    min_confidence: float = 0.45
    max_per_slot: int = 2
    max_item_chars: int = 80
    include_suspect: bool = False

    # persona
    persona_inject: bool = True
    persona_evolve: bool = True
    persona_max_chars: int = 160
    max_step: float = 0.03
    persona_daily_budget: float = 0.15
    persona_half_life_days: float = 7.0
    baseline: dict = field(default_factory=lambda: dict(DEFAULT_BASELINE))

    # semantic
    semantic_recall: bool = True
    embedding_provider_id: str = ""
    embedding_timeout: int = 10

    # care
    care_enable: bool = False
    care_min_idle_minutes: int = 90
    care_daily_max: int = 3
    care_max_unanswered: int = 2
    care_quiet_start: str = "23:30"
    care_quiet_end: str = "08:00"
    care_schedule: str = ""
    care_probability: float = 0.85
    care_max_chars: int = 45
    care_timezone: str = "Asia/Shanghai"
    care_provider_id: str = ""
    care_split: bool = True
    care_split_max: int = 2
    care_split_delay: float = 1.2

    # model
    extract_provider_id: str = ""
    llm_timeout: int = 45

    # journal
    log_level: str = "INFO"
    log_days: int = 7
    log_mirror: bool = True

    @classmethod
    def load(cls, raw: dict | None) -> "Config":
        raw = raw or {}
        g = raw.get
        cfg = cls(
            enabled=_bool(g("enabled"), True),
            db_path=_str(g("db_path"), ""),
            top_k=_int(g("top_k"), 6, 1, 20),
            max_inject_chars=_int(g("max_inject_chars"), 600, 100, 4000),
            max_buffer=_int(g("max_buffer"), 40, 4, 400),
            flush_turns=_int(g("flush_turns"), 6, 1, 50),
            idle_seconds=_int(g("idle_seconds"), 90, 10, 3600),
            min_confidence=_float(g("min_confidence"), 0.45, 0.0, 1.0),
            max_per_slot=_int(g("max_per_slot"), 2, 1, 5),
            max_item_chars=_int(g("max_item_chars"), 80, 10, 300),
            include_suspect=_bool(g("include_suspect"), False),
            persona_inject=_bool(g("persona_inject"), True),
            persona_evolve=_bool(g("persona_evolve"), True),
            persona_max_chars=_int(g("persona_max_chars"), 160, 20, 600),
            max_step=_float(g("max_step"), 0.03, 0.0, 0.2),
            persona_daily_budget=_float(g("persona_daily_budget"), 0.15, 0.0, 1.0),
            persona_half_life_days=_float(g("persona_half_life_days"), 7.0, 0.5, 90.0),
            semantic_recall=_bool(g("semantic_recall"), True),
            embedding_provider_id=_str(g("embedding_provider_id"), ""),
            embedding_timeout=_int(g("embedding_timeout"), 10, 1, 60),
            care_enable=_bool(g("care_enable"), False),
            care_min_idle_minutes=_int(g("care_min_idle_minutes"), 90, 5, 1440),
            care_daily_max=_int(g("care_daily_max"), 3, 0, 20),
            care_max_unanswered=_int(g("care_max_unanswered"), 2, 0, 10),
            care_quiet_start=_str(g("care_quiet_start"), "23:30"),
            care_quiet_end=_str(g("care_quiet_end"), "08:00"),
            care_schedule=_str(g("care_schedule"), ""),
            care_probability=_float(g("care_probability"), 0.85, 0.0, 1.0),
            care_max_chars=_int(g("care_max_chars"), 45, 10, 200),
            care_timezone=_str(g("care_timezone"), "Asia/Shanghai"),
            care_provider_id=_str(g("care_provider_id"), ""),
            care_split=_bool(g("care_split"), True),
            care_split_max=_int(g("care_split_max"), 2, 1, 4),
            care_split_delay=_float(g("care_split_delay"), 1.2, 0.0, 10.0),
            extract_provider_id=_str(g("extract_provider_id"), ""),
            llm_timeout=_int(g("llm_timeout"), 45, 5, 180),
            log_level=_str(g("log_level"), "INFO").upper(),
            log_days=_int(g("log_days"), 7, 1, 90),
            log_mirror=_bool(g("log_mirror"), True),
        )
        cfg.baseline = _baseline(g("baseline"), cfg.baseline_hint())
        return cfg

    @staticmethod
    def baseline_hint() -> dict:
        return dict(DEFAULT_BASELINE)

    def as_dict(self) -> dict:
        return asdict(self)

    def care_block(self) -> dict:
        """给 care 决策用的子配置，字段名与 care.decide 对齐。"""
        return {
            "enabled": self.care_enable,
            "min_idle_minutes": self.care_min_idle_minutes,
            "daily_max": self.care_daily_max,
            "max_unanswered": self.care_max_unanswered,
            "quiet_start": self.care_quiet_start,
            "quiet_end": self.care_quiet_end,
            "schedule": self.care_schedule,
            "probability": self.care_probability,
            "max_chars": self.care_max_chars,
            "timezone": self.care_timezone,
        }


def _baseline(raw: Any, default: dict) -> dict:
    result = dict(default)
    if not isinstance(raw, dict):
        return result
    for key in TRAIT_KEYS:
        if key in raw:
            result[key] = _float(raw.get(key), default[key], 0.0, 1.0)
    return result
