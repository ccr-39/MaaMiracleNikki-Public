"""Pre-click cadence for alliance business actions only.

Callers validate the page and target before invoking this helper. Existing
post-click waits, polling and common transient handling remain with callers.
"""

from __future__ import annotations

import random
from typing import Callable


ALLIANCE_PRE_CLICK_DELAY_MIN = 0.6
ALLIANCE_PRE_CLICK_DELAY_MAX = 0.9


def wait_before_alliance_click(
    session,
    *,
    delay_sampler: Callable[[float, float], float] | None = None,
) -> float:
    sampler = delay_sampler or random.uniform
    delay = float(sampler(ALLIANCE_PRE_CLICK_DELAY_MIN, ALLIANCE_PRE_CLICK_DELAY_MAX))
    if not ALLIANCE_PRE_CLICK_DELAY_MIN <= delay <= ALLIANCE_PRE_CLICK_DELAY_MAX:
        raise ValueError(f"Alliance pre-click delay out of range: {delay}")
    session.sleep(delay)
    return delay


def alliance_business_click(session, x: int, y: int, *, confirm=None):
    wait_before_alliance_click(session)
    if confirm is not None:
        target = confirm()
        if target is None:
            return False
        x, y = target.center
    return session.click(x, y) is not False
