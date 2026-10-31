"""主动开口：闸门式判定。

每个闸门「为什么不过」本身就是给用户看的原因，所以判定和解释必须是同一份
数据：`GATES` 里按顺序跑，第一个不通过的闸门就是原因，`/care why` 直接引用
同一份结果，不会出现「日志说 A、面板说 B」。
"""

from __future__ import annotations

import random
from datetime import datetime, time as daytime, timedelta, timezone

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

REASONS = {
    "disabled": "总开关没开",
    "muted": "这个会话关了",
    "quiet_hours": "免打扰时段",
    "outside_schedule": "不在允许时段",
    "too_soon": "还不到时候",
    "daily_cap": "今天发够了",
    "unanswered_cap": "连发没回，等你先说话",
    "probability": "这轮概率没抽中",
    "no_prompt": "没攒出话题",
    "forced": "手动触发",
    "ready": "可以发",
}

SENT_END = "。！？!?…~～"
BLOCK_SEP = chr(10)


def zone(tz_name: str):
    if ZoneInfo is None:
        return timezone(timedelta(hours=8))
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone(timedelta(hours=8))


def local_now(tz_name: str) -> datetime:
    return datetime.now(zone(tz_name))


def parse_hhmm(text: str, default: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    try:
        hour, _, minute = str(text).strip().partition(":")
        value = int(hour) * 60 + int(minute or 0)
    except (TypeError, ValueError):
        return default
    if not 0 <= value < 24 * 60:
        return default
    return divmod(value, 60)


def _minutes(text: str) -> int | None:
    hour, _, minute = str(text).strip().partition(":")
    try:
        total = int(hour) * 60 + int(minute)
    except (TypeError, ValueError):
        return None
    return total if 0 <= total < 24 * 60 else None


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    s, e = _minutes(start), _minutes(end)
    if s is None or e is None or s == e:
        return False
    current = now.hour * 60 + now.minute
    if s < e:
        return s <= current < e
    return current >= s or current < e


def parse_schedule(text: str) -> list[tuple[int, int]]:
    windows = []
    for piece in str(text or "").replace("；", ",").replace("，", ",").split(","):
        piece = piece.strip()
        if "-" not in piece:
            continue
        left, _, right = piece.partition("-")
        start, end = _minutes(left), _minutes(right)
        if start is None or end is None or start == end:
            continue
        windows.append((start, end))
    return windows


def in_schedule(now: datetime, windows: list[tuple[int, int]]) -> bool:
    if not windows:
        return True
    current = now.hour * 60 + now.minute
    for start, end in windows:
        if start < end:
            if start <= current < end:
                return True
        elif current >= start or current < end:
            return True
    return False


def day_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def split_sentences(text: str, max_parts: int = 2) -> list[str]:
    """先按空行/换行切块，块内再按句末标点切，最后合并到 max_parts。"""
    text = (text or "").strip()
    if not text:
        return []
    max_parts = max(1, int(max_parts))
    blocks = [b.strip() for b in text.splitlines() if b.strip()]
    pieces: list[tuple[str, bool]] = []  # (文本, 是否块首)
    for block in blocks:
        buffer = ""
        first = True
        for char in block:
            buffer += char
            if char in SENT_END and buffer.strip():
                pieces.append((buffer.strip(), first))
                buffer = ""
                first = False
        if buffer.strip():
            pieces.append((buffer.strip(), first))
    if not pieces:
        return [text]
    while len(pieces) > max_parts:
        tail_text, _ = pieces.pop()
        prev_text, prev_first = pieces.pop()
        sep = BLOCK_SEP if not tail_text.startswith(prev_text[:1]) else ""
        pieces.append((prev_text + (BLOCK_SEP if prev_first and not tail_text else "") + tail_text,
                       prev_first))
    return [p[0] for p in pieces if p[0].strip()]


def sanitize_reply(data: dict, max_chars: int = 45) -> tuple[str, int] | None:
    if not isinstance(data, dict):
        return None
    if data.get("send") is False:
        return None
    text = str(data.get("text") or "").strip().replace(chr(10), " ")
    if not text:
        return None
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    try:
        gap = int(data.get("gap", 60))
    except (TypeError, ValueError):
        gap = 60
    return text, max(15, min(240, gap))


def gate_detail(now: datetime, state: dict, cfg: dict) -> list[dict]:
    """按顺序算每个闸门，返回全部结果（含未通过的），给 why 用。"""
    last_active = float(state.get("last_active") or 0)
    last_sent = float(state.get("last_sent") or 0)
    min_idle = int(cfg.get("min_idle_minutes", 90))
    gap = int(state.get("next_gap") or 0)
    idle_minutes = (now.timestamp() - last_active) / 60 if last_active else None
    since_sent = (now.timestamp() - last_sent) / 60 if last_sent else None
    # 没有记录时不能拿 1e9 当分钟数显示出去——面板上会出现「1000000000 分钟」。
    if idle_minutes is None:
        idle_text = f"还没有聊过 / 要求 {min_idle}"
    else:
        idle_text = f"空闲 {idle_minutes:.0f} 分钟 / 要求 {min_idle}"
    if since_sent is None:
        gap_text = "还没主动说过话"
    elif gap <= 0:
        gap_text = f"距上次主动 {since_sent:.0f} 分钟 / 不限间隔"
    else:
        gap_text = f"距上次主动 {since_sent:.0f} 分钟 / 间隔 {gap}"
    return [
        {
            "key": "idle",
            "ok": idle_minutes is None or idle_minutes >= min_idle,
            "detail": idle_text,
        },
        {
            "key": "gap",
            "ok": since_sent is None or since_sent >= gap,
            "detail": gap_text,
        },
    ]


def decide(now: datetime, state: dict, cfg: dict, rng: random.Random | None = None,
           force: bool = False) -> tuple[bool, str]:
    if not cfg.get("enabled"):
        return False, "disabled"
    if not state.get("enabled", 1):
        return False, "muted"
    if in_quiet_hours(now, cfg.get("quiet_start", "23:30"), cfg.get("quiet_end", "08:00")):
        return False, "quiet_hours"
    if not in_schedule(now, parse_schedule(cfg.get("schedule", ""))):
        return False, "outside_schedule"
    if state.get("unanswered", 0) >= int(cfg.get("max_unanswered", 2)):
        return False, "unanswered_cap"
    if force:
        return True, "forced"

    for gate in gate_detail(now, state, cfg):
        if not gate["ok"]:
            return False, "too_soon"
    if day_key(now) == state.get("sent_day") and int(state.get("sent_today", 0)) >= int(
        cfg.get("daily_max", 3)
    ):
        return False, "daily_cap"
    picker = rng or random.Random()
    if picker.random() > float(cfg.get("probability", 0.85)):
        return False, "probability"
    return True, "ready"


def reason_text(reason: str) -> str:
    return REASONS.get(reason, reason)


def next_at(state: dict, cfg: dict) -> tuple[float, str]:
    """下一次最早能发的时间戳与依据。"""
    last_sent = float(state.get("last_sent") or 0)
    last_active = float(state.get("last_active") or 0)
    gap_at = last_sent + int(state.get("next_gap") or 0) * 60 if last_sent else 0
    idle_at = last_active + int(cfg.get("min_idle_minutes", 90)) * 60 if last_active else 0
    if gap_at >= idle_at and gap_at:
        return gap_at, "gap"
    if idle_at:
        return idle_at, "idle"
    return 0.0, "never_sent"
