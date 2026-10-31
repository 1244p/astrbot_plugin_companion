"""抽取提示词与容错解析。

模型输出的三种失败模式都在这处理：不返回 JSON、返回了但字段形状不对、
条目内容超出阈值。每一种都要能说出「为什么这条没存」，否则用户只能看到
「记忆 0 条」。
"""

from __future__ import annotations

import json
import re

from .config import TRAIT_KEYS

FACT_SYSTEM = "你是记忆整理器。只输出 JSON，不解释。"

FACT_PROMPT = """从下面的对话里抽出值得长期记住的事实。

规则：
1. 只记用户本人的稳定信息（偏好、习惯、身份、关系、在办的事、明确表态），不记助手说过的话。
2. slot 是归一化槽位，同类信息必须用同一个词：饮食、作息、工作、居住、健康、家庭、兴趣、称呼、禁忌、在办事项。
3. 状态变了（以前那样、现在这样）要用**相同的 slot** 输出新值，否则旧值盖不掉。
4. polarity：喜欢 1，不喜欢 -1，中性 0；importance 1-5；confidence 0-1，推测不要超过 0.6。
5. 没有可记的就输出 {"facts": []}。

已记住（不用重复）：
__KNOWN__

对话：
__CONV__

输出格式：
{"facts": [{"slot": "饮食", "content": "不吃香菜", "kind": "preference", "polarity": -1, "importance": 4, "confidence": 0.9}]}"""

PERSONA_SYSTEM = "你是角色状态评估器。只输出 JSON。"

PERSONA_PROMPT = """根据这段对话，评估用户希望助手此刻呈现的状态，五个维度各给 -0.06~0.06 的增量。

维度：intimacy 亲密度、playfulness 玩心、directness 直率、initiative 主动、warmth 温度。

没有明确信号就给 0。不要每轮都给正数，情绪平稳时全 0 是正确输出。

对话：
__CONV__

输出格式：
{"deltas": {"intimacy": 0.0, "playfulness": 0.0, "directness": 0.0, "initiative": 0.0, "warmth": 0.0}, "reason": "一句话说明依据"}"""

KINDS = {"preference", "profile", "fact", "event", "task"}
MAX_CONTENT = 300


def extract_json(text: str) -> dict | None:
    """从模型输出里挖出第一个 JSON 对象，容忍 ```json 围栏和前后废话。"""
    if not text:
        return None
    body = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", body, re.S)
    if fenced:
        body = fenced.group(1).strip()
    try:
        data = json.loads(body)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    start = body.find("{")
    while start != -1:
        depth = 0
        for index in range(start, len(body)):
            char = body[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(body[start : index + 1])
                        return data if isinstance(data, dict) else None
                    except ValueError:
                        break
        start = body.find("{", start + 1)
    return None


def fact_prompt(conversation: str, known: list[str]) -> str:
    known_text = "\n".join(f"- [{slot}] {content}" for slot, content in known) or "（无）"
    return (FACT_PROMPT.replace("__KNOWN__", known_text)
            .replace("__CONV__", conversation))


def persona_prompt(conversation: str) -> str:
    return PERSONA_PROMPT.replace("__CONV__", conversation)


def sanitize_facts(raw: dict | None, max_facts: int = 8) -> tuple[list[dict], list[str]]:
    """返回 (可用条目, 丢弃原因)。丢弃原因直接进日志和预览。"""
    if raw is None:
        return [], ["模型没返回 JSON"]
    items = raw.get("facts")
    if not isinstance(items, list):
        if items is None:
            return [], ["返回里没有 facts 字段"]
        return [], ["facts 不是数组"]
    kept: list[dict] = []
    dropped: list[str] = []
    for index, item in enumerate(items):
        if len(kept) >= max_facts:
            dropped.append(f"超过 {max_facts} 条，其余忽略")
            break
        if not isinstance(item, dict):
            dropped.append(f"第 {index + 1} 条不是对象")
            continue
        slot = str(item.get("slot") or "").strip()[:20]
        content = str(item.get("content") or "").strip().replace("\n", " ")
        if not slot or not content:
            dropped.append(f"第 {index + 1} 条缺 slot 或 content")
            continue
        if len(content) > MAX_CONTENT:
            dropped.append(f"第 {index + 1} 条 content 超过 {MAX_CONTENT} 字")
            continue
        kind = str(item.get("kind") or "fact").strip().lower()
        if kind not in KINDS:
            kind = "fact"
        kept.append(
            {
                "slot": slot,
                "content": content,
                "kind": kind,
                "importance": _clamp_int(item.get("importance"), 3, 1, 5),
                "confidence": _clamp_float(item.get("confidence"), 0.7, 0.0, 1.0),
                "polarity": _clamp_int(item.get("polarity"), 0, -1, 1),
            }
        )
    if not kept and not dropped:
        dropped.append("模型返回空数组")
    return kept, dropped


def sanitize_persona(raw: dict | None) -> tuple[dict, str]:
    if not isinstance(raw, dict):
        return {}, ""
    deltas = raw.get("deltas")
    if not isinstance(deltas, dict):
        return {}, ""
    out = {}
    for key, value in deltas.items():
        if key not in TRAIT_KEYS:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        out[str(key)] = max(-0.06, min(0.06, number))
    return out, str(raw.get("reason") or "").strip()[:120]


def _clamp_int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clamp_float(value, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return max(low, min(high, number))
