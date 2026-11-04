"""Local daily schedule; no background service or catch-up queue."""
from datetime import datetime
import re


def parse_times(value):
    if not isinstance(value, str):
        raise ValueError("定时时间必须为文本")
    times = [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]
    if not 1 <= len(times) <= 8 or any(
            not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", part) for part in times):
        raise ValueError("请填写 1–8 个 HH:mm 时间，例如 08:00,20:00")
    return tuple(sorted(set(times)))


class DailySchedule:
    def __init__(self):
        self._seen = set()

    def claim(self, config, now: datetime):
        if not config.schedule_enabled:
            return False
        minute = now.strftime("%H:%M")
        if minute not in parse_times(config.schedule_times):
            return False
        key = (now.date(), minute)
        if key in self._seen:
            return False
        # Also consume busy/invalid attempts, so a task finishing during the
        # same minute cannot cause a second queue. Only process memory is used.
        self._seen.add(key)
        self._seen = {item for item in self._seen if item[0] >= now.date()}
        return True
