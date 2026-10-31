"""事件日志：一行一个 JSON，按天分文件，超限就地切一份。

为什么要自己写：AstrBot 的 logger 是全局单例，插件只能往里打，用户没法按
来源过滤。出问题时他要的是「我这个插件这半小时干了什么」。
"""

from __future__ import annotations

import json
import os
import threading
import time

LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
MAX_BYTES = 2 * 1024 * 1024


class Journal:
    def __init__(self, root: str, level: str = "INFO", days: int = 7,
                 max_bytes: int = MAX_BYTES):
        self.root = root
        self.level = (level or "INFO").upper()
        self.days = max(1, int(days))
        self.max_bytes = max(64 * 1024, int(max_bytes))
        self._lock = threading.Lock()
        os.makedirs(root, exist_ok=True)

    # ---------- 写 ----------

    def enabled(self, level: str) -> bool:
        return LEVELS.get(str(level).upper(), 20) >= LEVELS.get(self.level, 20)

    def path_for(self, stamp: float) -> str:
        name = time.strftime("companion-%Y%m%d.log", time.localtime(stamp))
        return os.path.join(self.root, name)

    def write(self, level: str, event: str, message: str = "", **fields) -> dict | None:
        level = str(level).upper()
        if not self.enabled(level):
            return None
        record = {
            "ts": round(time.time(), 3),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "event": event,
            "msg": str(message or ""),
        }
        for key, value in fields.items():
            if value is None or value == "":
                continue
            record[key] = value
        line = json.dumps(record, ensure_ascii=False)
        try:
            with self._lock:
                path = self.path_for(record["ts"])
                self._rotate(path)
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(line + chr(10))
                self._prune(record["ts"])
        except OSError:
            return None
        return record

    def _rotate(self, path: str) -> None:
        try:
            if os.path.getsize(path) < self.max_bytes:
                return
        except OSError:
            return
        backup = path + ".1"
        try:
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(path, backup)
        except OSError:
            pass

    def _prune(self, stamp: float) -> None:
        cutoff = stamp - self.days * 86400
        try:
            names = os.listdir(self.root)
        except OSError:
            return
        for name in names:
            if not name.startswith("companion-"):
                continue
            path = os.path.join(self.root, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                continue

    # ---------- 读 ----------

    def files(self) -> list[str]:
        try:
            names = sorted(n for n in os.listdir(self.root) if n.startswith("companion-"))
        except OSError:
            return []
        return [os.path.join(self.root, n) for n in names]

    def tail(self, limit: int = 50, level: str = "", query: str = "", event: str = "") -> list[dict]:
        limit = max(1, min(500, int(limit)))
        floor = LEVELS.get(str(level).upper(), 0) if level else 0
        needle = (query or "").lower()
        prefix = (event or "").lower()
        collected: list[dict] = []
        for path in reversed(self.files()):
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    lines = handle.readlines()
            except OSError:
                continue
            for line in reversed(lines):
                record = self._parse(line)
                if record is None:
                    continue
                if floor and LEVELS.get(record.get("level", ""), 20) < floor:
                    continue
                if prefix and not str(record.get("event", "")).lower().startswith(prefix):
                    continue
                if needle and needle not in line.lower():
                    continue
                collected.append(record)
                if len(collected) >= limit:
                    return collected
        return collected

    def stats(self) -> dict:
        counts = {name: 0 for name in LEVELS}
        events: dict[str, int] = {}
        size = 0
        total = 0
        for path in self.files():
            try:
                size += os.path.getsize(path)
                with open(path, encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        record = self._parse(line)
                        if record is None:
                            continue
                        total += 1
                        counts[record.get("level", "INFO")] = counts.get(
                            record.get("level", "INFO"), 0
                        ) + 1
                        name = str(record.get("event", "")).split(".")[0]
                        events[name] = events.get(name, 0) + 1
            except OSError:
                continue
        return {
            "total": total,
            "counts": counts,
            "events": dict(sorted(events.items(), key=lambda kv: kv[1], reverse=True)[:12]),
            "bytes": size,
            "level": self.level,
            "days": self.days,
        }

    def clear(self) -> int:
        removed = 0
        for path in self.files():
            try:
                os.remove(path)
                removed += 1
            except OSError:
                continue
        return removed

    @staticmethod
    def _parse(line: str) -> dict | None:
        line = line.strip()
        if not line:
            return None
        try:
            data = json.loads(line)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def text_line(record: dict, width: int = 120) -> str:
        parts = [
            str(record.get("time", ""))[11:],
            str(record.get("level", ""))[:1],
            str(record.get("event", "")),
            str(record.get("msg", "")),
        ]
        extra = {
            k: v
            for k, v in record.items()
            if k not in {"ts", "time", "level", "event", "msg"}
        }
        if extra:
            parts.append(" ".join(f"{k}={v}" for k, v in extra.items()))
        line = " ".join(p for p in parts if p)
        return line if len(line) <= width else line[: width - 1] + "…"
