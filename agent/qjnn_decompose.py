from __future__ import annotations

from app_paths import DATA_ROOT

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

from qjnn_alliance_commission import OcrItem, Rect, find_text_box
from qjnn_common_popup import (
    GlobalTransientResult,
    guard_runtime_session,
    handle_transient_if_present,
)
from qjnn_diagnostic import DiagnosticFrameStore
from qjnn_home import detect_home_page
from qjnn_home_navigation import navigate_home_page
from qjnn_home_random_visit import home_random_visit_loading_classifier
from qjnn_reward import detect_reward_clothes_card

try:
    from maa.custom_action import CustomAction
except ImportError:  # MaaFramework is optional for offline tests.
    CustomAction = object


# The Maa controller normalizes portrait frames to 720 x 1280.  The supplied
# 900 x 1600 evidence has the same 9:16 aspect ratio; all constants below are
# stored in controller coordinates and scaled again for any actual frame size.
REFERENCE_SIZE = (720, 1280)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DECOMPOSE_DIAGNOSTIC_DIR = DATA_ROOT / "images" / "debug" / "decompose"

OCR_THRESHOLD = 0.70
POLL_INTERVAL_SECONDS = 0.4
PAGE_TIMEOUT_SECONDS = 12.0
REWARD_TIMEOUT_SECONDS = 15.0
LOADING_TIMEOUT_SECONDS = 20.0
MAX_REWARD_DISMISSES = 12

# Short, state-gated pacing delays.  These are deliberately separate from all
# polling intervals/timeouts: recognition completes first, then the matching
# delay is applied immediately before the single permitted click.
NORMAL_CLICK_PRE_DELAY_SECONDS = 0.6
DECOMPOSE_CLICK_PRE_DELAY_SECONDS = 1.0
RARE_CONFIRM_CLICK_PRE_DELAY_SECONDS = 1.5
REWARD_CLICK_PRE_DELAY_SECONDS = 0.6
BACK_CLICK_PRE_DELAY_SECONDS = 0.6

HOME_WORKSHOP_ROI = (280, 680, 256, 240)
HOME_FRIEND_ROI = (400, 1144, 176, 136)
WORKSHOP_DECOMPOSE_ROI = (32, 544, 264, 208)
WORKSHOP_ANCHOR_ROI = (320, 240, 368, 496)
DECOMPOSE_TITLE_ROI = (200, 16, 320, 104)
DECOMPOSE_QUICK_ROI = (448, 136, 264, 104)
DECOMPOSE_IDLE_ROI = (80, 320, 560, 184)
DECOMPOSE_SELECTED_ROI = (120, 400, 480, 224)
DECOMPOSE_CONFIRM_TEXT_ROI = (120, 496, 480, 168)
DECOMPOSE_CONFIRM_BUTTON_ROI = (360, 648, 224, 136)
DECOMPOSE_CANCEL_BUTTON_ROI = (128, 648, 208, 136)
DECOMPOSE_REWARD_TITLE_ROI = (144, 216, 432, 152)

# Fixed clicks use interior SafeRects in 720 x 1280 controller coordinates.
# OCR-driven clicks use the recognized Rect itself and therefore need no fixed
# coordinate conversion.
TOP_BACK_SAFE_RECT = (24, 20, 64, 64)
REWARD_SAFE_RECT = (288, 448, 144, 144)
RARE_WARNING_TEXT = "选择的服装里含有4星或以上的稀有服装,是否继续分解?"


@dataclass(frozen=True)
class DecomposeResult:
    success: bool
    reward_dismissals: int
    reason: str


class DecomposeSession(Protocol):
    def capture_frame(self): ...

    def recognize_text(self, frame) -> list[OcrItem]: ...

    def click(self, x: int, y: int) -> None: ...

    def sleep(self, seconds: float) -> None: ...

    def monotonic(self) -> float: ...


def _normalize_text(value: str) -> str:
    return (
        re.sub(r"\s+", "", str(value))
        .replace("，", ",")
        .replace("：", ":")
        .replace("？", "?")
    )


def _scaled_rect(rect: tuple[int, int, int, int], frame) -> Rect:
    height, width = frame.shape[:2]
    x, y, rect_width, rect_height = rect
    left = round(x * width / REFERENCE_SIZE[0])
    top = round(y * height / REFERENCE_SIZE[1])
    right = round((x + rect_width) * width / REFERENCE_SIZE[0])
    bottom = round((y + rect_height) * height / REFERENCE_SIZE[1])
    return Rect(left, top, max(1, right - left), max(1, bottom - top))


def top_back_safe_rect(frame) -> Rect:
    return _scaled_rect(TOP_BACK_SAFE_RECT, frame)


def reward_safe_rect(frame) -> Rect:
    return _scaled_rect(REWARD_SAFE_RECT, frame)


def _confident(items: Sequence[OcrItem]) -> list[OcrItem]:
    return [item for item in items if float(item.score) >= OCR_THRESHOLD]


def _find(items: Sequence[OcrItem], text: str, frame, roi) -> Rect | None:
    normalized_items = [
        OcrItem(_normalize_text(item.text), item.box, item.score)
        for item in _confident(items)
    ]
    return find_text_box(
        normalized_items,
        _normalize_text(text),
        roi=_scaled_rect(roi, frame),
    )


def find_normal_home(items: Sequence[OcrItem], frame) -> str | None:
    """Reuse the shared page detector and require the shared Friend footer anchor."""
    page = str(getattr(detect_home_page(frame), "page", "Unknown"))
    if page not in {"P1", "P2", "P3"}:
        return None
    if _find(items, "好友", frame, HOME_FRIEND_ROI) is None:
        return None
    return page


def find_workshop_entry(items: Sequence[OcrItem], frame) -> Rect | None:
    """Recognize P1 first, then support both joined and split title OCR."""
    if str(getattr(detect_home_page(frame), "page", "Unknown")) != "P1":
        return None
    joined = _find(items, "设计工坊", frame, HOME_WORKSHOP_ROI)
    if joined is not None:
        return joined
    design = _find(items, "设计", frame, HOME_WORKSHOP_ROI)
    workshop = _find(items, "工坊", frame, HOME_WORKSHOP_ROI)
    if design is None or workshop is None:
        return None
    if abs(design.center[0] - workshop.center[0]) > max(design.width, workshop.width):
        return None
    vertical_gap = max(0, max(design.y, workshop.y) - min(design.bottom, workshop.bottom))
    if vertical_gap > 2 * max(design.height, workshop.height):
        return None
    return design.union(workshop)


def find_workshop_page(items: Sequence[OcrItem], frame) -> Rect | None:
    entry = _find(items, "分解重构", frame, WORKSHOP_DECOMPOSE_ROI)
    if entry is None:
        return None
    anchors = (
        _find(items, "服装进化", frame, WORKSHOP_ANCHOR_ROI),
        _find(items, "高级定制", frame, WORKSHOP_ANCHOR_ROI),
        _find(items, "设计图", frame, WORKSHOP_ANCHOR_ROI),
    )
    return entry if sum(anchor is not None for anchor in anchors) >= 2 else None


def workshop_back_target(frame):
    """Appearance check for the translucent arrow on the positively owned workshop."""
    import cv2
    import numpy as np
    template=cv2.imdecode(np.fromfile(str(PROJECT_ROOT/'assets/resource/image/decompose/workshop_back_arrow.png'),np.uint8),cv2.IMREAD_COLOR)
    if template is None:return None
    h,w=frame.shape[:2];scale=w/720
    template=cv2.resize(template,None,fx=scale,fy=scale)
    roi=frame[:round(180*h/1600),:round(180*w/900)]
    if roi.shape[0]<template.shape[0] or roi.shape[1]<template.shape[1]:return None
    _,score,_,point=cv2.minMaxLoc(cv2.matchTemplate(roi,template,cv2.TM_CCOEFF_NORMED))
    if score<.9:return None
    return Rect(point[0]+2,point[1]+2,template.shape[1]-4,template.shape[0]-4)


def enter_decompose_list(session, *, capture=None, log=print):
    """Retry only this reversible menu entry, after fresh stable WORKSHOP proof."""
    deadline=session.monotonic()+PAGE_TIMEOUT_SECONDS
    attempts=0; stable=0; previous=None
    while session.monotonic()<deadline:
        frame,items=_snapshot(session)
        idle=find_decompose_idle(items,frame)
        if idle is not None:return frame,items,idle
        blocked=any(_normalize_text(i.text) in {'确定','确认','取消','购买'} for i in _confident(items))
        entry=None if blocked else find_workshop_page(items,frame)
        stable=stable+1 if entry is not None and entry==previous else 0
        previous=entry
        if entry is not None and stable>=2 and attempts<2:
            session.sleep(NORMAL_CLICK_PRE_DELAY_SECONDS)
            fresh,fresh_items=_snapshot(session)
            target=None if any(_normalize_text(i.text) in {'确定','确认','取消','购买'} for i in _confident(fresh_items)) else find_workshop_page(fresh_items,fresh)
            if target!=entry:continue
            attempts+=1
            log(f'[Decompose] fresh WORKSHOP entry attempt={attempts}/2 SafeRect={target}')
            if session.click(*target.center) is False:return None
            stable=0;previous=None
            session.sleep(1.2)
        else:
            session.sleep(POLL_INTERVAL_SECONDS)
    if capture is not None:capture('QJNN_DecomposeIdleTimeout',session.capture_frame())
    return None


def _decompose_base(items: Sequence[OcrItem], frame) -> tuple[Rect, Rect] | None:
    title = _find(items, "服装分解", frame, DECOMPOSE_TITLE_ROI)
    quick = _find(items, "快速选中无用途衣服", frame, DECOMPOSE_QUICK_ROI)
    return (title, quick) if title is not None and quick is not None else None


def find_decompose_idle(items: Sequence[OcrItem], frame) -> Rect | None:
    base = _decompose_base(items, frame)
    idle = _find(items, "请选择要分解的服装", frame, DECOMPOSE_IDLE_ROI)
    return base[1] if base is not None and idle is not None else None


def find_decompose_selected(items: Sequence[OcrItem], frame) -> Rect | None:
    if _decompose_base(items, frame) is None:
        return None
    selected_roi = _scaled_rect(DECOMPOSE_SELECTED_ROI, frame)
    selected_count = None
    for item in _confident(items):
        text = _normalize_text(item.text)
        match = re.search(r"共选件数:([1-9][0-9]*)", text)
        center_x, center_y = item.box.center
        if (
            match
            and selected_roi.x <= center_x <= selected_roi.right
            and selected_roi.y <= center_y <= selected_roi.bottom
        ):
            selected_count = int(match.group(1))
            break
    if selected_count is None:
        return None
    return _find(items, "分解", frame, DECOMPOSE_SELECTED_ROI)


def find_confirm_popup(items: Sequence[OcrItem], frame) -> Rect | None:
    warning_roi = _scaled_rect(DECOMPOSE_CONFIRM_TEXT_ROI, frame)
    warning_items = []
    for item in _confident(items):
        center_x, center_y = item.box.center
        if (
            warning_roi.x <= center_x <= warning_roi.right
            and warning_roi.y <= center_y <= warning_roi.bottom
        ):
            warning_items.append(item)
    warning_items.sort(key=lambda item: (item.box.center[1], item.box.x))
    warning_text = "".join(_normalize_text(item.text) for item in warning_items)
    confirm = _find(items, "确定", frame, DECOMPOSE_CONFIRM_BUTTON_ROI)
    cancel = _find(items, "取消", frame, DECOMPOSE_CANCEL_BUTTON_ROI)
    if RARE_WARNING_TEXT not in warning_text or confirm is None or cancel is None:
        return None
    return confirm


def clothes_reward_safe_rect(items: Sequence[OcrItem], frame) -> Rect | None:
    # Shared geometry and ribbon detection never read garment/rarity/badge text.
    clothes = detect_reward_clothes_card(frame)
    if clothes.matched:
        return _scaled_rect(clothes.safe_rect, frame)
    return None


def find_reward(items: Sequence[OcrItem], frame) -> Rect | None:
    clothes = clothes_reward_safe_rect(items, frame)
    if clothes is not None:
        return clothes
    return _find(items, "恭喜获得", frame, DECOMPOSE_REWARD_TITLE_ROI)


def _snapshot(session: DecomposeSession):
    frame = session.capture_frame()
    return frame, session.recognize_text(frame)


def _poll(
    session: DecomposeSession,
    selector: Callable[[Sequence[OcrItem], object], object | None],
    *,
    timeout: float,
    timeout_state: str,
    capture: Callable[[str, object], bool] | None,
    log: Callable[[str], None],
):
    deadline = session.monotonic() + timeout
    last_frame = None
    while True:
        frame, items = _snapshot(session)
        last_frame = frame
        value = selector(items, frame)
        if value is not None:
            return frame, list(items), value
        transient, fresh_frame, fresh_items = handle_transient_if_present(
            session,
            frame,
            items,
            loading_classifier=home_random_visit_loading_classifier,
            loading_timeout=LOADING_TIMEOUT_SECONDS,
            loading_interval=POLL_INTERVAL_SECONDS,
            loading_timeout_state="QJNN_DecomposeLoadingTimeout",
            loading_capture=capture,
            capture=capture,
            log=log,
        )
        if transient is GlobalTransientResult.ERROR:
            break
        if transient in {
            GlobalTransientResult.HANDLED_POPUP,
            GlobalTransientResult.WAITED_LOADING,
        }:
            value = selector(fresh_items, fresh_frame)
            if value is not None:
                return fresh_frame, list(fresh_items), value
            continue
        remaining = deadline - session.monotonic()
        if remaining <= 0:
            break
        session.sleep(min(POLL_INTERVAL_SECONDS, remaining))
    if capture is not None and last_frame is not None:
        capture(timeout_state, last_frame)
    return None


def _click_rect(
    session: DecomposeSession,
    box: Rect,
    *,
    pre_delay: float,
) -> None:
    session.sleep(pre_delay)
    session.click(*box.center)


def _reward_or_idle(items: Sequence[OcrItem], frame):
    reward = find_reward(items, frame)
    if reward is not None:
        return "reward", reward
    idle = find_decompose_idle(items, frame)
    if idle is not None:
        return "idle", idle
    return None


def run_decompose_daily(
    session: DecomposeSession,
    *,
    capture: Callable[[str, object], bool] | None = None,
    log: Callable[[str], None] = print,
    progress=None,
) -> DecomposeResult:
    """Run one guarded decomposition and finish only on a normal HOME page."""
    frame, items = _snapshot(session)
    phase = progress.read() if progress is not None else None
    if phase not in (None, 'submitted', 'rewards', 'exiting'):
        return DecomposeResult(False, 0, 'invalid_continuation_state')
    if phase == 'rewards' and find_decompose_idle(items, frame) is not None:
        progress.write('exiting')
        return _return_decompose_home(session, frame, 0, capture=capture, log=log)
    if phase == 'exiting':
        if find_normal_home(items, frame) is not None:
            return DecomposeResult(True, 0, 'completed')
        if find_decompose_idle(items, frame) is not None:
            return _return_decompose_home(session, frame, 0, capture=capture, log=log)
        if find_workshop_page(items, frame) is not None:
            session.sleep(BACK_CLICK_PRE_DELAY_SECONDS)
            fresh, fresh_items = _snapshot(session)
            if find_workshop_page(fresh_items, fresh) is None:
                return DecomposeResult(False, 0, 'workshop_changed')
            session.click(*top_back_safe_rect(fresh).center)
            home = _poll(session, find_normal_home, timeout=PAGE_TIMEOUT_SECONDS,
                         timeout_state='QJNN_DecomposeReturnHomeTimeout', capture=capture, log=log)
            return DecomposeResult(home is not None, 0, 'completed' if home else 'home_return_unconfirmed')
        return DecomposeResult(False, 0, 'exit_state_unconfirmed')
    initial_reward = find_reward(items, frame)
    if initial_reward is not None and _decompose_base(items, frame) is not None:
        log("[Decompose] resuming pending rewards; no new decomposition submitted")
        return _finish_decompose_rewards(session, (frame, items, ("reward", initial_reward)),
                                         capture=capture, log=log, progress=progress)
    if phase in ('submitted', 'rewards'):
        # Confirmation may have consumed clothes. Require its reward; never repeat.
        return DecomposeResult(False, 0, 'submitted_result_unconfirmed')
    initial_idle = find_decompose_idle(items, frame)
    initial_workshop = find_workshop_page(items, frame)
    if initial_idle is None and initial_workshop is None:
        navigation = navigate_home_page(
            "P1",
            session.capture_frame,
            session.click,
            sleep=session.sleep,
            log=log,
        )
        if not navigation.success:
            if capture is not None:
                capture("QJNN_DecomposeHomeNavigationFailure", session.capture_frame())
            return DecomposeResult(False, 0, f"home_p1_{navigation.reason}")
        log(
            f"[Decompose] HOME P1 confirmed by shared navigator; "
            f"page_clicks={navigation.clicks}"
        )

        home = _poll(
            session,
            find_workshop_entry,
            timeout=PAGE_TIMEOUT_SECONDS,
            timeout_state="QJNN_DecomposeHomeEntryTimeout",
            capture=capture,
            log=log,
        )
        if home is None:
            return DecomposeResult(False, 0, "workshop_entry_not_found")
        _, _, workshop_entry = home
        _click_rect(
            session,
            workshop_entry,
            pre_delay=NORMAL_CLICK_PRE_DELAY_SECONDS,
        )

    if initial_idle is not None:
        idle = (frame, list(items), initial_idle)
        log("[Decompose] resuming confirmed decomposition list")
    else:
        workshop = _poll(
            session,
            find_workshop_page,
            timeout=PAGE_TIMEOUT_SECONDS,
            timeout_state="QJNN_DecomposeWorkshopTimeout",
            capture=capture,
            log=log,
        )
        if workshop is None:
            return DecomposeResult(False, 0, "workshop_not_confirmed")
        _, _, decompose_entry = workshop
        log("[Decompose] WORKSHOP confirmed")
        idle = enter_decompose_list(session, capture=capture, log=log)
    if idle is None:
        return DecomposeResult(False, 0, "decompose_idle_not_confirmed")
    _, _, quick_select = idle
    log("[Decompose] DECOMPOSE_IDLE confirmed; quick select is now allowed")
    _click_rect(
        session,
        quick_select,
        pre_delay=NORMAL_CLICK_PRE_DELAY_SECONDS,
    )

    # Allow the selection animation to settle, then require two fresh empty
    # observations. The pre-click idle screenshot cannot prove there is no work.
    session.sleep(1.2)
    empty_observations = 0
    def selected_or_empty(current_items, current_frame):
        nonlocal empty_observations
        button = find_decompose_selected(current_items, current_frame)
        if button is not None:
            return "selected", button
        idle = find_decompose_idle(current_items, current_frame)
        empty_observations = empty_observations + 1 if idle is not None else 0
        return ("empty", idle) if empty_observations >= 2 else None

    selected = _poll(
        session,
        selected_or_empty,
        timeout=PAGE_TIMEOUT_SECONDS,
        timeout_state="QJNN_DecomposeSelectionTimeout",
        capture=capture,
        log=log,
    )
    if selected is None:
        return DecomposeResult(False, 0, "quick_selection_unconfirmed")
    selected_frame, _, (selection_kind, decompose_button) = selected
    if selection_kind == "empty":
        log("[Decompose] no unused clothes; returning home")
        if progress is not None:
            progress.write('exiting')
        return _return_decompose_home(session, selected_frame, 0, capture=capture,
                                      log=log, success_reason="no_unused_clothes")
    log("[Decompose] QUICK_SELECTED confirmed; decomposition button is now allowed")
    _click_rect(
        session,
        decompose_button,
        pre_delay=DECOMPOSE_CLICK_PRE_DELAY_SECONDS,
    )

    confirmation = _poll(
        session,
        find_confirm_popup,
        timeout=PAGE_TIMEOUT_SECONDS,
        timeout_state="QJNN_DecomposeConfirmTimeout",
        capture=capture,
        log=log,
    )
    if confirmation is None:
        log("[Decompose] exact rare-clothes confirmation was not recognized; SafeStop")
        return DecomposeResult(False, 0, "rare_confirmation_unconfirmed")
    _, _, confirm_button = confirmation
    log("[Decompose] RARE_WARNING_FULL matched; confirm button is now allowed")
    if progress is not None:
        progress.write('submitted')
    _click_rect(
        session,
        confirm_button,
        pre_delay=RARE_CONFIRM_CLICK_PRE_DELAY_SECONDS,
    )
    log("[Decompose] exact rare-clothes confirmation submitted once")

    # “分解成功” is intentionally only a tolerated transition. The first durable
    # reward page is the required post-confirmation checkpoint.
    reward = _poll(
        session,
        lambda items, frame: (
            ("reward", box) if (box := find_reward(items, frame)) is not None else None
        ),
        timeout=REWARD_TIMEOUT_SECONDS,
        timeout_state="QJNN_DecomposeRewardTimeout",
        capture=capture,
        log=log,
    )
    if reward is None:
        return DecomposeResult(False, 0, "reward_not_confirmed")

    return _finish_decompose_rewards(session, reward, capture=capture, log=log, progress=progress)


def _finish_decompose_rewards(session, reward, *, capture, log, progress=None):
    if progress is not None:
        progress.write('rewards')
    reward_dismissals = 0
    state = reward
    while True:
        frame, reward_items, (kind, _) = state
        if kind == "idle":
            break
        if reward_dismissals >= MAX_REWARD_DISMISSES:
            if capture is not None:
                capture("QJNN_DecomposeRewardLimit", frame)
            return DecomposeResult(False, reward_dismissals, "reward_limit")
        safe = clothes_reward_safe_rect(reward_items, frame) or reward_safe_rect(frame)
        log(
            f"[Decompose] REWARD recognized; click SafeRect={safe}, "
            f"dismiss={reward_dismissals + 1}/{MAX_REWARD_DISMISSES}"
        )
        session.sleep(REWARD_CLICK_PRE_DELAY_SECONDS)
        fresh_frame, fresh_items = _snapshot(session)
        if find_reward(fresh_items, fresh_frame) is None:
            # A stale reward must never authorize clicking the revealed list.
            state = _poll(session, _reward_or_idle, timeout=REWARD_TIMEOUT_SECONDS,
                          timeout_state="QJNN_DecomposeRewardTransitionTimeout",
                          capture=capture, log=log)
            if state is None:
                return DecomposeResult(False, reward_dismissals, "reward_transition_unknown")
            continue
        safe = clothes_reward_safe_rect(fresh_items, fresh_frame) or reward_safe_rect(fresh_frame)
        session.click(*safe.center)
        reward_dismissals += 1
        state = _poll(
            session,
            _reward_or_idle,
            timeout=REWARD_TIMEOUT_SECONDS,
            timeout_state="QJNN_DecomposeRewardTransitionTimeout",
            capture=capture,
            log=log,
        )
        if state is None:
            return DecomposeResult(
                False,
                reward_dismissals,
                "reward_transition_unknown",
            )

    log("[Decompose] DECOMPOSE_IDLE confirmed; reward loop finished")
    if progress is not None:
        progress.write('exiting')
    return _return_decompose_home(session, state[0], reward_dismissals,
                                  capture=capture, log=log)


def _return_decompose_home(session, idle_frame, reward_dismissals, *, capture, log,
                           success_reason="completed"):
    back_safe = top_back_safe_rect(idle_frame)
    log(f"[Decompose] return to WORKSHOP; Back SafeRect={back_safe}")
    session.sleep(BACK_CLICK_PRE_DELAY_SECONDS)
    session.click(*back_safe.center)
    returned_workshop = _poll(
        session,
        find_workshop_page,
        timeout=PAGE_TIMEOUT_SECONDS,
        timeout_state="QJNN_DecomposeReturnWorkshopTimeout",
        capture=capture,
        log=log,
    )
    if returned_workshop is None:
        return DecomposeResult(False, reward_dismissals, "workshop_return_unconfirmed")
    workshop_frame = returned_workshop[0]
    back_safe = top_back_safe_rect(workshop_frame)
    log(f"[Decompose] WORKSHOP confirmed; return to HOME; Back SafeRect={back_safe}")
    session.sleep(BACK_CLICK_PRE_DELAY_SECONDS)
    session.click(*back_safe.center)

    returned_home = _poll(
        session,
        find_normal_home,
        timeout=PAGE_TIMEOUT_SECONDS,
        timeout_state="QJNN_DecomposeReturnHomeTimeout",
        capture=capture,
        log=log,
    )
    if returned_home is None:
        return DecomposeResult(False, reward_dismissals, "home_return_unconfirmed")
    page = returned_home[2]
    log(f"[Decompose] completed; normal HOME {page} confirmed")
    return DecomposeResult(True, reward_dismissals, success_reason)


def _ocr_items_from_detail(detail) -> list[OcrItem]:
    if detail is None:
        return []
    results = list(getattr(detail, "all_results", []) or [])
    if not results and getattr(detail, "best_result", None) is not None:
        results = [detail.best_result]
    converted = []
    for result in results:
        box = result.box
        if all(hasattr(box, name) for name in ("x", "y", "w", "h")):
            rect = Rect(int(box.x), int(box.y), int(box.w), int(box.h))
        else:
            rect = Rect(*(int(value) for value in list(box)[:4]))
        converted.append(OcrItem(str(result.text), rect, float(getattr(result, "score", 0.0))))
    return converted


def _make_runtime_session(context):
    controller = context.tasker.controller

    class RuntimeSession:
        @staticmethod
        def capture_frame():
            job = controller.post_screencap()
            job.wait()
            if hasattr(job, "succeeded") and not job.succeeded:
                raise RuntimeError("decompose screenshot failed")
            cached = getattr(controller, "cached_image", None)
            frame = cached() if callable(cached) else cached
            if frame is None or getattr(frame, "size", 0) == 0:
                raise RuntimeError("decompose screenshot is empty")
            return frame

        @staticmethod
        def recognize_text(frame) -> list[OcrItem]:
            return _ocr_items_from_detail(context.run_recognition("QJNN_AgentOcrBase", frame))

        @staticmethod
        def click(x: int, y: int) -> None:
            job = controller.post_click(x, y)
            job.wait()
            if hasattr(job, "succeeded") and not job.succeeded:
                raise RuntimeError(f"decompose click failed: ({x}, {y})")

        sleep = staticmethod(time.sleep)
        monotonic = staticmethod(time.monotonic)

    return guard_runtime_session(context, RuntimeSession())


class DecomposeDailyAction(CustomAction):
    def __init__(self, run_state=None):
        super().__init__()
        self._run_state = run_state

    def run(self, context, argv) -> bool:
        session = None
        diagnostics = DiagnosticFrameStore(DECOMPOSE_DIAGNOSTIC_DIR)
        try:
            session = _make_runtime_session(context)
            from qjnn_run_state import run_record
            result = run_decompose_daily(session, capture=diagnostics.capture,
                **({'progress': run_record(self._run_state, 'decompose_daily_state.json')}
                   if self._run_state is not None else {}))
            if not result.success:
                print(
                    f"[Decompose] SafeStop reason={result.reason}, "
                    f"reward_dismissals={result.reward_dismissals}"
                )
            return result.success
        except Exception as exc:
            print(f"[Decompose] unhandled failure; SafeStop: {exc}")
            if session is not None:
                try:
                    diagnostics.capture(
                        "QJNN_DecomposeException",
                        session.capture_frame(),
                    )
                except Exception as capture_exc:
                    print(f"[Decompose] diagnostic unavailable: {capture_exc}")
            return False
