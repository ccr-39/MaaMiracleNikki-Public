from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, Sequence, TypeVar

from qjnn_alliance_commission import OcrItem, Rect
from qjnn_common_popup import GlobalTransientResult, handle_transient_if_present


HOME_ROOM_ENTRY_REFERENCE_SIZE = (720, 1280)
# Fixed interior of the bottom-left HOME "小屋" icon.  The footer layout is
# shared by HOME P1/P2/P3, so targeting must not depend on OCR of its label.
HOME_ROOM_ENTRY_SAFE_RECT = (52, 1178, 56, 40)
HOME_ROOM_ENTRY_ATTEMPTS = 3
HOME_ROOM_ENTRY_OBSERVE_ATTEMPTS = 5
HOME_ROOM_ENTRY_POLL_SECONDS = 0.5
# Short human-paced pause after a state/target has been confirmed and before
# an ordinary room-business click.  Random "随便逛逛" jumps intentionally do
# not use this delay because their existing Loading/settling clock owns timing.
HOME_ROOM_PRE_CLICK_DELAY = 0.6
# Deliberately shared by both directions so a click is never judged from the
# controller's pre-transition cached frame.
HOME_ROOM_TRANSITION_WAIT_SECONDS = 1.2
HOME_ROOM_EXIT_REFERENCE_SIZE = (900, 1600)
HOME_ROOM_EXIT_SAFE_RECT = (30, 25, 80, 80)
HOME_ROOM_EXIT_ATTEMPTS = 3
HOME_PAGES = frozenset({"P1", "P2", "P3"})
HOME_TOOLBAR_ANCHORS = frozenset({"衣柜", "成就", "任务", "好友"})


class HomeRoomEntrySession(Protocol):
    def capture_frame(self): ...
    def recognize_text(self, frame) -> list[OcrItem]: ...
    def click(self, x: int, y: int) -> None: ...
    def sleep(self, seconds: float) -> None: ...


SnapshotT = TypeVar("SnapshotT")


@dataclass(frozen=True)
class HomeRoomExitResult:
    page: str
    frame: object
    items: list[OcrItem]


def fixed_home_room_entry_safe_rect(frame) -> Rect | None:
    """Scale the verified 720 x 1280 HOME room-entry SafeRect."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    height, width = frame.shape[:2]
    reference_width, reference_height = HOME_ROOM_ENTRY_REFERENCE_SIZE
    x, y, rect_width, rect_height = HOME_ROOM_ENTRY_SAFE_RECT
    left = round(x * width / reference_width)
    top = round(y * height / reference_height)
    right = round((x + rect_width) * width / reference_width)
    bottom = round((y + rect_height) * height / reference_height)
    rect = Rect(left, top, max(1, right - left), max(1, bottom - top))
    if rect.x < 0 or rect.y < 0 or rect.right > width or rect.bottom > height:
        return None
    return rect


def home_room_exit_safe_rect(frame) -> Rect | None:
    """Scale the verified in-room pink back-button interior."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    height, width = frame.shape[:2]
    reference_width, reference_height = HOME_ROOM_EXIT_REFERENCE_SIZE
    x, y, rect_width, rect_height = HOME_ROOM_EXIT_SAFE_RECT
    left = round(x * width / reference_width)
    top = round(y * height / reference_height)
    right = round((x + rect_width) * width / reference_width)
    bottom = round((y + rect_height) * height / reference_height)
    rect = Rect(left, top, max(1, right - left), max(1, bottom - top))
    if rect.x < 0 or rect.y < 0 or rect.right > width or rect.bottom > height:
        return None
    return rect


def home_toolbar_anchor_count(items: Sequence[OcrItem], frame) -> int:
    """Count optional stable footer anchors without making them a hard gate."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return 0
    height = frame.shape[0]
    footer_top = round(1080 * height / HOME_ROOM_ENTRY_REFERENCE_SIZE[1])
    found = {
        "".join(str(item.text).split())
        for item in items
        if float(item.score) >= 0.60 and item.box.y >= footer_top
    }
    return len(found & HOME_TOOLBAR_ANCHORS)


def wait_before_home_room_click(
    session: HomeRoomEntrySession,
    *,
    action: str,
    log: Callable[[str], None],
) -> None:
    """Apply the shared pre-click cadence after state validation."""
    log(
        f"[HomeRoomClick] {action}: confirmed; "
        f"pre_click_delay={HOME_ROOM_PRE_CLICK_DELAY:.1f}s"
    )
    session.sleep(HOME_ROOM_PRE_CLICK_DELAY)


def enter_home_room(
    session: HomeRoomEntrySession,
    initial_snapshot: SnapshotT,
    *,
    detect_state: Callable[[Sequence[OcrItem], object], SnapshotT | None],
    state_of: Callable[[SnapshotT], object],
    home_states: set[object],
    room_state: object,
    capture: Callable[[str, object], bool] | None,
    failure_state: str,
    log: Callable[[str], None],
    log_prefix: str,
) -> SnapshotT | None:
    """Enter the room from any confirmed HOME page and verify it on fresh frames."""
    current = initial_snapshot
    last_frame = getattr(initial_snapshot, "frame", None)
    last_items = list(getattr(initial_snapshot, "items", []))

    for click_attempt in range(HOME_ROOM_ENTRY_ATTEMPTS):
        if state_of(current) not in home_states:
            break
        target = fixed_home_room_entry_safe_rect(last_frame)
        if target is None:
            break
        anchors = home_toolbar_anchor_count(last_items, last_frame)
        log(
            f"[{log_prefix}] enter_room attempt={click_attempt + 1}/"
            f"{HOME_ROOM_ENTRY_ATTEMPTS}, toolbar_anchors={anchors}, "
            f"SafeRect={target}, point={target.center}"
        )
        wait_before_home_room_click(session, action="enter_room", log=log)
        # A delayed transition can finish during the pre-click pause. Never
        # reuse HOME coordinates on a room frame (they hit the expand button).
        last_frame = session.capture_frame()
        last_items = list(session.recognize_text(last_frame))
        current = detect_state(last_items, last_frame)
        if current is not None and state_of(current) == room_state:
            log(f"[{log_prefix}] ROOM_MAIN confirmed before entry input; zero extra clicks")
            return current
        if current is None or state_of(current) not in home_states:
            break
        target = fixed_home_room_entry_safe_rect(last_frame)
        if target is None:
            break
        session.click(*target.center)

        # Every verification starts after the transition grace period and from
        # a forced new screenshot.  No retry/business click can precede it.
        session.sleep(HOME_ROOM_TRANSITION_WAIT_SECONDS)
        retry_snapshot = None
        for observe_attempt in range(HOME_ROOM_ENTRY_OBSERVE_ATTEMPTS):
            if observe_attempt > 0:
                session.sleep(HOME_ROOM_ENTRY_POLL_SECONDS)
            last_frame = session.capture_frame()
            last_items = list(session.recognize_text(last_frame))
            retry_snapshot = None
            candidate = detect_state(last_items, last_frame)
            if candidate is not None and state_of(candidate) == room_state:
                log(
                    f"[{log_prefix}] ROOM_MAIN confirmed on fresh frame, "
                    f"attempt={observe_attempt + 1}/"
                    f"{HOME_ROOM_ENTRY_OBSERVE_ATTEMPTS}"
                )
                return candidate
            if candidate is not None and state_of(candidate) in home_states:
                retry_snapshot = candidate
                # The controller can still expose the pre-click HOME frame.
                # Observe the whole transition window before authorizing retry.
                continue

            transient, fresh_frame, fresh_items = handle_transient_if_present(
                session,
                last_frame,
                last_items,
                capture=capture,
                log=log,
            )
            if transient is GlobalTransientResult.ERROR:
                last_frame, last_items = fresh_frame, list(fresh_items)
                break
            if transient in {
                GlobalTransientResult.HANDLED_POPUP,
                GlobalTransientResult.WAITED_LOADING,
            }:
                last_frame, last_items = fresh_frame, list(fresh_items)
                candidate = detect_state(last_items, last_frame)
                if candidate is not None and state_of(candidate) == room_state:
                    log(f"[{log_prefix}] ROOM_MAIN confirmed after transient")
                    return candidate
                if candidate is not None and state_of(candidate) in home_states:
                    retry_snapshot = candidate
                    continue
            retry_snapshot = None
        if retry_snapshot is None:
            break
        current = retry_snapshot

    if capture is not None and last_frame is not None:
        capture(failure_state, last_frame)
    log(
        f"[{log_prefix}] SafeStop state={failure_state}; "
        f"ROOM_MAIN not confirmed after {HOME_ROOM_ENTRY_ATTEMPTS} attempts"
    )
    return None


def exit_home_room(
    session: HomeRoomEntrySession,
    initial_snapshot: SnapshotT,
    *,
    detect_state: Callable[[Sequence[OcrItem], object], SnapshotT | None],
    state_of: Callable[[SnapshotT], object],
    room_state: object,
    page_detector: Callable[[object], object],
    capture: Callable[[str, object], bool] | None,
    failure_state: str,
    log: Callable[[str], None],
    log_prefix: str,
) -> HomeRoomExitResult | None:
    """Exit ROOM_MAIN safely; retry only from a fresh confirmed ROOM_MAIN."""
    current = initial_snapshot
    last_frame = getattr(initial_snapshot, "frame", None)
    last_items = list(getattr(initial_snapshot, "items", []))

    for click_attempt in range(HOME_ROOM_EXIT_ATTEMPTS):
        if state_of(current) != room_state:
            break
        target = home_room_exit_safe_rect(last_frame)
        if target is None:
            break
        log(
            f"[{log_prefix}] exit_room attempt={click_attempt + 1}/"
            f"{HOME_ROOM_EXIT_ATTEMPTS}, SafeRect={target}, point={target.center}"
        )
        wait_before_home_room_click(session, action="exit_room", log=log)
        # A preceding visiting-room return may still be completing. Its cached
        # ROOM_MAIN frame must not authorize a click on the HOME avatar now
        # occupying this location. Recheck after the ordinary pre-click delay.
        last_frame = session.capture_frame()
        last_items = list(session.recognize_text(last_frame))
        page = str(getattr(page_detector(last_frame), "page", "Unknown"))
        if page in HOME_PAGES:
            log(f"[{log_prefix}] HOME {page} confirmed before exit input; zero extra clicks")
            return HomeRoomExitResult(page, last_frame, last_items)
        current = detect_state(last_items, last_frame)
        if current is None or state_of(current) != room_state:
            break
        target = home_room_exit_safe_rect(last_frame)
        if target is None:
            break
        session.click(*target.center)
        session.sleep(HOME_ROOM_TRANSITION_WAIT_SECONDS)

        retry_snapshot = None
        for observe_attempt in range(HOME_ROOM_ENTRY_OBSERVE_ATTEMPTS):
            if observe_attempt > 0:
                session.sleep(HOME_ROOM_ENTRY_POLL_SECONDS)
            last_frame = session.capture_frame()
            last_items = list(session.recognize_text(last_frame))
            page = str(getattr(page_detector(last_frame), "page", "Unknown"))
            if page in HOME_PAGES:
                log(f"[{log_prefix}] HOME {page} confirmed; stop exit clicks")
                return HomeRoomExitResult(page, last_frame, last_items)
            candidate = detect_state(last_items, last_frame)
            if candidate is not None and state_of(candidate) == room_state:
                retry_snapshot = candidate
                continue
            retry_snapshot = None
            transient, fresh_frame, fresh_items = handle_transient_if_present(
                session, last_frame, last_items, capture=capture, log=log,
            )
            last_frame, last_items = fresh_frame, list(fresh_items)
            if transient is GlobalTransientResult.ERROR:
                break
            if transient in {
                GlobalTransientResult.HANDLED_POPUP,
                GlobalTransientResult.WAITED_LOADING,
            }:
                page = str(getattr(page_detector(last_frame), "page", "Unknown"))
                if page in HOME_PAGES:
                    log(f"[{log_prefix}] HOME {page} confirmed; stop exit clicks")
                    return HomeRoomExitResult(page, last_frame, last_items)
                candidate = detect_state(last_items, last_frame)
                if candidate is not None and state_of(candidate) == room_state:
                    retry_snapshot = candidate
        if retry_snapshot is None:
            break
        current = retry_snapshot

    if capture is not None and last_frame is not None:
        capture(failure_state, last_frame)
    log(f"[{log_prefix}] SafeStop state={failure_state}; HOME not confirmed")
    return None
