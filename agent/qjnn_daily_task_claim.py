"""Bounded daily reward claims. Device access exists only in CustomAction.run."""
from __future__ import annotations

from app_paths import DATA_ROOT

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from qjnn_alliance_commission import Rect
from qjnn_common_popup import _make_runtime_session, poll_target_with_common_popups
from qjnn_diagnostic import DiagnosticFrameStore
from qjnn_dreamweaver_commission import CustomAction, _wait_before_click
from qjnn_reward import common_reward_target, drain_common_rewards, has_common_reward_title
from qjnn_world_tour import _find, scaled_rect

ROOT = Path(__file__).resolve().parent.parent
MAX_CLAIM_ROUNDS = 4


@dataclass(frozen=True)
class DailyTaskDetection:
    state: str
    safe_rect: Rect


@dataclass(frozen=True)
class Result:
    success: bool
    rounds: int
    reason: str


def detect_daily_task(items, frame):
    """Only title, footer control and its pixels matter; task rows are ignored."""
    if frame is None or getattr(frame, "size", 0) == 0 or has_common_reward_title(items):
        return None
    title = _find(items, frame, "每日任务", (280, 290, 350, 85))
    # OCR consistently drops the leading 一 in these supplied screenshots.
    button = (_find(items, frame, "一键领取", (350, 1200, 200, 90))
              or _find(items, frame, "键领取", (350, 1200, 200, 90)))
    if button is None:
        return None
    # Blank left interior of the same button, excluding text, rim and shadow.
    probe = scaled_rect((370, 1228, 25, 27), frame)
    pixels = frame[probe.y:probe.bottom, probe.x:probe.right].astype(np.int16)
    blue, green, red = (pixels[..., c] for c in range(3))
    pink = np.mean((red >= 210) & (red - green >= 65) & (red - blue >= 65))
    gray = np.mean((np.max(pixels, axis=2) - np.min(pixels, axis=2) <= 12)
                   & (red >= 130) & (red <= 200))
    if pink >= .85:
        return DailyTaskDetection("claimable", button) if title is not None else None
    if gray >= .85:
        if title is None:
            # Some game builds lose the panel/title textures after claiming.
            # Permit only closing a completed panel, never claiming from this
            # fallback: require the daily refresh notice, activity and rows.
            def normalized(item):
                return ''.join(item.text.split())
            notice=any(i.score>=.9 and '每日任务' in normalized(i)
                       and '竞技场次数' in normalized(i) and '早上5点刷' in normalized(i)
                       and .2*frame.shape[0]<i.box.y<.3*frame.shape[0]
                       for i in items)
            activity=any(i.score>=.9 and normalized(i).startswith('今日活跃度')
                         and .7*frame.shape[1]<i.box.x and .15*frame.shape[0]<i.box.y<.25*frame.shape[0]
                         for i in items)
            rows=sum(i.score>=.9 and normalized(i)=='马上前往'
                     and .7*frame.shape[1]<i.box.x and .38*frame.shape[0]<i.box.y<.7*frame.shape[0]
                     for i in items)
            if not (notice and activity and rows>=2):return None
        # Audited blank strip above the panel (top at y=305), below HOME controls.
        return DailyTaskDetection("disabled", scaled_rect((380, 250, 140, 35), frame))
    return None


def _poll(session, selector):
    return poll_target_with_common_popups(session, selector, timeout=6, interval=.5)


def find_task_entry(items, frame):
    # P3's stylized 任 is missed by the supplied offline OCR model. Restrict
    # the exact suffix to the same footer slot, always gated by HOME upstream.
    full = _find(items, frame, "任务", (410, 1490, 140, 110))
    if full is not None:
        return full
    area = scaled_rect((460, 1520, 65, 65), frame)
    suffix = [i.box for i in items if "".join(i.text.split()) == "务" and i.score >= .5
              and area.x <= i.box.x < i.box.right <= area.right
              and area.y <= i.box.y < i.box.bottom <= area.bottom]
    return suffix[0] if len(suffix) == 1 else None


def _click(session, selector):
    _wait_before_click(session)
    frame = session.capture_frame()
    target = selector(session.recognize_text(frame), frame)
    return target is not None and session.click(*target.center) is not False


def run_daily_task_claim(session, *, capture=None, log=print):
    capture = capture or DiagnosticFrameStore(DATA_ROOT / "images/debug/pipeline/daily_task_claim").capture
    rounds = 0

    def fail(reason):
        capture("QJNN_DailyTaskClaim_" + reason, session.capture_frame())
        log(f"[DailyTaskClaim] SafeStop: {reason}; rounds={rounds}")
        return Result(False, rounds, reason)

    def home(items, frame):
        # Reuse the registered qjnn_home_any_page, including all its HOME gates.
        if (detect_daily_task(items, frame) is not None or has_common_reward_title(items)
                or _find(items, frame, "每日任务", (280, 290, 350, 85))):
            return None
        return session.recognize_node("QJNN_FriendHomeAnyPage", frame)

    def entry(items, frame):
        return find_task_entry(items, frame) if home(items, frame) else None

    def daily_target(expected):
        def select(items, frame):
            detected = detect_daily_task(items, frame)
            return detected.safe_rect if detected and detected.state == expected else None
        return select

    try:
        initial = _poll(session, lambda items, frame:
            "reward" if common_reward_target(session, items, frame) is not None else
            "daily" if detect_daily_task(items, frame) is not None else
            "home" if home(items, frame) is not None else None)
        if initial is None:
            return fail("home_entry_unconfirmed")
        if initial[2] == "reward":
            if not drain_common_rewards(session, detect_daily_task,
                    lambda selector: _click(session, selector), require_reward=False, log=log):
                return fail("reward_dismiss_unconfirmed")
        elif initial[2] == "home" and not _click(session, entry):
            return fail("home_entry_unconfirmed")
        for _ in range(MAX_CLAIM_ROUNDS + 1):
            page = _poll(session, detect_daily_task)
            if page is None:
                return fail("daily_state_unknown")
            if page[2].state == "disabled":
                if not _click(session, daily_target("disabled")):
                    return fail("close_state_changed")
                if _poll(session, home) is None:
                    return fail("home_return_unconfirmed")
                log(f"[DailyTaskClaim] completed; rounds={rounds}")
                return Result(True, rounds, "completed")
            if rounds >= MAX_CLAIM_ROUNDS:
                return fail("claim_round_limit")
            if not _click(session, daily_target("claimable")):
                return fail("claim_state_changed")
            rounds += 1
            if not drain_common_rewards(session, detect_daily_task,
                    lambda selector: _click(session, selector), log=log):
                return fail("reward_dismiss_unconfirmed")
        return fail("step_limit")
    except Exception as exc:
        log(f"[DailyTaskClaim] exception: {exc}")
        return fail("exception")


class DailyTaskClaimAction(CustomAction):
    # Generic recovery must not restart this action and reset its four-round cap.
    allow_back_to_home_recovery = False

    def run(self, context, argv):
        try:
            session = _make_runtime_session(context)

            def recognize_node(name, frame):
                detail = context.run_recognition(name, frame)
                if detail is None or not detail.hit or detail.best_result is None:
                    return None
                box = detail.best_result.box
                values = ((box.x, box.y, box.w, box.h) if hasattr(box, "w") else list(box)[:4])
                return Rect(*map(int, values))

            session.recognize_node = recognize_node
            return run_daily_task_claim(session).success
        except Exception as exc:
            print(f"[DailyTaskClaim] SafeStop: {exc}")
            return False
