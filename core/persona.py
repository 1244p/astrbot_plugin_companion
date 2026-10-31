"""人格：五维慢变量。

两个约束决定这里的数学：

1. 步长按**真实时间**配速，不按对话轮数。上一版按每次抽取限速，用户把
   `flush_turns` 调小之后一天能推上百次，几小时五维顶满。
2. 每天有总预算。时间配速只管单次，管不住「一天抽五十次」这种密度，
   所以再加一道当日累计上限；预算只算模型推力，半衰期回归不占额度，
   否则离基线越远，回得越多、越没额度可用。
"""

from __future__ import annotations

import json

from .config import TRAIT_KEYS, Config

DEFAULT_MAX_STEP = 0.03
DEFAULT_DAILY_BUDGET = 0.15
DEFAULT_HALF_LIFE_DAYS = 7.0


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def move_step(max_step: float, hours: float, daily_used: float = 0.0,
              daily_budget: float = DEFAULT_DAILY_BUDGET) -> float:
    """单次位移上限 = min(单次硬上限, 时间配速, 当日余额)。"""
    if max_step <= 0:
        return 0.0
    paced = max_step * min(1.0, max(0.0, hours) / 24.0)
    remain = max(0.0, daily_budget - max(0.0, daily_used)) if daily_budget > 0 else paced
    return max(0.0, min(max_step, paced, remain))


def relax_factor(hours: float, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """向基线回归的比例：半衰期到点走一半。"""
    if half_life_days <= 0:
        return 1.0
    span = half_life_days * 24.0
    return 1.0 - 0.5 ** (max(0.0, hours) / span)


def apply_deltas(
    traits: dict,
    deltas: dict,
    cfg: Config,
    baseline: dict,
    hours: float = 24.0,
    daily_used: float = 0.0,
) -> tuple[dict, float, dict]:
    """返回 (新五维, 本次实际花掉的预算, 每维实际位移)。

    预算必须**五维共享**：早期版本把它当成「每维可用余额」，五维各花剩余额度，
    一天能花出去五倍预算。所以这里先按单次上限截，再按剩余总额等比缩。
    另外单次最多花掉当日预算的三分之一——否则一次抽取就能把一天用完。

    第三个返回值是**实际**位移，要写进 persona_log：模型要了 +4、实际只加了 +0.0001
    的时候，时间线上必须显示 0，否则面板上的数字和实际人格对不上。
    """
    budget = cfg.persona_daily_budget
    step = move_step(cfg.max_step, hours, daily_used, budget)
    relax = relax_factor(hours, cfg.persona_half_life_days)
    remain_total = max(0.0, budget - max(0.0, daily_used))
    single_cap = remain_total if budget <= 0 else min(remain_total, budget / 3.0)

    wanted_by_trait = {}
    pulled_by_trait = {}
    for key in TRAIT_KEYS:
        base = clamp(float(baseline.get(key, 0.5)))
        current = clamp(float(traits.get(key, base)))
        pulled_by_trait[key] = current + (base - current) * relax
        try:
            wanted = float(deltas.get(key, 0.0))
        except (TypeError, ValueError):
            wanted = 0.0
        wanted_by_trait[key] = max(-step, min(step, wanted))

    total = sum(abs(value) for value in wanted_by_trait.values())
    scale = 1.0
    if total > single_cap:
        scale = (single_cap / total) if total > 0 else 0.0

    out = {}
    applied = {}
    spent = 0.0
    for key in TRAIT_KEYS:
        delta = wanted_by_trait[key] * scale
        out[key] = round(clamp(pulled_by_trait[key] + delta), 4)
        applied[key] = round(delta, 4)
        spent += abs(delta)
    return out, round(spent, 4), applied


def render_block(traits: dict, baseline: dict, max_chars: int) -> str:
    labels = {
        "intimacy": "亲密度",
        "playfulness": "玩心",
        "directness": "直率",
        "initiative": "主动",
        "warmth": "温度",
    }
    parts = []
    for key in TRAIT_KEYS:
        value = clamp(float(traits.get(key, 0.5)))
        base = clamp(float(baseline.get(key, 0.5)))
        diff = value - base
        if abs(diff) < 0.03:
            continue
        tone = "偏高" if diff > 0 else "偏低"
        parts.append(f"{labels[key]}{tone}{abs(diff) * 100:.0f}%")
    if not parts:
        return ""
    text = "、".join(parts)
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


def parse_traits(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, value in data.items():
        if key in TRAIT_KEYS:
            try:
                out[key] = clamp(float(value))
            except (TypeError, ValueError):
                continue
    return out
