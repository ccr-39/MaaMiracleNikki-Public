"""Competition settlement hours use the game's China time zone."""
from datetime import datetime, timedelta, timezone

GAME_TIMEZONE = timezone(timedelta(hours=8))
COMPETITION_TIME_NOTICE = "周二、周六 02:00–10:00 暂停参赛和点赞，10 点后开启点赞（北京时间）。"


class CompetitionTimeRestricted(Exception):
    pass


def competition_time_restricted(moment: datetime | None = None) -> bool:
    current = moment if moment is not None else datetime.now(GAME_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=GAME_TIMEZONE)
    else:
        current = current.astimezone(GAME_TIMEZONE)
    return current.weekday() in {1, 5} and 2 <= current.hour < 10
