from __future__ import annotations

import re
import random
import time
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Callable, Protocol

from qjnn_alliance_commission import (
    OcrItem,
    Rect,
    find_text_box,
    map_alliance_reference_point,
)
from qjnn_arena_capture import ArenaCaptureManager
from qjnn_common_popup import (
    CommonPopupKind, detect_common_safe_popup,
    GlobalTransientResult,
    guard_runtime_session,
    handle_transient_if_present,
)
from qjnn_home import detect_home_page
from qjnn_home_navigation import navigate_home_page
from qjnn_state_recovery import detect_entry_state
from qjnn_reward import common_reward_target

try:
    from maa.custom_action import CustomAction
except ImportError:  # MaaFramework is optional for offline state-machine tests.
    CustomAction = object


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARENA_BACK_TEMPLATE = (
    PROJECT_ROOT / "assets" / "resource" / "image" / "arena" / "back_button.png"
)
ARENA_BACK_TEMPLATE_REFERENCE_WIDTH = 720
ARENA_ENTRY_ROI = (240, 450, 300, 230)
ARENA_TITLE_ROI = (190, 20, 520, 180)
ARENA_STAGE_BUTTON_ROI = (220, 1160, 460, 300)
ARENA_REMAINING_ROI = (500, 1400, 400, 200)
ARENA_QUICK_FINISH_ROI = (380, 1180, 500, 350)
ARENA_BATTLE_MARKER_ROI = (250, 350, 400, 250)
ARENA_LOADING_ROI = (410, 750, 80, 80)
ARENA_REWARD_TITLE_ROI = (150, 120, 600, 280)
ARENA_REWARD_CARD_RECT = (120, 250, 660, 800)
ARENA_REWARD_SAFE_RECT = (320, 500, 260, 260)
ARENA_BACK_ROI = (0, 0, 150, 150)

ARENA_REWARD_WHITE_SATURATION_MAX = 55
ARENA_REWARD_WHITE_VALUE_MIN = 185
ARENA_BACK_CLICK_POINT = (70, 65)
ARENA_BACK_THRESHOLD = 0.90
ARENA_OCR_THRESHOLD = 0.70
ARENA_LOADING_CYAN_RATIO_MIN = 0.08
ARENA_LOADING_CYAN_RATIO_MAX = 0.35

ARENA_POLL_INTERVAL_SECONDS = 0.5
ARENA_PAGE_TIMEOUT_SECONDS = 20.0
ARENA_LOADING_TIMEOUT_SECONDS = 20.0
ARENA_COUNT_REFRESH_TIMEOUT_SECONDS = 8.0
ARENA_HOME_RETURN_TIMEOUT_SECONDS = 12.0
ARENA_MAX_BATTLES = 5
ARENA_MAX_REWARD_DISMISSES = 4
ARENA_PRE_CLICK_DELAY_MIN = 0.6
ARENA_PRE_CLICK_DELAY_MAX = 0.9


def _wait_before_click(
    session,
    *,
    sampler: Callable[[float, float], float] | None = None,
) -> float:
    delay = float((sampler or random.uniform)(ARENA_PRE_CLICK_DELAY_MIN, ARENA_PRE_CLICK_DELAY_MAX))
    if not ARENA_PRE_CLICK_DELAY_MIN <= delay <= ARENA_PRE_CLICK_DELAY_MAX:
        raise ValueError(f"Arena pre-click delay out of range: {delay}")
    session.sleep(delay)
    return delay


def _business_click(session, x: int, y: int) -> None:
    # Callers confirm the business state/target before reaching this helper.
    _wait_before_click(session)
    session.click(x, y)


@dataclass(frozen=True)
class ArenaHomeState:
    title: Rect
    stage: Rect
    remaining: int


@dataclass(frozen=True)
class ArenaResult:
    success: bool
    remaining: int | None
    battles: int
    reason: str


class ArenaEntryKind(str, Enum):
    HOME = "HOME"
    ARENA_HOME = "ARENA_HOME"
    BATTLE_READY = "BATTLE_READY"
    REWARD = "REWARD"


@dataclass(frozen=True)
class ArenaEntryState:
    kind: ArenaEntryKind
    value: object


class ArenaSession(Protocol):
    def capture_frame(self): ...

    def recognize_text(self, frame) -> list[OcrItem]: ...

    def click(self, x: int, y: int) -> None: ...

    def sleep(self, seconds: float) -> None: ...

    def monotonic(self) -> float: ...

    def back_arrow_score(self, frame) -> float: ...


def _scaled_rect(
    rect: tuple[int, int, int, int], frame_width: int, frame_height: int
) -> Rect:
    left, top = map_alliance_reference_point(
        (rect[0], rect[1]), frame_width, frame_height
    )
    right, bottom = map_alliance_reference_point(
        (rect[0] + rect[2], rect[1] + rect[3]), frame_width, frame_height
    )
    return Rect(left, top, max(1, right - left), max(1, bottom - top))


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("：", ":")


def _confident_items(items: list[OcrItem]) -> list[OcrItem]:
    return [item for item in items if item.score >= ARENA_OCR_THRESHOLD]


def find_arena_entry(items: list[OcrItem], frame) -> Rect | None:
    height, width = frame.shape[:2]
    return find_text_box(
        _confident_items(items),
        "搭配竞技场",
        roi=_scaled_rect(ARENA_ENTRY_ROI, width, height),
    )


def parse_arena_remaining(items: list[OcrItem], frame) -> int | None:
    height, width = frame.shape[:2]
    roi = _scaled_rect(ARENA_REMAINING_ROI, width, height)
    pattern = re.compile(r"今日剩余次数:?([0-5])/5")
    candidates = [
        item
        for item in _confident_items(items)
        if roi.x <= item.box.center[0] <= roi.right
        and roi.y <= item.box.center[1] <= roi.bottom
    ]
    for item in candidates:
        match = pattern.search(_normalize_text(item.text))
        if match:
            return int(match.group(1))
    combined = "".join(
        _normalize_text(item.text)
        for item in sorted(candidates, key=lambda item: (item.box.y, item.box.x))
    )
    match = pattern.search(combined)
    return int(match.group(1)) if match else None


def find_arena_home(items: list[OcrItem], frame) -> ArenaHomeState | None:
    height, width = frame.shape[:2]
    items = _confident_items(items)
    title = find_text_box(
        items,
        "搭配竞技场",
        roi=_scaled_rect(ARENA_TITLE_ROI, width, height),
    )
    stage = find_text_box(
        items,
        "登上舞台",
        roi=_scaled_rect(ARENA_STAGE_BUTTON_ROI, width, height),
    )
    remaining = parse_arena_remaining(items, frame)
    if title is None or stage is None or remaining is None:
        return None
    return ArenaHomeState(title, stage, remaining)


def find_quick_finish(items: list[OcrItem], frame) -> Rect | None:
    if detect_common_safe_popup(frame, items).kind in {
            CommonPopupKind.NOTICE_CONFIRM, CommonPopupKind.NOTICE_OUTSIDE, CommonPopupKind.SKILL_UNLOCK}:
        return None
    height, width = frame.shape[:2]
    return find_text_box(
        _confident_items(items),
        "快速完成",
        roi=_scaled_rect(ARENA_QUICK_FINISH_ROI, width, height),
    )


def arena_loading_cyan_ratio(frame) -> float:
    import cv2

    height, width = frame.shape[:2]
    roi = _scaled_rect(ARENA_LOADING_ROI, width, height)
    region = frame[roi.y : roi.bottom, roi.x : roi.right]
    if region.size == 0:
        return 0.0
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    cyan = (
        (hsv[:, :, 0] >= 75)
        & (hsv[:, :, 0] <= 110)
        & (hsv[:, :, 1] >= 45)
        & (hsv[:, :, 2] >= 140)
    )
    return float(cyan.mean())


def find_arena_battle_surface(items: list[OcrItem], frame) -> bool:
    height, width = frame.shape[:2]
    confident = _confident_items(items)
    title = find_text_box(
        confident,
        "搭配竞技场",
        roi=_scaled_rect(ARENA_TITLE_ROI, width, height),
    )
    battle = find_text_box(
        confident,
        "比拼",
        roi=_scaled_rect(ARENA_BATTLE_MARKER_ROI, width, height),
    )
    return bool(
        title is not None
        and battle is not None
        and find_quick_finish(items, frame) is not None
        and parse_arena_remaining(items, frame) is not None
    )


def find_arena_loading(items: list[OcrItem], frame) -> bool:
    cyan_ratio = arena_loading_cyan_ratio(frame)
    return bool(
        find_arena_battle_surface(items, frame)
        and ARENA_LOADING_CYAN_RATIO_MIN
        <= cyan_ratio
        <= ARENA_LOADING_CYAN_RATIO_MAX
    )


def detect_arena_entry_state(
    items: list[OcrItem],
    frame,
    *,
    detect_page: Callable[[object], object] = detect_home_page,
) -> ArenaEntryState | None:
    reward = find_reward_popup(items, frame)
    if reward is not None:
        return ArenaEntryState(ArenaEntryKind.REWARD, reward)
    home = find_arena_home(items, frame)
    if home is not None:
        return ArenaEntryState(ArenaEntryKind.ARENA_HOME, home)
    loading = find_arena_loading(items, frame)
    if find_arena_battle_surface(items, frame) and not loading:
        quick_finish = find_quick_finish(items, frame)
        remaining = parse_arena_remaining(items, frame)
        if quick_finish is not None and remaining is not None:
            return ArenaEntryState(
                ArenaEntryKind.BATTLE_READY,
                (quick_finish, remaining),
            )
    if loading:
        return None
    page = str(getattr(detect_page(frame), "page", "Unknown"))
    if page in {"P1", "P2", "P3"}:
        return ArenaEntryState(ArenaEntryKind.HOME, page)
    return None


def arena_reward_white_ratio(frame) -> float:
    import cv2

    height, width = frame.shape[:2]
    card = _scaled_rect(ARENA_REWARD_CARD_RECT, width, height)
    region = frame[card.y : card.bottom, card.x : card.right]
    if region.size == 0:
        return 0.0
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    white = (hsv[:, :, 1] <= ARENA_REWARD_WHITE_SATURATION_MAX) & (
        hsv[:, :, 2] >= ARENA_REWARD_WHITE_VALUE_MIN
    )
    return float(white.mean())


def find_reward_popup(items: list[OcrItem], frame) -> Rect | None:
    height, width = frame.shape[:2]
    title = find_text_box(
        _confident_items(items),
        "恭喜获得",
        roi=_scaled_rect(ARENA_REWARD_TITLE_ROI, width, height),
    )
    # A confident title is authoritative.  Card whiteness is intentionally only
    # an auxiliary diagnostic because reward art/content can change its ratio.
    if title is None:
        return None
    return title


def reward_safe_rect(frame) -> Rect:
    height, width = frame.shape[:2]
    return _scaled_rect(ARENA_REWARD_SAFE_RECT, width, height)


def _arena_reward_target(session,items,frame):
    if find_reward_popup(items,frame) is None:return None
    target=common_reward_target(session,items,frame)
    if target is not None and any(''.join(i.text.split()) in {'跳过','确定'}
            and target.x<=i.box.center[0]<target.right and target.y<=i.box.center[1]<target.bottom
            for i in items):return target
    return reward_safe_rect(frame)


@lru_cache(maxsize=1)
def _load_arena_back_template():
    import cv2
    import numpy as np

    data = np.fromfile(ARENA_BACK_TEMPLATE, dtype=np.uint8)
    template = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if template is None:
        raise RuntimeError(f"无法读取竞技场返回按钮模板：{ARENA_BACK_TEMPLATE}")
    return template


def arena_back_button_score(frame) -> float:
    import cv2

    height, width = frame.shape[:2]
    roi = _scaled_rect(ARENA_BACK_ROI, width, height)
    search = frame[roi.y : roi.bottom, roi.x : roi.right]
    template = _load_arena_back_template()
    scale = width / ARENA_BACK_TEMPLATE_REFERENCE_WIDTH
    template = cv2.resize(
        template,
        (
            max(1, round(template.shape[1] * scale)),
            max(1, round(template.shape[0] * scale)),
        ),
        interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC,
    )
    if template.shape[1] > search.shape[1] or template.shape[0] > search.shape[0]:
        return 0.0
    return float(
        cv2.minMaxLoc(
            cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
        )[1]
    )


def log_arena_home_diagnostics(
    items: list[OcrItem], frame, *, log: Callable[[str], None] = print
) -> None:
    height, width = frame.shape[:2]
    components = (
        ("title", ARENA_TITLE_ROI),
        ("stage", ARENA_STAGE_BUTTON_ROI),
        ("remaining", ARENA_REMAINING_ROI),
    )
    log(f"[ArenaHomeDebug] frame: {width}x{height}")
    for name, reference_roi in components:
        roi = _scaled_rect(reference_roi, width, height)
        log(
            f"[ArenaHomeDebug] {name} ROI: "
            f"({roi.x}, {roi.y}, {roi.width}, {roi.height})"
        )
        candidates = [
            item
            for item in items
            if roi.x <= item.box.center[0] <= roi.right
            and roi.y <= item.box.center[1] <= roi.bottom
        ]
        for item in candidates:
            log(
                f"[ArenaHomeDebug] {name} OCR: {item.text!r}, "
                f"confidence={item.score:.6f}, box="
                f"({item.box.x}, {item.box.y}, {item.box.width}, {item.box.height})"
            )
    title = find_text_box(
        _confident_items(items),
        "搭配竞技场",
        roi=_scaled_rect(ARENA_TITLE_ROI, width, height),
    )
    stage = find_text_box(
        _confident_items(items),
        "登上舞台",
        roi=_scaled_rect(ARENA_STAGE_BUTTON_ROI, width, height),
    )
    remaining = parse_arena_remaining(items, frame)
    log(
        "[ArenaHomeDebug] matched: "
        f"title={title is not None}, stage={stage is not None}, "
        f"remaining={remaining!r}"
    )


def _poll_snapshot(
    session: ArenaSession,
    selector: Callable[[list[OcrItem], object], object | None],
    *,
    timeout: float,
    on_timeout: Callable[[object, list[OcrItem]], None] | None = None,
    loading_capture: Callable[[str, object], bool] | None = None,
    log: Callable[[str], None] = print,
) -> tuple[object, list[OcrItem], object] | None:
    deadline = session.monotonic() + timeout
    while True:
        frame = session.capture_frame()
        items = session.recognize_text(frame)
        value = selector(items, frame)
        if value is not None:
            return frame, items, value
        transient, fresh_frame, fresh_items = handle_transient_if_present(
            session,
            frame,
            items,
            loading_classifier=find_arena_loading,
            loading_timeout=ARENA_LOADING_TIMEOUT_SECONDS,
            loading_interval=ARENA_POLL_INTERVAL_SECONDS,
            loading_timeout_state="QJNN_ArenaLoadingTimeout",
            loading_capture=loading_capture,
            log=log,
        )
        if transient is GlobalTransientResult.ERROR:
            return None
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
            if on_timeout is not None:
                on_timeout(frame, items)
            return None
        session.sleep(min(ARENA_POLL_INTERVAL_SECONDS, remaining))

def _reward_or_home(items: list[OcrItem], frame):
    reward = find_reward_popup(items, frame)
    if reward is not None:
        return "reward", reward
    home = find_arena_home(items, frame)
    if home is not None:
        return "home", home
    return None


def process_arena_rewards(
    session: ArenaSession,
    *,
    capture: Callable[[str, object], bool] | None = None,
    log: Callable[[str], None] = print,
) -> tuple[object, ArenaHomeState, int] | None:
    dismissals = 0
    transition_deadline: float | None = None
    unknown_deadline: float | None = None
    while True:
        frame = session.capture_frame()
        items = session.recognize_text(frame)
        state = _reward_or_home(items, frame)
        if state is None:
            transient, fresh_frame, fresh_items = handle_transient_if_present(
                session,
                frame,
                items,
                loading_classifier=find_arena_loading,
                loading_timeout=ARENA_LOADING_TIMEOUT_SECONDS,
                loading_interval=ARENA_POLL_INTERVAL_SECONDS,
                loading_timeout_state="QJNN_ArenaLoadingTimeout",
                loading_capture=capture,
                log=log,
            )
            if transient is GlobalTransientResult.ERROR:
                return None
            if transient in {
                GlobalTransientResult.HANDLED_POPUP,
                GlobalTransientResult.WAITED_LOADING,
            }:
                frame, items = fresh_frame, list(fresh_items)
                state = _reward_or_home(items, frame)
                if state is not None:
                    pass
                else:
                    continue

        if state is None:
            if find_arena_battle_surface(items, frame):
                if transition_deadline is None:
                    transition_deadline = (
                        session.monotonic() + ARENA_PAGE_TIMEOUT_SECONDS
                    )
                remaining = transition_deadline - session.monotonic()
                if remaining <= 0:
                    log("Arena reward flow timed out on a known battle transition")
                    return None
                session.sleep(min(ARENA_POLL_INTERVAL_SECONDS, remaining))
                continue
            # The final result can briefly lose both its heading and home
            # controls. Wait read-only for settled OCR; never dismiss unknown UI.
            if unknown_deadline is None:
                unknown_deadline=session.monotonic()+6
            if session.monotonic()<unknown_deadline:
                session.sleep(ARENA_POLL_INTERVAL_SECONDS)
                continue
            log("Arena reward flow reached an unknown non-loading page")
            if capture is not None:
                capture("QJNN_ArenaRewardFailure", frame)
            return None

        transition_deadline = None
        unknown_deadline = None
        state_name, value = state
        if state_name == "home":
            return frame, value, dismissals
        if dismissals >= ARENA_MAX_REWARD_DISMISSES:
            log("Arena reward flow exceeded the safe dismiss limit")
            if capture is not None:
                capture("QJNN_ArenaRewardFailure", frame)
            return None
        if capture is not None:
            capture("QJNN_ArenaRewardPopup", frame)
        # Reuse reward Skip/Confirm/card handling, but re-read after the random
        # delay and twice before input. Never click the old frame's coordinates.
        _wait_before_click(session)
        fresh=session.capture_frame(); fresh_items=session.recognize_text(fresh)
        safe=_arena_reward_target(session,fresh_items,fresh)
        if safe is None:continue
        session.sleep(.4)
        newest=session.capture_frame(); newest_items=session.recognize_text(newest)
        target=_arena_reward_target(session,newest_items,newest)
        if target is None:continue
        if any(abs(a-b)>3 for a,b in zip((safe.x,safe.y,safe.width,safe.height),(target.x,target.y,target.width,target.height))):
            return None
        if session.click(*target.center) is False:return None
        dismissals += 1
        session.sleep(ARENA_POLL_INTERVAL_SECONDS)


def wait_for_decreased_remaining(
    session: ArenaSession,
    remaining_before: int,
    initial_frame,
    initial_home: ArenaHomeState,
    *,
    capture: Callable[[str, object], bool] | None = None,
    log: Callable[[str], None] = print,
) -> tuple[object, ArenaHomeState] | None:
    if initial_home.remaining < remaining_before:
        return initial_frame, initial_home
    if initial_home.remaining > remaining_before:
        log("Arena remaining count increased unexpectedly")
        return None

    refreshed = _poll_snapshot(
        session,
        lambda items, frame: (
            home
            if (home := find_arena_home(items, frame)) is not None
            and home.remaining < remaining_before
            else None
        ),
        timeout=ARENA_COUNT_REFRESH_TIMEOUT_SECONDS,
        loading_capture=capture,
        log=log,
    )
    if refreshed is None:
        log("Arena remaining count did not decrease")
        return None
    frame, _, home = refreshed
    return frame, home


def _exit_arena_from_confirmed_home(
    session: ArenaSession,
    home_frame,
    home: ArenaHomeState,
    *,
    battles: int,
    capture: Callable[[str, object], bool] | None,
    detect_page: Callable[[object], object],
    log: Callable[[str], None],
) -> ArenaResult:
    if home.remaining != 0:
        log(f"Arena exit refused: remaining is {home.remaining}/5, not 0/5")
        return ArenaResult(False, home.remaining, battles, "arena_not_done")

    if capture is not None:
        capture("QJNN_ArenaHomeDone", home_frame)
    height, width = home_frame.shape[:2]
    back_roi = _scaled_rect(ARENA_BACK_ROI, width, height)
    back_score = session.back_arrow_score(home_frame)
    if back_score < ARENA_BACK_THRESHOLD:
        log("Arena exit refused: back button was not confirmed")
        log(f"[ArenaExit] frame: {width}x{height}")
        log(
            "[ArenaExit] back ROI: "
            f"({back_roi.x}, {back_roi.y}, {back_roi.width}, {back_roi.height})"
        )
        log(
            f"[ArenaExit] template score={back_score:.6f}, "
            f"threshold={ARENA_BACK_THRESHOLD:.2f}, matched=False"
        )
        if capture is not None:
            capture("QJNN_ArenaExitBackFailure", home_frame)
        return ArenaResult(False, 0, battles, "back_button_not_found")

    log(
        f"[ArenaExit] template score={back_score:.6f}, "
        f"threshold={ARENA_BACK_THRESHOLD:.2f}, matched=True"
    )
    _business_click(session, *map_alliance_reference_point(ARENA_BACK_CLICK_POINT, width, height))

    returned = _poll_snapshot(
        session,
        lambda items, frame: (
            "P3" if str(getattr(detect_page(frame), "page", "Unknown")) == "P3" else None
        ),
        timeout=ARENA_HOME_RETURN_TIMEOUT_SECONDS,
        loading_capture=capture,
        log=log,
    )
    if returned is None:
        log("Arena exit did not return to Home P3")
        return ArenaResult(False, 0, battles, "home_p3_not_confirmed")
    returned_frame, _, _ = returned
    if capture is not None:
        capture("QJNN_ArenaBackHome", returned_frame)
    return ArenaResult(True, 0, battles, "completed")


def run_arena_exit(
    session: ArenaSession,
    *,
    capture: Callable[[str, object], bool] | None = None,
    detect_page: Callable[[object], object] = detect_home_page,
    log: Callable[[str], None] = print,
) -> ArenaResult:
    frame = session.capture_frame()
    items = session.recognize_text(frame)
    home = find_arena_home(items, frame)
    if home is None:
        transient, fresh_frame, fresh_items = handle_transient_if_present(
            session,
            frame,
            items,
            loading_classifier=find_arena_loading,
            loading_timeout=ARENA_LOADING_TIMEOUT_SECONDS,
            loading_interval=ARENA_POLL_INTERVAL_SECONDS,
            loading_timeout_state="QJNN_ArenaLoadingTimeout",
            loading_capture=capture,
            log=log,
        )
        if transient is GlobalTransientResult.ERROR:
            return ArenaResult(False, None, 0, "transient_timeout")
        if transient in {
            GlobalTransientResult.HANDLED_POPUP,
            GlobalTransientResult.WAITED_LOADING,
        }:
            frame, items = fresh_frame, list(fresh_items)
            home = find_arena_home(items, frame)
    if home is None:
        log("Arena exit refused: current screen is not ArenaHome")
        return ArenaResult(False, None, 0, "arena_home_not_found")
    if home.remaining != 0:
        log(f"Arena exit refused: remaining is {home.remaining}/5, not 0/5")
        return ArenaResult(False, home.remaining, 0, "arena_not_done")
    return _exit_arena_from_confirmed_home(
        session,
        frame,
        home,
        battles=0,
        capture=capture,
        detect_page=detect_page,
        log=log,
    )


def run_arena_daily(
    session: ArenaSession,
    *,
    capture: Callable[[str, object], bool] | None = None,
    detect_page: Callable[[object], object] = detect_home_page,
    log: Callable[[str], None] = print,
) -> ArenaResult:
    recovery = detect_entry_state(
        session,
        lambda items, frame: detect_arena_entry_state(
            list(items), frame, detect_page=detect_page
        ),
        task_name="QJNN_ArenaDaily",
        loading_classifier=find_arena_loading,
        loading_timeout=ARENA_LOADING_TIMEOUT_SECONDS,
        loading_interval=ARENA_POLL_INTERVAL_SECONDS,
        capture_unknown=capture,
        unknown_capture_state="QJNN_ArenaUnknownStart",
        loading_timeout_state="QJNN_ArenaLoadingTimeout",
        log=log,
    )
    if recovery is None:
        return ArenaResult(False, None, 0, "start_state_unknown")
    start = recovery.state
    log(f"[Arena] restart state: {start.kind.value}")

    def arena_home_timeout(frame, items: list[OcrItem]) -> None:
        log_arena_home_diagnostics(items, frame, log=log)
        if capture is not None:
            capture("QJNN_ArenaHomeWaitFailure", frame)

    battles = 0

    if start.kind is ArenaEntryKind.HOME:
        entry_box = find_arena_entry(recovery.items, recovery.frame)
        if start.value != "P3" or entry_box is None:
            navigation = navigate_home_page(
                "P3",
                session.capture_frame,
                session.click,
                sleep=session.sleep,
                log=log,
            )
            if not navigation.success:
                return ArenaResult(False, None, 0, "home_navigation_failed")
            entry = _poll_snapshot(
                session,
                find_arena_entry,
                timeout=ARENA_PAGE_TIMEOUT_SECONDS,
                loading_capture=capture,
                log=log,
            )
            if entry is None:
                log("Arena entry was not found on Home P3")
                return ArenaResult(False, None, 0, "entry_not_found")
            _, _, entry_box = entry
        _business_click(session, *entry_box.center)
        arena = _poll_snapshot(
            session,
            find_arena_home,
            timeout=ARENA_PAGE_TIMEOUT_SECONDS,
            on_timeout=arena_home_timeout,
            loading_capture=capture,
            log=log,
        )
        if arena is None:
            log("Arena home did not become ready")
            return ArenaResult(False, None, 0, "arena_home_not_found")
        home_frame, _, home = arena
    elif start.kind is ArenaEntryKind.ARENA_HOME:
        home_frame = recovery.frame
        home = start.value
    elif start.kind is ArenaEntryKind.REWARD:
        rewards = process_arena_rewards(session, capture=capture, log=log)
        if rewards is None:
            return ArenaResult(False, None, 0, "reward_resume_failed")
        home_frame, home, _ = rewards
    else:
        quick_finish, remaining_before = start.value
        if capture is not None:
            capture("QJNN_ArenaBattleQuickFinish", recovery.frame)
        _business_click(session, *quick_finish.center)
        rewards = process_arena_rewards(session, capture=capture, log=log)
        if rewards is None:
            return ArenaResult(False, remaining_before, 0, "reward_flow_failed")
        returned_frame, returned_home, _ = rewards
        decreased = wait_for_decreased_remaining(
            session,
            remaining_before,
            returned_frame,
            returned_home,
            capture=capture,
            log=log,
        )
        if decreased is None:
            return ArenaResult(False, remaining_before, 1, "count_not_decreased")
        home_frame, home = decreased
        battles = 1

    log(f"[Arena] remaining: {home.remaining}/5")

    while home.remaining > 0 and battles < ARENA_MAX_BATTLES:
        remaining_before = home.remaining
        if capture is not None:
            capture("QJNN_ArenaHomeActive", home_frame)
        _business_click(session, *home.stage.center)

        battle = _poll_snapshot(
            session,
            find_quick_finish,
            timeout=ARENA_PAGE_TIMEOUT_SECONDS,
            loading_capture=capture,
            log=log,
        )
        if battle is None:
            log("Arena Quick Finish did not become ready; no blind click performed")
            return ArenaResult(False, remaining_before, battles, "quick_finish_not_found")
        battle_frame, _, quick_finish = battle
        if capture is not None:
            capture("QJNN_ArenaBattleQuickFinish", battle_frame)
        _business_click(session, *quick_finish.center)

        rewards = process_arena_rewards(session, capture=capture, log=log)
        if rewards is None:
            return ArenaResult(False, remaining_before, battles, "reward_flow_failed")
        returned_frame, returned_home, _ = rewards
        decreased = wait_for_decreased_remaining(
            session,
            remaining_before,
            returned_frame,
            returned_home,
            capture=capture,
            log=log,
        )
        if decreased is None:
            return ArenaResult(False, remaining_before, battles + 1, "count_not_decreased")
        home_frame, home = decreased
        battles += 1
        log(f"[Arena] remaining: {home.remaining}/5")

    if home.remaining > 0:
        log("Arena battle loop exceeded the hard limit")
        return ArenaResult(False, home.remaining, battles, "max_battles")

    return _exit_arena_from_confirmed_home(
        session,
        home_frame,
        home,
        battles=battles,
        capture=capture,
        detect_page=detect_page,
        log=log,
    )


def _make_runtime_session(context):
    controller = context.tasker.controller

    class RuntimeSession:
        @staticmethod
        def capture_frame():
            job = controller.post_screencap()
            job.wait()
            if hasattr(job, "succeeded") and not job.succeeded:
                raise RuntimeError("竞技场截图失败")
            cached = getattr(controller, "cached_image", None)
            frame = cached() if callable(cached) else cached
            if frame is None or getattr(frame, "size", 0) == 0:
                raise RuntimeError("竞技场截图为空")
            return frame

        @staticmethod
        def recognize_text(frame) -> list[OcrItem]:
            detail = context.run_recognition("QJNN_AgentOcrBase", frame)
            if detail is None:
                return []
            results = list(getattr(detail, "all_results", []) or [])
            if not results and getattr(detail, "best_result", None) is not None:
                results = [detail.best_result]
            converted: list[OcrItem] = []
            for result in results:
                box = result.box
                if all(hasattr(box, name) for name in ("x", "y", "w", "h")):
                    rect = Rect(int(box.x), int(box.y), int(box.w), int(box.h))
                else:
                    rect = Rect(*(int(value) for value in list(box)[:4]))
                converted.append(
                    OcrItem(
                        str(result.text),
                        rect,
                        float(getattr(result, "score", 0.0)),
                    )
                )
            return converted

        @staticmethod
        def click(x: int, y: int) -> None:
            job = controller.post_click(x, y)
            job.wait()
            if hasattr(job, "succeeded") and not job.succeeded:
                raise RuntimeError(f"竞技场点击失败：({x}, {y})")

        sleep = staticmethod(time.sleep)
        monotonic = staticmethod(time.monotonic)
        back_arrow_score = staticmethod(arena_back_button_score)

    return guard_runtime_session(context, RuntimeSession())


class ArenaDailyAction(CustomAction):
    def __init__(self, arena_capture: ArenaCaptureManager | None = None) -> None:
        super().__init__()
        self._arena_capture = arena_capture

    def run(self, context, argv) -> bool:
        try:
            capture = (
                self._arena_capture.capture_once
                if self._arena_capture is not None
                else None
            )
            return run_arena_daily(
                _make_runtime_session(context), capture=capture
            ).success
        except Exception as exc:
            print(f"搭配竞技场任务失败：{exc}")
            return False


class ArenaExitDebugAction(CustomAction):
    def __init__(self, arena_capture: ArenaCaptureManager | None = None) -> None:
        super().__init__()
        self._arena_capture = arena_capture

    def run(self, context, argv) -> bool:
        try:
            capture = (
                self._arena_capture.capture_once
                if self._arena_capture is not None
                else None
            )
            return run_arena_exit(
                _make_runtime_session(context), capture=capture
            ).success
        except Exception as exc:
            print(f"竞技场退出调试失败：{exc}")
            return False
