from __future__ import annotations

from app_paths import DATA_ROOT

import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol, Sequence

from qjnn_alliance_commission import OcrItem, Rect
from qjnn_common_popup import (
    CommonPopupKind,
    detect_common_safe_popup,
    GlobalTransientResult,
    guard_runtime_session,
    handle_transient_if_present,
)
from qjnn_diagnostic import DiagnosticFrameStore
from qjnn_home import detect_home_page
from qjnn_home_room_entry import (
    HOME_ROOM_ENTRY_SAFE_RECT as HOME_ROOM_ICON_REFERENCE_SAFE_RECT,
    HOME_ROOM_TRANSITION_WAIT_SECONDS,
    enter_home_room,
    fixed_home_room_entry_safe_rect,
    wait_before_home_room_click,
)

try:
    from maa.custom_action import CustomAction
except ImportError:  # MaaFramework is optional for static and offline tests.
    CustomAction = object


PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOME_LIFE_BITS_DIAGNOSTIC_DIR = (
    DATA_ROOT / "images" / "debug" / "pipeline" / "home_life_bits"
)
HOME_LIFE_BITS_OCR_THRESHOLD = 0.60
HOME_LIFE_BITS_STATE_ATTEMPTS = 5
HOME_LIFE_BITS_POLL_SECONDS = 0.5
HOME_LIFE_BITS_OPEN_ATTEMPTS = 3
ROOM_MAIN_REFERENCE_SIZE = (720, 1280)
# Relative to the OCR label's horizontal centre and top edge.  Measured from
# QJNN_HomeLifeBitsOpenPanelFailed_001.png; the rectangle stays inside the
# circular notebook icon rather than clicking the text below it.
LIFE_BITS_ICON_SAFE_SIZE = (56, 48)
LIFE_BITS_ICON_CENTER_Y_FROM_TEXT_TOP = -44
# Reference-pixel margins around the ROOM_MAIN “生活点滴” label.  The real
# running-entry timer is directly above the label; unrelated full-screen times
# and the timer inside an open panel are outside this anchored ROI.
ROOM_LIFE_BITS_TIMER_ROI_MARGINS = (15, 55, 20, 8)

LIFE_BITS_REFERENCE_SIZE = (720, 1280)
# Verified offline against QJNN_HomeLifeBitsClaimIconSafeRectTodo_001.png.
# This is the resulting 720 x 1280 reference Rect for documentation/tests;
# runtime derives the target from the dynamic "可获得XX张心意券" OCR anchor.
HOME_LIFE_BITS_CLAIM_ICON_SAFE_RECT = (337, 512, 64, 56)
CLAIM_ICON_SAFE_SIZE = (64, 56)
CLAIM_ICON_TOP_FROM_REWARD_TOP = -145
# Verified offline against QJNN_HomeLifeBitsRewardDismissSafeRectTodo_001.png.
# The Rect is a large blank area inside the foreground reward card, below its
# title ribbon and above its reward illustration.  It avoids all OCR, icons,
# buttons, the character, and the underlying ROOM_MAIN/LifeBits controls.
HOME_LIFE_BITS_REWARD_DISMISS_SAFE_RECT: tuple[int, int, int, int] | None = (
    250,
    370,
    220,
    100,
)
# Verified offline against QJNN_HomeLifeBitsSecondTaskIconSafeRectTodo_001.png.
# This is the resulting 720 x 1280 reference Rect; runtime targeting is derived
# from the 8-hour and 20-ticket OCR anchors rather than clicking this coordinate.
HOME_LIFE_BITS_SECOND_TASK_ICON_SAFE_RECT = (336, 498, 56, 56)
SECOND_TASK_ICON_SAFE_SIZE = (56, 56)
SECOND_TASK_ICON_CENTER_Y_FROM_HOUR_TOP = -104

# Verified offline against QJNN_HomeLifeBitsDismissSafeRectTodo_001.png at
# 720 x 1280.  This central floor area is below the panel and above the bottom
# controls, clear of the character and both side control columns.
HOME_LIFE_BITS_PANEL_DISMISS_SAFE_RECT: tuple[int, int, int, int] | None = (
    300,
    950,
    120,
    80,
)

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９：", "0123456789:")
_TIMER_PATTERN = re.compile(r"^[0-9]{2}:[0-9]{2}:[0-9]{2}$")
_RUNNING_REWARD_PATTERN = re.compile(r"^可获得[0-9]+张心意券$")
_HEART_TICKET_PATTERN = re.compile(r"^[0-9]+张心意券$")
_REWARD_CONGRATS_PATTERN = re.compile(r"^恭喜获得(?:[0-9]+张心意券)?$")


class HomeLifeBitsState(str, Enum):
    HOME_P1 = "HOME_P1"
    HOME_P2 = "HOME_P2"
    HOME_P3 = "HOME_P3"
    ROOM_MAIN = "ROOM_MAIN"
    ROOM_EXPANSION_CONFIRM = "ROOM_EXPANSION_CONFIRM"
    LIFE_BITS_CLAIM_READY = "LIFE_BITS_CLAIM_READY"
    LIFE_BITS_REWARD = "LIFE_BITS_REWARD"
    LIFE_BITS_SELECT = "LIFE_BITS_SELECT"
    LIFE_BITS_CONFIRM = "LIFE_BITS_CONFIRM"
    LIFE_BITS_RUNNING = "LIFE_BITS_RUNNING"


@dataclass(frozen=True)
class HomeLifeBitsSnapshot:
    state: HomeLifeBitsState
    frame: object
    items: list[OcrItem]
    target: Rect | None = None
    room_entry_running: bool = False


@dataclass(frozen=True)
class HomeLifeBitsResult:
    success: bool
    start_state: str
    reason: str


class HomeLifeBitsSession(Protocol):
    def capture_frame(self): ...
    def recognize_text(self, frame) -> list[OcrItem]: ...
    def click(self, x: int, y: int) -> None: ...
    def sleep(self, seconds: float) -> None: ...
    def monotonic(self) -> float: ...


def normalize_life_bits_text(value: str) -> str:
    return "".join(str(value).split()).translate(_FULLWIDTH_DIGITS)


def _confident(items: Sequence[OcrItem]) -> list[OcrItem]:
    return [item for item in items if float(item.score) >= HOME_LIFE_BITS_OCR_THRESHOLD]


def _exact(items: Sequence[OcrItem], text: str) -> OcrItem | None:
    expected = normalize_life_bits_text(text)
    return next(
        (item for item in items if normalize_life_bits_text(item.text) == expected),
        None,
    )


def _contains(items: Sequence[OcrItem], text: str) -> OcrItem | None:
    expected = normalize_life_bits_text(text)
    return next(
        (item for item in items if expected in normalize_life_bits_text(item.text)),
        None,
    )


def _matches(items: Sequence[OcrItem], pattern: re.Pattern[str]) -> OcrItem | None:
    return next(
        (
            item
            for item in items
            if pattern.fullmatch(normalize_life_bits_text(item.text)) is not None
        ),
        None,
    )


def _box_inside(box: Rect, roi: Rect) -> bool:
    return (
        box.x >= roi.x
        and box.y >= roi.y
        and box.right <= roi.right
        and box.bottom <= roi.bottom
    )


def life_bits_entry_safe_rect(text_box: Rect, frame) -> Rect | None:
    """Project the “生活点滴” OCR label upward into its icon's safe interior."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    height, width = frame.shape[:2]
    ref_width, ref_height = ROOM_MAIN_REFERENCE_SIZE
    safe_width = max(1, round(LIFE_BITS_ICON_SAFE_SIZE[0] * width / ref_width))
    safe_height = max(1, round(LIFE_BITS_ICON_SAFE_SIZE[1] * height / ref_height))
    center_x = text_box.center[0]
    center_y = text_box.y + round(
        LIFE_BITS_ICON_CENTER_Y_FROM_TEXT_TOP * height / ref_height
    )
    safe = Rect(
        center_x - safe_width // 2,
        center_y - safe_height // 2,
        safe_width,
        safe_height,
    )
    if safe.x < 0 or safe.y < 0 or safe.right > width or safe.bottom > height:
        return None
    return safe


def room_life_bits_entry_timer(
    items: Sequence[OcrItem],
    entry_box: Rect,
    frame,
) -> OcrItem | None:
    """Find HH:MM:SS only inside the ROI anchored to the ROOM_MAIN entry."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    height, width = frame.shape[:2]
    ref_width, ref_height = ROOM_MAIN_REFERENCE_SIZE
    left_margin, top_margin, right_margin, bottom_margin = (
        ROOM_LIFE_BITS_TIMER_ROI_MARGINS
    )
    left = max(0, entry_box.x - round(left_margin * width / ref_width))
    top = max(0, entry_box.y - round(top_margin * height / ref_height))
    right = min(width, entry_box.right + round(right_margin * width / ref_width))
    bottom = min(
        height,
        entry_box.bottom + round(bottom_margin * height / ref_height),
    )
    roi = Rect(left, top, max(1, right - left), max(1, bottom - top))
    return next(
        (
            item
            for item in _confident(items)
            if _TIMER_PATTERN.fullmatch(normalize_life_bits_text(item.text))
            is not None
            and normalize_life_bits_text(item.text) != "00:00:00"
            and _box_inside(item.box, roi)
        ),
        None,
    )


def _scaled_reference_rect(
    rect: tuple[int, int, int, int] | None,
    frame,
    *,
    reference_size: tuple[int, int] = LIFE_BITS_REFERENCE_SIZE,
) -> Rect | None:
    if rect is None or frame is None or getattr(frame, "size", 0) == 0:
        return None
    height, width = frame.shape[:2]
    ref_width, ref_height = reference_size
    x, y, rect_width, rect_height = rect
    scaled = Rect(
        round(x * width / ref_width),
        round(y * height / ref_height),
        max(1, round(rect_width * width / ref_width)),
        max(1, round(rect_height * height / ref_height)),
    )
    if scaled.x < 0 or scaled.y < 0 or scaled.right > width or scaled.bottom > height:
        return None
    return scaled


def _selection_has_three_columns(items: Sequence[OcrItem], frame) -> bool:
    """Require the 4/8/24-hour and 10/20/60-ticket columns in x order."""
    # The history list below the choices also contains strings such as
    # “……获得60张心意券。”.  Maa returns those rows before the compact choice
    # labels on the real page, so a first-substring match can bind a column to
    # history text and fail otherwise-correct geometry.  Prefer the standalone
    # labels; substring matching remains only an OCR-merge fallback.
    def choice_label(value: str) -> OcrItem | None:
        return _exact(items, value) or _contains(items, value)

    hours = [
        choice_label(value)
        for value in ("4小时后获得", "8小时后获得", "24小时后获得")
    ]
    tickets = [
        choice_label(value)
        for value in ("10张心意券", "20张心意券", "60张心意券")
    ]
    if any(value is None for value in hours + tickets):
        return False
    hour_centres = [value.box.center[0] for value in hours if value is not None]
    ticket_centres = [value.box.center[0] for value in tickets if value is not None]
    if hour_centres != sorted(hour_centres) or ticket_centres != sorted(ticket_centres):
        return False
    width = frame.shape[1]
    return all(
        abs(hour_x - ticket_x) <= width * 0.15
        for hour_x, ticket_x in zip(hour_centres, ticket_centres)
    )


def second_task_icon_safe_rect(
    items: Sequence[OcrItem],
    frame,
) -> Rect | None:
    """Project the verified second-column reward anchors into the icon interior."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    hour = _exact(items, "8小时后获得") or _contains(items, "8小时后获得")
    ticket = _exact(items, "20张心意券") or _contains(items, "20张心意券")
    if hour is None or ticket is None:
        return None

    height, width = frame.shape[:2]
    ref_width, ref_height = LIFE_BITS_REFERENCE_SIZE
    if abs(hour.box.center[0] - ticket.box.center[0]) > width * 0.15:
        return None
    safe_width = max(1, round(SECOND_TASK_ICON_SAFE_SIZE[0] * width / ref_width))
    safe_height = max(1, round(SECOND_TASK_ICON_SAFE_SIZE[1] * height / ref_height))
    center_x = round((hour.box.center[0] + ticket.box.center[0]) / 2)
    center_y = hour.box.y + round(
        SECOND_TASK_ICON_CENTER_Y_FROM_HOUR_TOP * height / ref_height
    )
    safe = Rect(
        center_x - safe_width // 2,
        center_y - safe_height // 2,
        safe_width,
        safe_height,
    )
    if safe.x < 0 or safe.y < 0 or safe.right > width or safe.bottom > height:
        return None
    return safe


def claim_icon_safe_rect(
    reward: OcrItem,
    frame,
) -> Rect | None:
    """Project the dynamic ticket-reward OCR anchor into the completed icon."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    if _RUNNING_REWARD_PATTERN.fullmatch(
        normalize_life_bits_text(reward.text)
    ) is None:
        return None

    height, width = frame.shape[:2]
    ref_width, ref_height = LIFE_BITS_REFERENCE_SIZE
    safe_width = max(1, round(CLAIM_ICON_SAFE_SIZE[0] * width / ref_width))
    safe_height = max(1, round(CLAIM_ICON_SAFE_SIZE[1] * height / ref_height))
    safe = Rect(
        reward.box.center[0] - safe_width // 2,
        reward.box.y + round(CLAIM_ICON_TOP_FROM_REWARD_TOP * height / ref_height),
        safe_width,
        safe_height,
    )
    if safe.x < 0 or safe.y < 0 or safe.right > width or safe.bottom > height:
        return None
    # The icon must remain wholly above its OCR anchor.  This also rejects a
    # badly shifted/merged reward box instead of converting it into a blind tap.
    if safe.bottom >= reward.box.y:
        return None
    return safe


def detect_home_life_bits_state(
    items: Sequence[OcrItem],
    frame,
) -> HomeLifeBitsSnapshot | None:
    """Detect legal restart states, ordered from modal to underlying page."""
    if detect_common_safe_popup(frame, items).kind in {
        CommonPopupKind.NOTICE_CONFIRM, CommonPopupKind.NOTICE_OUTSIDE,
    }:
        return None  # Clear the foreground notice before recognizing room controls.
    confident = _confident(items)

    cancel = _exact(confident, "取消")
    if (
        cancel is not None
        and _exact(confident, "确定") is not None
        and _contains(confident, "确定要扩建") is not None
        and _contains(confident, "当前房间空间") is not None
    ):
        return HomeLifeBitsSnapshot(
            HomeLifeBitsState.ROOM_EXPANSION_CONFIRM, frame, list(items), cancel.box,
        )

    reward_title = _matches(confident, _REWARD_CONGRATS_PATTERN)
    reward_ticket = _matches(confident, _HEART_TICKET_PATTERN)
    if reward_title is not None and (
        reward_ticket is not None or "心意券" in normalize_life_bits_text(reward_title.text)
    ):
        return HomeLifeBitsSnapshot(
            HomeLifeBitsState.LIFE_BITS_REWARD,
            frame,
            list(items),
            _scaled_reference_rect(HOME_LIFE_BITS_REWARD_DISMISS_SAFE_RECT, frame),
        )

    confirm_button = _exact(confident, "确定")
    if (
        confirm_button is not None
        and _exact(confident, "取消") is not None
        and _contains(confident, "8小时") is not None
        and (
            _contains(confident, "确认选择") is not None
            or _contains(confident, "结束前不能取消") is not None
        )
    ):
        return HomeLifeBitsSnapshot(
            HomeLifeBitsState.LIFE_BITS_CONFIRM,
            frame,
            list(items),
            confirm_button.box,
        )

    timer = _matches(confident, _TIMER_PATTERN)
    if timer is not None and normalize_life_bits_text(timer.text) == "00:00:00":
        timer = None
    running_reward = _matches(confident, _RUNNING_REWARD_PATTERN)
    if (
        _exact(confident, "生活点滴") is not None
        and timer is not None
        and running_reward is not None
    ):
        return HomeLifeBitsSnapshot(
            HomeLifeBitsState.LIFE_BITS_RUNNING,
            frame,
            list(items),
        )

    life_title = _exact(confident, "生活点滴")
    prompt = _contains(confident, "接下来要做什么")
    if (
        life_title is not None
        and prompt is not None
        and _selection_has_three_columns(confident, frame)
    ):
        return HomeLifeBitsSnapshot(
            HomeLifeBitsState.LIFE_BITS_SELECT,
            frame,
            list(items),
            second_task_icon_safe_rect(confident, frame),
        )

    if life_title is not None and prompt is not None:
        # The server/loading overlay can hide a column after submission. The
        # room toolbar behind this panel is not permission to click its entry.
        return None

    # A completed task shares the reward line with RUNNING, so this branch is
    # intentionally after the timer and selection-page gates.  The task name is
    # dynamic and is deliberately absent from both detection and targeting.
    if (
        life_title is not None
        and running_reward is not None
        and timer is None
        and prompt is None
    ):
        return HomeLifeBitsSnapshot(
            HomeLifeBitsState.LIFE_BITS_CLAIM_READY,
            frame,
            list(items),
            claim_icon_safe_rect(running_reward, frame),
        )

    life_entry = _exact(confident, "生活点滴")
    room_markers = sum(
        _exact(confident, marker) is not None
        for marker in ("小屋信息", "我的家具")
    )
    if life_entry is not None and room_markers >= 1:
        entry_timer = room_life_bits_entry_timer(
            confident,
            life_entry.box,
            frame,
        )
        return HomeLifeBitsSnapshot(
            HomeLifeBitsState.ROOM_MAIN,
            frame,
            list(items),
            life_bits_entry_safe_rect(life_entry.box, frame),
            entry_timer is not None,
        )

    # HOME identity and the fixed footer target are independent from OCR of the
    # low-contrast “小屋” label.  P1/P2/P3 share the same bottom toolbar.
    page = str(getattr(detect_home_page(frame), "page", "Unknown"))
    if page in {"P1", "P2", "P3"}:
        return HomeLifeBitsSnapshot(
            HomeLifeBitsState[f"HOME_{page}"],
            frame,
            list(items),
            fixed_home_room_entry_safe_rect(frame),
        )
    return None


def _log_diagnostics(
    state: str,
    items: Sequence[OcrItem],
    *,
    last_action: str,
    log: Callable[[str], None],
) -> None:
    log(f"[HomeLifeBits] SafeStop state={state}, last_action={last_action}")
    for item in items:
        log(
            f"[HomeLifeBits] OCR text={item.text!r}, "
            f"confidence={float(item.score):.6f}, box="
            f"({item.box.x},{item.box.y},{item.box.width},{item.box.height})"
        )


def _observe_state(
    session: HomeLifeBitsSession,
    *,
    allowed: set[HomeLifeBitsState],
    capture: Callable[[str, object], bool] | None,
    failure_state: str,
    last_action: str,
    log: Callable[[str], None],
    attempts: int = HOME_LIFE_BITS_STATE_ATTEMPTS,
    require_running: bool = False,
    stable_frames: int = 1,
) -> HomeLifeBitsSnapshot | None:
    last_frame = None
    last_items: list[OcrItem] = []
    stable = 0
    for attempt in range(attempts):
        last_frame = session.capture_frame()
        last_items = list(session.recognize_text(last_frame))
        snapshot = detect_home_life_bits_state(last_items, last_frame)
        running = (snapshot is not None and (snapshot.state is HomeLifeBitsState.LIFE_BITS_RUNNING
                   or snapshot.state is HomeLifeBitsState.ROOM_MAIN and snapshot.room_entry_running))
        if snapshot is not None and snapshot.state in allowed and (not require_running or running):
            stable += 1
        else:
            stable = 0
        if stable >= stable_frames:
            log(
                f"[HomeLifeBits] state={snapshot.state.value}, "
                f"attempt={attempt + 1}/{attempts}"
            )
            return snapshot

        transient, fresh_frame, fresh_items = handle_transient_if_present(
            session,
            last_frame,
            last_items,
            capture=capture,
            log=log,
        )
        if transient is GlobalTransientResult.ERROR:
            last_frame = fresh_frame
            last_items = list(fresh_items)
            break
        if transient in {
            GlobalTransientResult.HANDLED_POPUP,
            GlobalTransientResult.WAITED_LOADING,
        }:
            last_frame = fresh_frame
            last_items = list(fresh_items)
            snapshot = detect_home_life_bits_state(last_items, last_frame)
            if (snapshot is not None and snapshot.state in allowed
                    and not require_running and stable_frames == 1):
                log(f"[HomeLifeBits] transient cleared; state={snapshot.state.value}")
                return snapshot
        if attempt + 1 < attempts:
            session.sleep(HOME_LIFE_BITS_POLL_SECONDS)

    if capture is not None and last_frame is not None:
        capture(failure_state, last_frame)
    _log_diagnostics(failure_state, last_items, last_action=last_action, log=log)
    return None


def _click_target(
    session: HomeLifeBitsSession,
    snapshot: HomeLifeBitsSnapshot,
    *,
    action: str,
    log: Callable[[str], None],
) -> bool:
    if snapshot.target is None or snapshot.target.width <= 0 or snapshot.target.height <= 0:
        log(f"[HomeLifeBits] {action}: verified target Rect missing; zero clicks")
        return False
    log(
        f"[HomeLifeBits] {action}: target_box={snapshot.target}, "
        f"point={snapshot.target.center}"
    )
    wait_before_home_room_click(session, action=action, log=log)
    session.click(*snapshot.target.center)
    return True


def _scaled_safe_rect(
    rect: tuple[int, int, int, int], frame
) -> Rect | None:
    return _scaled_reference_rect(rect, frame, reference_size=LIFE_BITS_REFERENCE_SIZE)


def _stop_for_unverified_target(
    snapshot: HomeLifeBitsSnapshot,
    *,
    diagnostic: str,
    reason: str,
    action: str,
    capture: Callable[[str, object], bool] | None,
    log: Callable[[str], None],
    start_state: str,
) -> HomeLifeBitsResult:
    if capture is not None:
        capture(diagnostic, snapshot.frame)
    _log_diagnostics(diagnostic, snapshot.items, last_action=action, log=log)
    log(f"[HomeLifeBits] TODO: {action} needs a verified target SafeRect; zero clicks")
    return HomeLifeBitsResult(False, start_state, reason)


def run_home_life_bits(
    session: HomeLifeBitsSession,
    *,
    dismiss_safe_rect: tuple[int, int, int, int] | None = HOME_LIFE_BITS_PANEL_DISMISS_SAFE_RECT,
    capture: Callable[[str, object], bool] | None = None,
    log: Callable[[str], None] = print,
) -> HomeLifeBitsResult:
    legal_states = set(HomeLifeBitsState)
    snapshot = _observe_state(
        session,
        allowed=legal_states,
        capture=capture,
        failure_state="QJNN_HomeLifeBitsUnknownStart",
        last_action="none",
        log=log,
    )
    if snapshot is None:
        return HomeLifeBitsResult(False, "UNKNOWN", "unknown_start")
    start_state = snapshot.state.value

    if snapshot.state is HomeLifeBitsState.ROOM_EXPANSION_CONFIRM:
        if not _click_target(session, snapshot, action="cancel_room_expansion", log=log):
            return HomeLifeBitsResult(False, start_state, "expansion_cancel_failed")
        session.sleep(HOME_ROOM_TRANSITION_WAIT_SECONDS)
        snapshot = _observe_state(
            session, allowed={HomeLifeBitsState.ROOM_MAIN}, capture=capture,
            failure_state="QJNN_HomeExpansionCancelFailed",
            last_action="cancel_room_expansion", log=log,
        )
        if snapshot is None:
            return HomeLifeBitsResult(False, start_state, "expansion_cancel_failed")

    home_states = {
        HomeLifeBitsState.HOME_P1,
        HomeLifeBitsState.HOME_P2,
        HomeLifeBitsState.HOME_P3,
    }
    if snapshot.state in home_states:
        snapshot = enter_home_room(
            session,
            snapshot,
            detect_state=detect_home_life_bits_state,
            state_of=lambda value: value.state,
            home_states=home_states,
            room_state=HomeLifeBitsState.ROOM_MAIN,
            capture=capture,
            failure_state="QJNN_HomeLifeBitsEnterRoomFailed",
            log=log,
            log_prefix="HomeLifeBits",
        )
        if snapshot is None:
            return HomeLifeBitsResult(False, start_state, "enter_room_failed")

    if snapshot.state is HomeLifeBitsState.ROOM_MAIN:
        for attempt in range(HOME_LIFE_BITS_OPEN_ATTEMPTS):
            if snapshot.room_entry_running:
                log(
                    "[HomeLifeBits] ROOM_MAIN entry has a local HH:MM:SS; "
                    "LifeBits already running, no panel click needed"
                )
                return HomeLifeBitsResult(True, start_state, "already_running")
            log(
                f"[HomeLifeBits] open_life_bits attempt={attempt + 1}/"
                f"{HOME_LIFE_BITS_OPEN_ATTEMPTS}"
            )
            if not _click_target(session, snapshot, action="open_life_bits", log=log):
                if capture is not None:
                    capture("QJNN_HomeLifeBitsOpenPanelFailed", snapshot.frame)
                return HomeLifeBitsResult(False, start_state, "open_life_bits_target_missing")
            # Room controls can be visible before the entry animation accepts
            # taps. Wait for the transition, then authorize any retry only from
            # a fresh ROOM_MAIN snapshot (including its newly derived target).
            session.sleep(HOME_ROOM_TRANSITION_WAIT_SECONDS)
            snapshot = _observe_state(
                session,
                allowed={
                    HomeLifeBitsState.ROOM_MAIN,
                    HomeLifeBitsState.LIFE_BITS_CLAIM_READY,
                    HomeLifeBitsState.LIFE_BITS_REWARD,
                    HomeLifeBitsState.LIFE_BITS_SELECT,
                    HomeLifeBitsState.LIFE_BITS_RUNNING,
                },
                capture=capture,
                failure_state="QJNN_HomeLifeBitsOpenPanelFailed",
                last_action="open_life_bits",
                log=log,
            )
            if snapshot is None:
                return HomeLifeBitsResult(False, start_state, "open_life_bits_failed")
            if snapshot.state is not HomeLifeBitsState.ROOM_MAIN:
                break
        if snapshot.state is HomeLifeBitsState.ROOM_MAIN:
            if snapshot.room_entry_running:
                return HomeLifeBitsResult(True, start_state, "already_running")
            if capture is not None:
                capture("QJNN_HomeLifeBitsOpenPanelFailed", snapshot.frame)
            _log_diagnostics(
                "QJNN_HomeLifeBitsOpenPanelFailed", snapshot.items,
                last_action="open_life_bits", log=log,
            )
            return HomeLifeBitsResult(False, start_state, "open_life_bits_failed")

    if snapshot.state is HomeLifeBitsState.LIFE_BITS_CLAIM_READY:
        claim_reward = _matches(_confident(snapshot.items), _RUNNING_REWARD_PATTERN)
        if claim_reward is not None:
            log(
                "[HomeLifeBits] claim reward anchor: "
                f"text={claim_reward.text!r}, box={claim_reward.box}"
            )
            log(
                "[HomeLifeBits] claim icon SafeRect: "
                f"anchor_center_x={claim_reward.box.center[0]}, "
                f"anchor_top={claim_reward.box.y}, safe_rect={snapshot.target}"
            )
        if not _click_target(session, snapshot, action="claim_completed_life_bits", log=log):
            return _stop_for_unverified_target(
                snapshot,
                diagnostic="QJNN_HomeLifeBitsClaimIconSafeRectTodo",
                reason="claim_icon_safe_rect_unverified",
                action="claim_completed_life_bits",
                capture=capture,
                log=log,
                start_state=start_state,
            )
        snapshot = _observe_state(
            session,
            allowed={HomeLifeBitsState.LIFE_BITS_REWARD},
            capture=capture,
            failure_state="QJNN_HomeLifeBitsClaimNoTransition",
            last_action="claim_completed_life_bits",
            log=log,
        )
        if snapshot is None:
            return HomeLifeBitsResult(False, start_state, "claim_no_transition")

    if snapshot.state is HomeLifeBitsState.LIFE_BITS_REWARD:
        log(
            "[HomeLifeBits] reward dismiss SafeRect: "
            f"reference_rect={HOME_LIFE_BITS_REWARD_DISMISS_SAFE_RECT}, "
            f"scaled_rect={snapshot.target}, "
            f"point={snapshot.target.center if snapshot.target else None}"
        )
        if not _click_target(session, snapshot, action="dismiss_life_bits_reward", log=log):
            return _stop_for_unverified_target(
                snapshot,
                diagnostic="QJNN_HomeLifeBitsRewardDismissSafeRectTodo",
                reason="reward_dismiss_safe_rect_unverified",
                action="dismiss_life_bits_reward",
                capture=capture,
                log=log,
                start_state=start_state,
            )
        snapshot = _observe_state(
            session,
            allowed={HomeLifeBitsState.LIFE_BITS_SELECT},
            capture=capture,
            failure_state="QJNN_HomeLifeBitsRewardDismissNoTransition",
            last_action="dismiss_life_bits_reward",
            log=log,
        )
        if snapshot is None:
            return HomeLifeBitsResult(False, start_state, "reward_dismiss_no_transition")
        log(f"[HomeLifeBits] reward dismiss successor: state={snapshot.state.value}")

    if snapshot.state is HomeLifeBitsState.LIFE_BITS_SELECT:
        if not _click_target(session, snapshot, action="select_second_8h_task", log=log):
            return _stop_for_unverified_target(
                snapshot,
                diagnostic="QJNN_HomeLifeBitsSecondTaskIconSafeRectTodo",
                reason="second_task_icon_safe_rect_unverified",
                action="select_second_8h_task",
                capture=capture,
                log=log,
                start_state=start_state,
            )
        snapshot = _observe_state(
            session,
            allowed={HomeLifeBitsState.LIFE_BITS_CONFIRM},
            capture=capture,
            failure_state="QJNN_HomeLifeBitsConfirmMissing",
            last_action="select_second_8h_task",
            log=log,
        )
        if snapshot is None:
            return HomeLifeBitsResult(False, start_state, "life_bits_confirm_missing")

    if snapshot.state is HomeLifeBitsState.LIFE_BITS_CONFIRM:
        if not _click_target(session, snapshot, action="confirm_second_8h_task", log=log):
            if capture is not None:
                capture("QJNN_HomeLifeBitsStartUnconfirmed", snapshot.frame)
            return HomeLifeBitsResult(False, start_state, "confirm_target_missing")
        snapshot = _observe_state(
            session,
            allowed={HomeLifeBitsState.LIFE_BITS_RUNNING, HomeLifeBitsState.ROOM_MAIN},
            capture=capture,
            failure_state="QJNN_HomeLifeBitsStartUnconfirmed",
            last_action="confirm_second_8h_task",
            log=log,
            attempts=40,  # Backend submission can leave the old selection panel visible.
            require_running=True,
            stable_frames=2,
        )
        if snapshot is None:
            return HomeLifeBitsResult(False, start_state, "start_unconfirmed")

        if snapshot.state is HomeLifeBitsState.ROOM_MAIN and snapshot.room_entry_running:
            return HomeLifeBitsResult(True, start_state, "completed")

    if snapshot.state is not HomeLifeBitsState.LIFE_BITS_RUNNING:
        if capture is not None:
            capture("QJNN_HomeLifeBitsIllegalTransition", snapshot.frame)
        return HomeLifeBitsResult(False, start_state, "illegal_transition")

    if dismiss_safe_rect is None:
        if capture is not None:
            capture("QJNN_HomeLifeBitsDismissSafeRectTodo", snapshot.frame)
        _log_diagnostics(
            "QJNN_HomeLifeBitsDismissSafeRectTodo",
            snapshot.items,
            last_action="running_confirmed",
            log=log,
        )
        log(
            "[HomeLifeBits] TODO: running panel SafeRect requires a real "
            "ADB screenshot; zero clicks"
        )
        return HomeLifeBitsResult(False, start_state, "dismiss_safe_rect_unverified")

    safe = _scaled_safe_rect(dismiss_safe_rect, snapshot.frame)
    if safe is None:
        if capture is not None:
            capture("QJNN_HomeLifeBitsDismissSafeRectInvalid", snapshot.frame)
        return HomeLifeBitsResult(False, start_state, "dismiss_safe_rect_invalid")
    action = "dismiss_running_panel"
    log(f"[HomeLifeBits] {action}: SafeRect={safe}, point={safe.center}")
    wait_before_home_room_click(session, action=action, log=log)
    session.click(*safe.center)
    closed = _observe_state(
        session,
        allowed={HomeLifeBitsState.ROOM_MAIN},
        capture=capture,
        failure_state="QJNN_HomeLifeBitsDismissUnconfirmed",
        last_action=action,
        log=log,
    )
    if closed is None:
        return HomeLifeBitsResult(False, start_state, "dismiss_unconfirmed")
    return HomeLifeBitsResult(True, start_state, "completed")


def _make_runtime_session(context):
    controller = context.tasker.controller

    def ocr_items(detail) -> list[OcrItem]:
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
                OcrItem(str(result.text), rect, float(getattr(result, "score", 0.0)))
            )
        return converted

    class RuntimeSession:
        @staticmethod
        def capture_frame():
            job = controller.post_screencap()
            job.wait()
            if hasattr(job, "succeeded") and not job.succeeded:
                raise RuntimeError("小屋生活点滴截图失败")
            cached = getattr(controller, "cached_image", None)
            frame = cached() if callable(cached) else cached
            if frame is None or getattr(frame, "size", 0) == 0:
                raise RuntimeError("小屋生活点滴截图为空")
            return frame

        @staticmethod
        def recognize_text(frame) -> list[OcrItem]:
            detail = context.run_recognition("QJNN_AgentOcrBase", frame)
            return ocr_items(detail)

        @staticmethod
        def click(x: int, y: int) -> None:
            job = controller.post_click(x, y)
            job.wait()
            if hasattr(job, "succeeded") and not job.succeeded:
                raise RuntimeError(f"小屋生活点滴点击失败：({x}, {y})")

        sleep = staticmethod(time.sleep)
        monotonic = staticmethod(time.monotonic)

    return guard_runtime_session(context, RuntimeSession())


class HomeLifeBitsAction(CustomAction):
    def run(self, context, argv) -> bool:
        session = None
        capture = DiagnosticFrameStore(HOME_LIFE_BITS_DIAGNOSTIC_DIR)
        try:
            session = _make_runtime_session(context)
            result = run_home_life_bits(
                session,
                capture=capture.capture,
            )
            print(
                f"[HomeLifeBits] result={result.reason}, "
                f"start_state={result.start_state}"
            )
            return result.success
        except Exception as exc:
            print(f"[HomeLifeBits] SafeStop exception: {exc}")
            if session is not None:
                try:
                    capture.capture(
                        "QJNN_HomeLifeBitsException",
                        session.capture_frame(),
                    )
                except Exception as capture_exc:
                    print(
                        "[HomeLifeBits] exception diagnostic unavailable: "
                        f"{capture_exc}"
                    )
            return False
