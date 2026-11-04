from __future__ import annotations

from app_paths import DATA_ROOT

import math
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Callable, Protocol, Sequence

from qjnn_alliance_commission import OcrItem, Rect
from qjnn_common_popup import GlobalTransientResult, handle_transient_if_present, detect_common_safe_popup
from qjnn_diagnostic import DiagnosticFrameStore
from qjnn_home import detect_home_page
from qjnn_home_progress import HomeTaskProgress, RANDOM_VISIT_LIMIT
from qjnn_home_life_bits import (
    HomeLifeBitsState,
    _make_runtime_session,
    detect_home_life_bits_state,
)
from qjnn_home_room_entry import (
    HOME_ROOM_TRANSITION_WAIT_SECONDS,
    enter_home_room,
    exit_home_room,
    home_room_exit_safe_rect,
    wait_before_home_room_click,
)

try:
    from maa.custom_action import CustomAction
except ImportError:  # MaaFramework is optional for static and offline tests.
    CustomAction = object


PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOME_RANDOM_VISIT_DIAGNOSTIC_DIR = (
    DATA_ROOT / "images" / "debug" / "pipeline" / "home_random_visit"
)
# Include one extra confirmed jump: initial menu entry is not proof that the
# game's daily visit counter advanced. Never count a click without a transition.
RANDOM_VISIT_TARGET = RANDOM_VISIT_LIMIT
RANDOM_VISIT_OCR_THRESHOLD = 0.60
RANDOM_VISIT_STATE_ATTEMPTS = 5
RANDOM_VISIT_POLL_SECONDS = 0.5
RANDOM_VISIT_RETURN_ATTEMPTS = 3
VISIT_MENU_OPEN_ATTEMPTS = 3
VISIT_MENU_OPEN_TIMEOUT_SECONDS = 20.0
RANDOM_VISIT_TRANSITION_TIMEOUT_SECONDS = 20.0
RANDOM_VISIT_LOADING_TIMEOUT_SECONDS = 20.0
RANDOM_VISIT_STABLE_ROOM_FRAMES = 3
RANDOM_VISIT_STABLE_ROOM_MIN_SECONDS = 1.5
RANDOM_VISIT_TRANSITION_MISS_FRAMES = 2
RANDOM_VISIT_TRANSITION_MIN_SECONDS = 0.5
RANDOM_VISIT_LOADING_TRACE_SECONDS = 8.0
RANDOM_VISIT_LOADING_TRACE_INTERVAL_SECONDS = 0.2
RANDOM_VISIT_LOADING_TRACE_MAX_SAVED_FRAMES = 30
HOME_RANDOM_VISIT_REFERENCE_SIZE = (720, 1280)
HOME_RANDOM_VISIT_LOADING_TRACE_DIR = (
    HOME_RANDOM_VISIT_DIAGNOSTIC_DIR / "loading_trace"
)
HOME_RANDOM_VISIT_LOADING_TEMPLATE_DIR = (
    PROJECT_ROOT / "assets" / "resource" / "image" / "home_random_visit"
)
HOME_RANDOM_VISIT_LOADING_TEMPLATES = tuple(
    HOME_RANDOM_VISIT_LOADING_TEMPLATE_DIR / f"loading_snowflake_{index:02d}.png"
    for index in range(1, 4)
)
HOME_RANDOM_VISIT_LOADING_ROI = (310, 585, 100, 105)
HOME_RANDOM_VISIT_LOADING_TEMPLATE_SIZE = (65, 69)
HOME_RANDOM_VISIT_LOADING_THRESHOLD = 0.46
HOME_RANDOM_VISIT_LOADING_CANNY = (50, 120)

# Verified offline against QJNN_HomeRandomVisitEntrySafeRectTodo_001.png.
# Runtime derives this reference result from the current "外出拜访" OCR label;
# the label itself is not treated as the interactive surface.
HOME_RANDOM_VISIT_ENTRY_SAFE_RECT = (642, 669, 44, 44)
VISIT_ENTRY_ICON_SAFE_SIZE = (44, 44)
VISIT_ENTRY_ICON_CENTER_Y_FROM_TEXT_TOP = -35
# Verified against the real VISIT_MENU screenshots.  The OCR anchor must stay
# inside the third button, then runtime clicks this fixed interior SafeRect.
VISIT_MENU_RANDOM_BUTTON_RECT = (256, 616, 206, 84)
VISIT_MENU_RANDOM_BUTTON_SAFE_RECT = (294, 627, 150, 50)
# QJNN_HomeRandomVisitLoadingClassifierTodo_001.png is historically named but
# is actually a fully loaded VISITING_ROOM frame, not a Loading positive.
VISITING_ROOM_RANDOM_ICON_SAFE_RECT = (632, 1168, 48, 48)
VISITING_ROOM_RANDOM_ICON_SAFE_SIZE = (48, 48)
VISITING_ROOM_RANDOM_ICON_CENTER_X_FROM_TEXT_CENTER = 12
VISITING_ROOM_RANDOM_ICON_CENTER_Y_FROM_TEXT_TOP = -34
# Verified offline against QJNN_HomeRandomVisitBackSafeRectTodo_001.png.
# The pink circular button spans about x=8..105, y=3..102 at 720x1280;
# this conservative interior rectangle stays clear of its circular edge.
VISITING_ROOM_BACK_SAFE_RECT = (28, 22, 56, 56)

Targeter = Callable[[OcrItem, object], Rect | None]
VisitingClassifier = Callable[[Sequence[OcrItem], object], bool]
LoadingClassifier = Callable[[Sequence[OcrItem], object], bool]
LoadingTraceWriter = Callable[[int, float, object], bool]


class RandomVisitState(str, Enum):
    HOME_P1 = "HOME_P1"
    HOME_P2 = "HOME_P2"
    HOME_P3 = "HOME_P3"
    ROOM_MAIN = "ROOM_MAIN"
    VISIT_MENU = "VISIT_MENU"
    VISITING_ROOM = "VISITING_ROOM"


class RandomVisitTransitionPhase(str, Enum):
    CLICKED_PENDING = "CLICKED_PENDING"
    TRANSITION_STARTED = "TRANSITION_STARTED"
    SETTLING = "SETTLING"
    STABLE_VISITING_ROOM = "STABLE_VISITING_ROOM"


@dataclass(frozen=True)
class RandomVisitSnapshot:
    state: RandomVisitState
    frame: object
    items: list[OcrItem]
    target: Rect | None = None
    back_target: Rect | None = None


@dataclass(frozen=True)
class RandomVisitResult:
    success: bool
    start_state: str
    successful_visits: int
    reason: str
    final_snapshot: RandomVisitSnapshot | None = None


@dataclass(frozen=True)
class LoadingTraceResult:
    captured_frames: int
    saved_frames: int
    elapsed_seconds: float


@dataclass(frozen=True)
class LoadingProbe:
    score: float
    template_index: int | None


@dataclass(frozen=True)
class LoadingMissTraceFrame:
    frame_index: int
    elapsed_seconds: float
    frame: object


class RandomVisitSession(Protocol):
    def capture_frame(self): ...
    def recognize_text(self, frame) -> list[OcrItem]: ...
    def click(self, x: int, y: int) -> None: ...
    def sleep(self, seconds: float) -> None: ...
    def monotonic(self) -> float: ...


def _normalize(value: str) -> str:
    return "".join(str(value).split())


def _confident(items: Sequence[OcrItem]) -> list[OcrItem]:
    return [item for item in items if float(item.score) >= RANDOM_VISIT_OCR_THRESHOLD]


def _exact(items: Sequence[OcrItem], text: str) -> OcrItem | None:
    expected = _normalize(text)
    return next((item for item in items if _normalize(item.text) == expected), None)


def _valid_rect(rect: Rect | None) -> bool:
    return rect is not None and rect.width > 0 and rect.height > 0


@lru_cache(maxsize=1)
def _load_home_random_visit_loading_templates():
    import cv2
    import numpy as np

    expected_width, expected_height = HOME_RANDOM_VISIT_LOADING_TEMPLATE_SIZE
    templates = []
    for path in HOME_RANDOM_VISIT_LOADING_TEMPLATES:
        data = np.fromfile(path, dtype=np.uint8)
        template = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
        if template is None:
            raise RuntimeError(f"无法读取小屋随机拜访 Loading 模板：{path}")
        if template.shape != (expected_height, expected_width):
            raise RuntimeError(
                "小屋随机拜访 Loading 模板尺寸错误："
                f"{path}, actual={template.shape}, "
                f"expected={(expected_height, expected_width)}"
            )
        templates.append(
            cv2.Canny(template, *HOME_RANDOM_VISIT_LOADING_CANNY)
        )

    yy, xx = np.ogrid[:expected_height, :expected_width]
    mask = (
        (
            (xx - expected_width / 2) ** 2 / 31**2
            + (yy - expected_height / 2) ** 2 / 33**2
        )
        <= 1
    ).astype(np.uint8) * 255
    return tuple(templates), mask


def home_random_visit_loading_probe(frame) -> LoadingProbe:
    """Score the real snowflake and identify the best 1-based template."""
    import cv2
    import numpy as np

    if frame is None or getattr(frame, "size", 0) == 0:
        return LoadingProbe(0.0, None)
    reference_width, reference_height = HOME_RANDOM_VISIT_REFERENCE_SIZE
    height, width = frame.shape[:2]
    if (width, height) != (reference_width, reference_height):
        interpolation = (
            cv2.INTER_AREA
            if width > reference_width or height > reference_height
            else cv2.INTER_CUBIC
        )
        frame = cv2.resize(
            frame,
            (reference_width, reference_height),
            interpolation=interpolation,
        )

    roi = Rect(*HOME_RANDOM_VISIT_LOADING_ROI)
    search = frame[roi.y : roi.bottom, roi.x : roi.right]
    search_edges = cv2.Canny(
        cv2.cvtColor(search, cv2.COLOR_BGR2GRAY),
        *HOME_RANDOM_VISIT_LOADING_CANNY,
    )
    templates, mask = _load_home_random_visit_loading_templates()
    best = 0.0
    best_template_index: int | None = None
    for template_index, template in enumerate(templates, start=1):
        scores = cv2.matchTemplate(
            search_edges,
            template,
            cv2.TM_CCORR_NORMED,
            mask=mask,
        )
        finite = scores[np.isfinite(scores)]
        if finite.size:
            score = float(finite.max())
            if score > best:
                best = score
                best_template_index = template_index
    return LoadingProbe(best, best_template_index)


def home_random_visit_loading_score(frame) -> float:
    """Compatibility API returning only the best real-snowflake score."""
    return home_random_visit_loading_probe(frame).score


def home_random_visit_loading_classifier(
    items: Sequence[OcrItem], frame
) -> bool:
    """Detect only the verified HomeRandomVisit center snowflake."""
    del items
    return (
        home_random_visit_loading_probe(frame).score
        >= HOME_RANDOM_VISIT_LOADING_THRESHOLD
    )


def _visiting_room_random_entry(
    items: Sequence[OcrItem], frame
) -> OcrItem | None:
    """Return only a bottom-right random-visit label, never the menu button."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    height, width = frame.shape[:2]
    candidates = [
        item
        for item in items
        if _normalize(item.text) == "随便逛逛"
        and item.box.center[0] >= width * 0.70
        and item.box.center[1] >= height * 0.85
    ]
    return max(candidates, key=lambda item: (item.box.center[1], item.box.center[0]), default=None)


def visit_entry_icon_safe_rect(entry_box: Rect, frame) -> Rect | None:
    """Project the ROOM_MAIN label upward into the visit character icon."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    height, width = frame.shape[:2]
    ref_width, ref_height = HOME_RANDOM_VISIT_REFERENCE_SIZE
    safe_width = max(1, round(VISIT_ENTRY_ICON_SAFE_SIZE[0] * width / ref_width))
    safe_height = max(1, round(VISIT_ENTRY_ICON_SAFE_SIZE[1] * height / ref_height))
    center_x = entry_box.center[0]
    center_y = entry_box.y + round(
        VISIT_ENTRY_ICON_CENTER_Y_FROM_TEXT_TOP * height / ref_height
    )
    safe = Rect(
        center_x - safe_width // 2,
        center_y - safe_height // 2,
        safe_width,
        safe_height,
    )
    if safe.x < 0 or safe.y < 0 or safe.right > width or safe.bottom > height:
        return None
    if safe.bottom >= entry_box.y:
        return None
    return safe


def visit_menu_random_button_safe_rect(anchor: OcrItem, frame) -> Rect | None:
    """Build an interior Rect for the VISIT_MENU orange random button."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    if _normalize(anchor.text) != "随便逛逛":
        return None
    height, width = frame.shape[:2]
    ref_width, ref_height = HOME_RANDOM_VISIT_REFERENCE_SIZE
    def scaled(reference: tuple[int, int, int, int]) -> Rect:
        left = round(reference[0] * width / ref_width)
        top = round(reference[1] * height / ref_height)
        right = round((reference[0] + reference[2]) * width / ref_width)
        bottom = round((reference[1] + reference[3]) * height / ref_height)
        return Rect(left, top, max(1, right - left), max(1, bottom - top))

    button = scaled(VISIT_MENU_RANDOM_BUTTON_RECT)
    safe = scaled(VISIT_MENU_RANDOM_BUTTON_SAFE_RECT)
    # Validate the OCR anchor against the whole third-button surface.  The
    # click itself stays in the smaller fixed interior and never follows an
    # OCR edge.
    if (
        anchor.box.x < button.x
        or anchor.box.y < button.y
        or anchor.box.right > button.right
        or anchor.box.bottom > button.bottom
    ):
        return None
    return safe


def visiting_room_classifier(items: Sequence[OcrItem], frame) -> bool:
    """Recognize stable visit controls without using variable room content."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return False
    confident = _confident(items)
    menu_labels = [
        _exact(confident, text)
        for text in ("拜访好友", "拜访联盟成员", "随便逛逛", "应邀拜访")
    ]
    if menu_labels[2] is not None and sum(value is not None for value in menu_labels) >= 3:
        return False

    room_selector = _exact(confident, "房间选择")
    retract = _exact(confident, "点击收回")
    random_entry = _visiting_room_random_entry(confident, frame)
    if random_entry is None:
        return False

    height, width = frame.shape[:2]
    left_max = width * 0.25
    stable_anchors = tuple(
        anchor for anchor in (room_selector, retract) if anchor is not None
    )
    return any(
        anchor.box.center[0] <= left_max
        and anchor.box.y < height * 0.35
        for anchor in stable_anchors
    )


def visiting_room_random_icon_safe_rect(anchor: OcrItem, frame) -> Rect | None:
    """Project the bottom-right label into the visiting character icon."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    if _normalize(anchor.text) != "随便逛逛":
        return None
    height, width = frame.shape[:2]
    ref_width, ref_height = HOME_RANDOM_VISIT_REFERENCE_SIZE
    safe_width = max(
        1,
        round(VISITING_ROOM_RANDOM_ICON_SAFE_SIZE[0] * width / ref_width),
    )
    safe_height = max(
        1,
        round(VISITING_ROOM_RANDOM_ICON_SAFE_SIZE[1] * height / ref_height),
    )
    center_x = anchor.box.center[0] + round(
        VISITING_ROOM_RANDOM_ICON_CENTER_X_FROM_TEXT_CENTER * width / ref_width
    )
    center_y = anchor.box.y + round(
        VISITING_ROOM_RANDOM_ICON_CENTER_Y_FROM_TEXT_TOP * height / ref_height
    )
    safe = Rect(
        center_x - safe_width // 2,
        center_y - safe_height // 2,
        safe_width,
        safe_height,
    )
    if safe.x < 0 or safe.y < 0 or safe.right > width or safe.bottom > height:
        return None
    if safe.bottom >= anchor.box.y:
        return None
    return safe


def visiting_room_back_safe_rect(frame) -> Rect | None:
    """Scale the real-screenshot-verified interior of the game back button."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    height, width = frame.shape[:2]
    ref_width, ref_height = HOME_RANDOM_VISIT_REFERENCE_SIZE
    x, y, safe_width, safe_height = VISITING_ROOM_BACK_SAFE_RECT
    left = round(x * width / ref_width)
    top = round(y * height / ref_height)
    right = round((x + safe_width) * width / ref_width)
    bottom = round((y + safe_height) * height / ref_height)
    safe = Rect(left, top, max(1, right - left), max(1, bottom - top))
    if safe.x < 0 or safe.y < 0 or safe.right > width or safe.bottom > height:
        return None
    return safe


def detect_random_visit_state(
    items: Sequence[OcrItem],
    frame,
    *,
    room_visit_targeter: Targeter | None = None,
    menu_targeter: Targeter | None = None,
    visiting_classifier: VisitingClassifier | None = None,
    visiting_targeter: Targeter | None = None,
    back_targeter: Targeter | None = None,
) -> RandomVisitSnapshot | None:
    """Recognize business states from most specific overlay to base pages."""
    confident = _confident(items)

    menu_labels = {
        text: _exact(confident, text)
        for text in ("拜访好友", "拜访联盟成员", "随便逛逛", "应邀拜访")
    }
    menu_random = menu_labels["随便逛逛"]
    if menu_random is not None and sum(value is not None for value in menu_labels.values()) >= 3:
        target = (
            menu_targeter(menu_random, frame)
            if menu_targeter
            else visit_menu_random_button_safe_rect(menu_random, frame)
        )
        return RandomVisitSnapshot(
            RandomVisitState.VISIT_MENU,
            frame,
            list(items),
            target,
        )

    visiting_random = _visiting_room_random_entry(confident, frame)
    classify_visiting = visiting_classifier or visiting_room_classifier
    if classify_visiting(confident, frame) and visiting_random is not None:
        target = (
            visiting_targeter(visiting_random, frame)
            if visiting_targeter
            else visiting_room_random_icon_safe_rect(visiting_random, frame)
        )
        back = (
            back_targeter(visiting_random, frame)
            if back_targeter
            else visiting_room_back_safe_rect(frame)
        )
        return RandomVisitSnapshot(
            RandomVisitState.VISITING_ROOM,
            frame,
            list(items),
            target,
            back,
        )

    shared = detect_home_life_bits_state(confident, frame)
    if shared is None:
        return None
    if shared.state is HomeLifeBitsState.ROOM_MAIN:
        visit_entry = _exact(confident, "外出拜访")
        target = (
            room_visit_targeter(visit_entry, frame)
            if visit_entry is not None and room_visit_targeter is not None
            else (
                visit_entry_icon_safe_rect(visit_entry.box, frame)
                if visit_entry is not None
                else None
            )
        )
        return RandomVisitSnapshot(
            RandomVisitState.ROOM_MAIN,
            frame,
            list(items),
            target,
        )
    if shared.state in {
        HomeLifeBitsState.HOME_P1,
        HomeLifeBitsState.HOME_P2,
        HomeLifeBitsState.HOME_P3,
    }:
        return RandomVisitSnapshot(
            RandomVisitState[shared.state.value],
            frame,
            list(items),
            shared.target,
        )
    return None


def _diagnose(
    state: str,
    frame,
    items: Sequence[OcrItem],
    *,
    capture: Callable[[str, object], bool] | None,
    log: Callable[[str], None],
) -> None:
    if capture is not None and frame is not None:
        capture(state, frame)
    log(f"[HomeRandomVisit] SafeStop state={state}")
    for item in items:
        log(
            f"[HomeRandomVisit] OCR text={item.text!r}, confidence={item.score:.6f}, "
            f"box=({item.box.x},{item.box.y},{item.box.width},{item.box.height})"
        )


def _make_loading_trace_writer(
    output_dir: Path,
    *,
    log: Callable[[str], None],
) -> LoadingTraceWriter:
    """Create one finite trace writer with a unique run id."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = next(
        (
            value
            for value in range(1, 1000)
            if not any(
                output_dir.glob(
                    f"QJNN_HomeRandomVisitLoadingTrace_{value:03d}_*.png"
                )
            )
        ),
        None,
    )
    if run_id is None:
        raise RuntimeError("no free HomeRandomVisit loading trace run id")

    def write(frame_index: int, elapsed: float, frame) -> bool:
        try:
            import cv2

            if frame is None or getattr(frame, "size", 0) == 0:
                return False
            destination = output_dir / (
                f"QJNN_HomeRandomVisitLoadingTrace_{run_id:03d}_"
                f"{frame_index:03d}.png"
            )
            encoded, data = cv2.imencode(".png", frame)
            if not encoded:
                return False
            data.tofile(str(destination))
            log(
                "[HomeRandomVisit] loading trace saved: "
                f"elapsed={elapsed:.2f}s, path={destination}"
            )
            return True
        except Exception as exc:
            log(f"[HomeRandomVisit] loading trace save failed: {exc}")
            return False

    return write


def _capture_loading_trace(
    session: RandomVisitSession,
    *,
    detector_kwargs: dict,
    log: Callable[[str], None],
    writer: LoadingTraceWriter | None = None,
) -> LoadingTraceResult:
    """Capture a finite diagnostic trace without clicks or state mutation."""
    if writer is None:
        writer = _make_loading_trace_writer(
            HOME_RANDOM_VISIT_LOADING_TRACE_DIR,
            log=log,
        )

    started_at = session.monotonic()
    captured_frames = 0
    saved_frames = 0
    save_step = (
        RANDOM_VISIT_LOADING_TRACE_SECONDS
        / RANDOM_VISIT_LOADING_TRACE_MAX_SAVED_FRAMES
    )
    next_save_elapsed = 0.0

    while session.monotonic() - started_at < RANDOM_VISIT_LOADING_TRACE_SECONDS:
        capture_started_at = session.monotonic()
        frame = session.capture_frame()
        items = list(session.recognize_text(frame))
        elapsed = session.monotonic() - started_at
        snapshot = detect_random_visit_state(items, frame, **detector_kwargs)
        confident = _confident(items)
        room_selector = _exact(confident, "房间选择") is not None
        retract = _exact(confident, "点击收回") is not None
        random_entry = _exact(confident, "随便逛逛") is not None
        visiting_room = (
            snapshot is not None
            and snapshot.state is RandomVisitState.VISITING_ROOM
        )
        visit_menu = (
            snapshot is not None and snapshot.state is RandomVisitState.VISIT_MENU
        )
        loading_probe = home_random_visit_loading_probe(frame)
        loading_detected = (
            loading_probe.score >= HOME_RANDOM_VISIT_LOADING_THRESHOLD
        )
        log(
            "[HomeRandomVisit] loading trace: "
            f"frame_index={captured_frames}, elapsed={elapsed:.2f}s, "
            f"loading_score={loading_probe.score:.6f}, "
            f"loading_threshold={HOME_RANDOM_VISIT_LOADING_THRESHOLD:.3f}, "
            f"best_template={loading_probe.template_index}, "
            f"loading_detected={loading_detected}, "
            f"VISITING_ROOM={visiting_room}, VISIT_MENU={visit_menu}, "
            f"room_selector={room_selector}, retract={retract}, "
            f"random_entry={random_entry}"
        )

        if (
            saved_frames < RANDOM_VISIT_LOADING_TRACE_MAX_SAVED_FRAMES
            and elapsed >= next_save_elapsed
        ):
            if writer(saved_frames, elapsed, frame):
                saved_frames += 1
            next_save_elapsed += save_step
        captured_frames += 1

        remaining = RANDOM_VISIT_LOADING_TRACE_SECONDS - (
            session.monotonic() - started_at
        )
        if remaining <= 0:
            break
        capture_elapsed = session.monotonic() - capture_started_at
        delay = min(
            max(0.0, RANDOM_VISIT_LOADING_TRACE_INTERVAL_SECONDS - capture_elapsed),
            remaining,
        )
        if delay > 0:
            session.sleep(delay)

    return LoadingTraceResult(
        captured_frames=captured_frames,
        saved_frames=saved_frames,
        elapsed_seconds=session.monotonic() - started_at,
    )


def _observe_state(
    session: RandomVisitSession,
    *,
    allowed: set[RandomVisitState],
    capture: Callable[[str, object], bool] | None,
    failure_state: str,
    log: Callable[[str], None],
    detector_kwargs: dict,
) -> RandomVisitSnapshot | None:
    last_frame = None
    last_items: list[OcrItem] = []
    for attempt in range(RANDOM_VISIT_STATE_ATTEMPTS):
        last_frame = session.capture_frame()
        last_items = list(session.recognize_text(last_frame))
        snapshot = detect_random_visit_state(last_items, last_frame, **detector_kwargs)
        if snapshot is not None and snapshot.state in allowed:
            log(
                f"[HomeRandomVisit] state={snapshot.state.value}, "
                f"attempt={attempt + 1}/{RANDOM_VISIT_STATE_ATTEMPTS}"
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
            last_frame, last_items = fresh_frame, list(fresh_items)
            break
        if transient is GlobalTransientResult.HANDLED_POPUP:
            snapshot = detect_random_visit_state(
                fresh_items,
                fresh_frame,
                **detector_kwargs,
            )
            if snapshot is not None and snapshot.state in allowed:
                return snapshot
        if attempt + 1 < RANDOM_VISIT_STATE_ATTEMPTS:
            session.sleep(RANDOM_VISIT_POLL_SECONDS)
    _diagnose(failure_state, last_frame, last_items, capture=capture, log=log)
    return None


def _click_once(
    session: RandomVisitSession,
    rect: Rect | None,
    *,
    action: str,
    log: Callable[[str], None],
    pre_click_delay: bool = True,
) -> bool:
    if not _valid_rect(rect):
        log(f"[HomeRandomVisit] {action}: verified target Rect missing; zero clicks")
        return False
    assert rect is not None
    log(f"[HomeRandomVisit] {action}: Rect={rect}, point={rect.center}")
    if pre_click_delay:
        wait_before_home_room_click(session, action=action, log=log)
    session.click(*rect.center)
    return True


def _open_visit_menu(session, *, detector_kwargs, capture, log):
    """Retry only room -> menu navigation, never a random-visit jump.

    The initial room frame may belong to its entrance animation. A click can
    be ignored; every retry therefore needs another fresh room and target.
    """
    deadline = session.monotonic() + VISIT_MENU_OPEN_TIMEOUT_SECONDS
    last_frame, last_items = None, []
    for attempt in range(VISIT_MENU_OPEN_ATTEMPTS):
        wait_before_home_room_click(session, action="open_visit_menu", log=log)
        last_frame = session.capture_frame()
        last_items = list(session.recognize_text(last_frame))
        current = detect_random_visit_state(last_items, last_frame, **detector_kwargs)
        blocked = (detect_common_safe_popup(last_frame, last_items).matched
                   or home_random_visit_loading_classifier(last_items, last_frame))
        if not blocked and current is not None and current.state is RandomVisitState.VISIT_MENU:
            return current  # A delayed menu must never receive the old room click.
        if (session.monotonic() >= deadline or current is None
                or current.state is not RandomVisitState.ROOM_MAIN
                or blocked
                or not _valid_rect(current.target)):
            break
        log(f"[HomeRandomVisit] open_visit_menu attempt={attempt + 1}/{VISIT_MENU_OPEN_ATTEMPTS}; fresh ROOM_MAIN confirmed")
        if not _click_once(session, current.target, action="open_visit_menu", log=log, pre_click_delay=False):
            break
        for poll in range(RANDOM_VISIT_STATE_ATTEMPTS):
            if session.monotonic() >= deadline:
                break
            session.sleep(min(RANDOM_VISIT_POLL_SECONDS, deadline - session.monotonic()))
            last_frame = session.capture_frame()
            last_items = list(session.recognize_text(last_frame))
            current = detect_random_visit_state(last_items, last_frame, **detector_kwargs)
            blocked = (detect_common_safe_popup(last_frame, last_items).matched
                       or home_random_visit_loading_classifier(last_items, last_frame))
            if not blocked and current is not None and current.state is RandomVisitState.VISIT_MENU:
                log("[HomeRandomVisit] visit menu confirmed; stop room entry clicks")
                return current
            if current is not None and current.state is not RandomVisitState.ROOM_MAIN:
                break
        # The next attempt revalidates after the pre-click delay. Unknown,
        # HOME, loading or an overlay can never authorize a navigation retry.
    _diagnose("QJNN_HomeRandomVisitOpenMenuNoTransition", last_frame, last_items,
              capture=capture, log=log)
    return None


def _wait_for_effective_visit(
    session: RandomVisitSession,
    *,
    source_state: RandomVisitState,
    loading_classifier: LoadingClassifier | None,
    capture: Callable[[str, object], bool] | None,
    log: Callable[[str], None],
    detector_kwargs: dict,
    pending_started_at: float | None = None,
    deadline: float | None = None,
    loading_miss_trace_enabled: bool = False,
    loading_miss_trace_writer: LoadingTraceWriter | None = None,
) -> tuple[RandomVisitSnapshot | None, str]:
    """Observe one pending click until a conservatively stable visited room."""
    if pending_started_at is None:
        pending_started_at = session.monotonic()
    if deadline is None:
        deadline = pending_started_at + RANDOM_VISIT_TRANSITION_TIMEOUT_SECONDS
    remaining_timeout = max(0.0, deadline - session.monotonic())
    menu_source = source_state is RandomVisitState.VISIT_MENU
    observation_interval = (
        min(
            RANDOM_VISIT_POLL_SECONDS,
            RANDOM_VISIT_LOADING_TRACE_INTERVAL_SECONDS,
        )
        if loading_miss_trace_enabled and not menu_source
        else RANDOM_VISIT_POLL_SECONDS
    )
    attempts = max(
        1,
        math.ceil(remaining_timeout / observation_interval),
    )
    loading_seen = False
    transition_started = False
    phase = RandomVisitTransitionPhase.CLICKED_PENDING
    transition_miss_frames = 0
    transition_miss_started_at: float | None = None
    stable_frames = 0
    stable_started_at: float | None = None
    last_frame = None
    last_items: list[OcrItem] = []
    pending: tuple[object, Sequence[OcrItem]] | None = None
    max_loading_probe = LoadingProbe(0.0, None)
    max_loading_score_elapsed = 0.0
    max_loading_score_frame = None
    loading_miss_trace: list[LoadingMissTraceFrame] = []
    next_trace_elapsed = 0.0
    trace_step = (
        RANDOM_VISIT_LOADING_TRACE_SECONDS
        / RANDOM_VISIT_LOADING_TRACE_MAX_SAVED_FRAMES
    )
    log(
        "[HomeRandomVisit] transition pending started: "
        f"source={source_state.value}"
    )

    for frame_index in range(attempts):
        if pending is None:
            last_frame = session.capture_frame()
            last_items = list(session.recognize_text(last_frame))
        else:
            last_frame, pending_items = pending
            last_items = list(pending_items)
            pending = None

        snapshot = detect_random_visit_state(last_items, last_frame, **detector_kwargs)
        stable_visiting = (
            snapshot is not None
            and snapshot.state is RandomVisitState.VISITING_ROOM
        )
        elapsed = session.monotonic() - pending_started_at
        loading_probe = home_random_visit_loading_probe(last_frame)
        if (
            max_loading_score_frame is None
            or loading_probe.score > max_loading_probe.score
        ):
            max_loading_probe = loading_probe
            max_loading_score_elapsed = elapsed
            max_loading_score_frame = (
                last_frame.copy() if hasattr(last_frame, "copy") else last_frame
            )
        if (
            loading_miss_trace_enabled
            and not menu_source
            and elapsed <= RANDOM_VISIT_LOADING_TRACE_SECONDS
            and len(loading_miss_trace)
            < RANDOM_VISIT_LOADING_TRACE_MAX_SAVED_FRAMES
            and elapsed >= next_trace_elapsed
        ):
            loading_miss_trace.append(
                LoadingMissTraceFrame(
                    frame_index,
                    elapsed,
                    last_frame.copy()
                    if hasattr(last_frame, "copy")
                    else last_frame,
                )
            )
            next_trace_elapsed += trace_step
        if loading_classifier is home_random_visit_loading_classifier:
            loading_on_frame = (
                loading_probe.score >= HOME_RANDOM_VISIT_LOADING_THRESHOLD
            )
        else:
            loading_on_frame = (
                loading_classifier is not None
                and loading_classifier(last_items, last_frame)
            )
        if not menu_source:
            log(
                "[HomeRandomVisit] loading probe: "
                f"elapsed={elapsed:.2f}s, score={loading_probe.score:.6f}, "
                f"threshold={HOME_RANDOM_VISIT_LOADING_THRESHOLD:.3f}, "
                f"template={loading_probe.template_index}, "
                f"loading={loading_on_frame}, "
                f"VISITING_ROOM={stable_visiting}"
            )
        if not menu_source and not transition_started and loading_on_frame:
            transition_started = True
            phase = RandomVisitTransitionPhase.TRANSITION_STARTED
            log(
                "[HomeRandomVisit] loading detected; "
                "transition_started=True"
            )
        settled_visiting = stable_visiting and not loading_on_frame

        if menu_source and settled_visiting:
            # Entering visit mode is navigation, not one of the five room hops.
            # Its first frame can precede a delayed loading overlay.
            transition_started = True
        if transition_started:
            phase = RandomVisitTransitionPhase.SETTLING
            if settled_visiting:
                now = session.monotonic()
                if stable_frames == 0:
                    stable_started_at = now
                stable_frames += 1
                stable_duration = now - (
                    stable_started_at if stable_started_at is not None else now
                )
                log(
                    "[HomeRandomVisit] settling: "
                    f"stable_visiting={stable_frames}/"
                    f"{RANDOM_VISIT_STABLE_ROOM_FRAMES}, "
                    f"duration={stable_duration:.2f}s"
                )
                if (
                    stable_frames >= RANDOM_VISIT_STABLE_ROOM_FRAMES
                    and stable_duration + 1e-6 >= RANDOM_VISIT_STABLE_ROOM_MIN_SECONDS
                ):
                    phase = RandomVisitTransitionPhase.STABLE_VISITING_ROOM
                    log("[HomeRandomVisit] transition completed")
                    return snapshot, "completed"
            else:
                if stable_frames:
                    interruption = " by loading" if loading_on_frame else ""
                    log(
                        "[HomeRandomVisit] settling interrupted"
                        f"{interruption}; stable counter reset"
                    )
                stable_frames = 0
                stable_started_at = None

        transient, fresh_frame, fresh_items = handle_transient_if_present(
            session,
            last_frame,
            last_items,
            loading_classifier=loading_classifier,
            loading_timeout=RANDOM_VISIT_LOADING_TIMEOUT_SECONDS,
            loading_interval=RANDOM_VISIT_POLL_SECONDS,
            loading_timeout_state="QJNN_HomeRandomVisitLoadingTimeout",
            capture=capture,
            loading_capture=capture,
            log=log,
        )
        if transient is GlobalTransientResult.ERROR:
            if not menu_source and transition_started:
                elapsed = session.monotonic() - pending_started_at
                log(
                    "[HomeRandomVisit] transition diagnostic: "
                    f"source={source_state.value}, phase={phase.value}, "
                    f"transition_started=True, stable_count={stable_frames}, "
                    f"elapsed={elapsed:.2f}s"
                )
                _diagnose(
                    "QJNN_HomeRandomVisitTransitionNotStable",
                    fresh_frame if fresh_frame is not None else last_frame,
                    list(fresh_items) if fresh_items is not None else last_items,
                    capture=capture,
                    log=log,
                )
                return None, "transition_not_stable"
            return None, "loading_or_popup_error"
        if transient is GlobalTransientResult.WAITED_LOADING:
            loading_seen = True
            if not menu_source and not transition_started:
                transition_started = True
                phase = RandomVisitTransitionPhase.TRANSITION_STARTED
                log(
                    "[HomeRandomVisit] loading detected; "
                    "transition_started=True"
                )
            transition_miss_frames = 0
            transition_miss_started_at = None
            pending = (fresh_frame, fresh_items)
            continue
        if transient is GlobalTransientResult.HANDLED_POPUP:
            # A whitelisted popup is an overlay, not visit-transition evidence.
            transition_miss_frames = 0
            transition_miss_started_at = None
            pending = (fresh_frame, fresh_items)
            continue

        if not menu_source and not transition_started:
            if not stable_visiting:
                now = session.monotonic()
                if transition_miss_frames == 0:
                    transition_miss_started_at = now
                transition_miss_frames += 1
                miss_duration = now - (
                    transition_miss_started_at
                    if transition_miss_started_at is not None
                    else now
                )
                if (
                    transition_miss_frames >= RANDOM_VISIT_TRANSITION_MISS_FRAMES
                    and miss_duration >= RANDOM_VISIT_TRANSITION_MIN_SECONDS
                ):
                    transition_started = True
                    phase = RandomVisitTransitionPhase.TRANSITION_STARTED
                    log(
                        "[HomeRandomVisit] transition_started by non-visiting "
                        f"frames: frames={transition_miss_frames}, "
                        f"duration={miss_duration:.2f}s"
                    )
            else:
                transition_miss_frames = 0
                transition_miss_started_at = None

        remaining = deadline - session.monotonic()
        if remaining <= 0:
            break
        session.sleep(min(observation_interval, remaining))

    if not menu_source and transition_started:
        diagnostic = "QJNN_HomeRandomVisitTransitionNotStable"
        reason = "transition_not_stable"
    elif menu_source and loading_seen:
        diagnostic = "QJNN_HomeRandomVisitVisitingRoomNotStable"
        reason = "visiting_room_not_stable"
    else:
        diagnostic = "QJNN_HomeRandomVisitClickNoTransition"
        reason = "click_no_transition"
    elapsed = session.monotonic() - pending_started_at
    if diagnostic == "QJNN_HomeRandomVisitClickNoTransition" and not menu_source:
        log(
            "[HomeRandomVisit] loading miss diagnostic: "
            f"max_loading_score={max_loading_probe.score:.6f}, "
            f"at={max_loading_score_elapsed:.2f}s, "
            f"template={max_loading_probe.template_index}, "
            f"threshold={HOME_RANDOM_VISIT_LOADING_THRESHOLD:.3f}"
        )
        if max_loading_score_frame is not None and capture is not None:
            capture(
                "QJNN_HomeRandomVisitLoadingMissBest",
                max_loading_score_frame,
            )
        if loading_miss_trace_enabled and loading_miss_trace:
            writer = loading_miss_trace_writer or _make_loading_trace_writer(
                HOME_RANDOM_VISIT_LOADING_TRACE_DIR,
                log=log,
            )
            saved = 0
            for trace_frame in loading_miss_trace:
                if writer(
                    saved,
                    trace_frame.elapsed_seconds,
                    trace_frame.frame,
                ):
                    saved += 1
            log(
                "[HomeRandomVisit] loading miss trace saved: "
                f"captured={len(loading_miss_trace)}, saved={saved}, "
                f"window={min(elapsed, RANDOM_VISIT_LOADING_TRACE_SECONDS):.2f}s"
            )
    log(
        "[HomeRandomVisit] transition diagnostic: "
        f"source={source_state.value}, phase={phase.value}, "
        f"transition_started={transition_started}, "
        f"stable_count={stable_frames}, elapsed={elapsed:.2f}s"
    )
    _diagnose(diagnostic, last_frame, last_items, capture=capture, log=log)
    return None, reason


def run_home_random_visit(
    session: RandomVisitSession,
    *,
    capture: Callable[[str, object], bool] | None = None,
    log: Callable[[str], None] = print,
    loading_classifier: LoadingClassifier | None = home_random_visit_loading_classifier,
    room_visit_targeter: Targeter | None = None,
    menu_targeter: Targeter | None = None,
    visiting_classifier: VisitingClassifier | None = None,
    visiting_targeter: Targeter | None = None,
    back_targeter: Targeter | None = None,
    loading_trace_enabled: bool = False,
    loading_trace_writer: LoadingTraceWriter | None = None,
    loading_miss_trace_enabled: bool = True,
    loading_miss_trace_writer: LoadingTraceWriter | None = None,
    home_page_detector: Callable[[object], object] = detect_home_page,
    exit_to_home: bool = True,
    progress: HomeTaskProgress | None = None,
) -> RandomVisitResult:
    # Automatic re-entry belongs to this queue, not a new round of five hops.
    progress = progress if progress is not None else HomeTaskProgress()
    successful_visits = progress.successful_visits
    if progress.visit_pending:
        log(f"[HomeRandomVisit] unresolved prior jump at {successful_visits}/{RANDOM_VISIT_TARGET}; no repeat random click")
        return RandomVisitResult(False, "RESUME", successful_visits, "prior_visit_unconfirmed")
    if successful_visits:
        log(f"[HomeRandomVisit] resuming queue progress: {successful_visits}/{RANDOM_VISIT_TARGET}")
    detector_kwargs = {
        "room_visit_targeter": room_visit_targeter,
        "menu_targeter": menu_targeter,
        "visiting_classifier": visiting_classifier,
        "visiting_targeter": visiting_targeter,
        "back_targeter": back_targeter,
    }
    snapshot = _observe_state(
        session,
        allowed=set(RandomVisitState),
        capture=capture,
        failure_state="QJNN_HomeRandomVisitUnknownStart",
        log=log,
        detector_kwargs=detector_kwargs,
    )
    if snapshot is None:
        return RandomVisitResult(False, "UNKNOWN", successful_visits, "unknown_start")
    start_state = snapshot.state.value

    home_states = {
        RandomVisitState.HOME_P1,
        RandomVisitState.HOME_P2,
        RandomVisitState.HOME_P3,
    }
    if successful_visits == RANDOM_VISIT_TARGET:
        log("[HomeRandomVisit] visit limit already confirmed; return/exit only")
        if snapshot.state in home_states:
            # Recovery may already have returned HOME. Never enter the room or
            # open a visit menu just to finish a completed round.
            if str(getattr(home_page_detector(snapshot.frame), "page", "Unknown")) not in {"P1", "P2", "P3"}:
                return RandomVisitResult(False, start_state, successful_visits, "home_not_confirmed")
            session.sleep(RANDOM_VISIT_POLL_SECONDS)
            frame = session.capture_frame()
            items = list(session.recognize_text(frame))
            page = str(getattr(home_page_detector(frame), "page", "Unknown"))
            if page not in {"P1", "P2", "P3"}:
                return RandomVisitResult(False, start_state, successful_visits, "home_not_confirmed")
            return RandomVisitResult(True, start_state, successful_visits, "completed",
                                     RandomVisitSnapshot(RandomVisitState[f"HOME_{page}"], frame, items))
        if snapshot.state is RandomVisitState.ROOM_MAIN:
            return _finish_from_own_room(
                session, snapshot, successful_visits, start_state,
                exit_to_home=exit_to_home, home_page_detector=home_page_detector,
                detector_kwargs=detector_kwargs, capture=capture, log=log,
            )
        if snapshot.state is not RandomVisitState.VISITING_ROOM:
            return RandomVisitResult(False, start_state, successful_visits, "completed_visits_exit_unconfirmed")
    if snapshot.state in home_states:
        snapshot = enter_home_room(
            session,
            snapshot,
            detect_state=lambda items, frame: detect_random_visit_state(
                items, frame, **detector_kwargs
            ),
            state_of=lambda value: value.state,
            home_states=home_states,
            room_state=RandomVisitState.ROOM_MAIN,
            capture=capture,
            failure_state="QJNN_HomeRandomVisitEnterRoomFailed",
            log=log,
            log_prefix="HomeRandomVisit",
        )
        if snapshot is None:
            return RandomVisitResult(False, start_state, successful_visits, "enter_room_failed")

    if snapshot.state is RandomVisitState.ROOM_MAIN:
        visit_entry = _exact(_confident(snapshot.items), "外出拜访")
        if visit_entry is not None:
            log(
                "[HomeRandomVisit] visit entry anchor: "
                f"text={visit_entry.text!r}, box={visit_entry.box}"
            )
            log(
                "[HomeRandomVisit] visit entry icon SafeRect: "
                f"anchor_center_x={visit_entry.box.center[0]}, "
                f"anchor_top={visit_entry.box.y}, safe_rect={snapshot.target}"
            )
        snapshot = _open_visit_menu(session, detector_kwargs=detector_kwargs, capture=capture, log=log)
        if snapshot is None:
            return RandomVisitResult(False, start_state, successful_visits, "open_menu_no_transition")

    if snapshot.state not in {RandomVisitState.VISIT_MENU, RandomVisitState.VISITING_ROOM}:
        _diagnose(
            "QJNN_HomeRandomVisitIllegalStartState",
            snapshot.frame,
            snapshot.items,
            capture=capture,
            log=log,
        )
        return RandomVisitResult(False, start_state, successful_visits, "illegal_start_state")

    while successful_visits < RANDOM_VISIT_TARGET:
        action = (
            "visit_menu_random"
            if snapshot.state is RandomVisitState.VISIT_MENU
            else "visiting_room_random"
        )
        if snapshot.state is RandomVisitState.VISIT_MENU:
            menu_random = _exact(_confident(snapshot.items), "随便逛逛")
            if menu_random is not None:
                log(
                    "[HomeRandomVisit] menu random anchor: "
                    f"text={menu_random.text!r}, box={menu_random.box}"
                )
                log(
                    "[HomeRandomVisit] menu random SafeRect: "
                    f"anchor_center={menu_random.box.center}, "
                    f"safe_rect={snapshot.target}"
                )
        elif snapshot.state is RandomVisitState.VISITING_ROOM:
            visiting_random = _exact(_confident(snapshot.items), "随便逛逛")
            if visiting_random is not None:
                log(
                    "[HomeRandomVisit] visiting random anchor: "
                    f"text={visiting_random.text!r}, box={visiting_random.box}"
                )
                log(
                    "[HomeRandomVisit] visiting random icon SafeRect: "
                    f"anchor_center={visiting_random.box.center}, "
                    f"anchor_top={visiting_random.box.y}, "
                    f"safe_rect={snapshot.target}"
                )
        if _valid_rect(snapshot.target):
            progress.begin_visit()
        if not _click_once(
            session,
            snapshot.target,
            action=action,
            log=log,
            # Preserve the verified Loading + stable-settling timing exactly.
            pre_click_delay=False,
        ):
            diagnostic = (
                "QJNN_HomeRandomVisitMenuRandomTargetInvalid"
                if snapshot.state is RandomVisitState.VISIT_MENU
                else "QJNN_HomeRandomVisitRoomRandomSafeRectTodo"
            )
            _diagnose(
                diagnostic,
                snapshot.frame,
                snapshot.items,
                capture=capture,
                log=log,
            )
            return RandomVisitResult(False, start_state, successful_visits, "random_target_unverified")

        log(
            "[HomeRandomVisit] jump_pending created; "
            f"successful_visits={successful_visits}/{RANDOM_VISIT_TARGET}"
        )

        pending_started_at = session.monotonic()
        pending_deadline = (
            pending_started_at + RANDOM_VISIT_TRANSITION_TIMEOUT_SECONDS
        )
        if (
            loading_trace_enabled
            and snapshot.state is RandomVisitState.VISITING_ROOM
        ):
            trace = _capture_loading_trace(
                session,
                detector_kwargs=detector_kwargs,
                log=log,
                writer=loading_trace_writer,
            )
            log(
                "[HomeRandomVisit] loading trace completed: "
                f"captured={trace.captured_frames}, saved={trace.saved_frames}, "
                f"elapsed={trace.elapsed_seconds:.2f}s; "
                "pending remains unconsumed"
            )

        completed, reason = _wait_for_effective_visit(
            session,
            source_state=snapshot.state,
            loading_classifier=loading_classifier,
            capture=capture,
            log=log,
            detector_kwargs=detector_kwargs,
            pending_started_at=pending_started_at,
            deadline=pending_deadline,
            loading_miss_trace_enabled=loading_miss_trace_enabled,
            loading_miss_trace_writer=loading_miss_trace_writer,
        )
        if completed is None:
            return RandomVisitResult(False, start_state, successful_visits, reason)

        # One pending click token is consumed exactly here and nowhere else.
        entering_visit_mode = snapshot.state is RandomVisitState.VISIT_MENU
        progress.confirm_visit(count_visit=not entering_visit_mode)
        successful_visits = progress.successful_visits
        snapshot = completed
        if entering_visit_mode:
            log("[HomeRandomVisit] visit mode ready; now perform five confirmed room hops")
        else:
            log(f"[HomeRandomVisit] effective visit confirmed: {successful_visits}/{RANDOM_VISIT_TARGET}")

    # The loop condition prevents any random click beyond the target. Returning is
    # legal only after all target visits and from a fresh VISITING_ROOM
    # snapshot; the queue checkpoint prevents recovery from starting another round.
    if (
        successful_visits != RANDOM_VISIT_TARGET
        or snapshot.state is not RandomVisitState.VISITING_ROOM
    ):
        _diagnose(
            "QJNN_HomeRandomVisitReturnPreconditionFailed",
            snapshot.frame,
            snapshot.items,
            capture=capture,
            log=log,
        )
        return RandomVisitResult(
            False,
            start_state,
            successful_visits,
            "return_precondition_failed",
        )
    returned = None
    for attempt in range(RANDOM_VISIT_RETURN_ATTEMPTS):
        log(
            f"[HomeRandomVisit] return_own_room attempt={attempt + 1}/"
            f"{RANDOM_VISIT_RETURN_ATTEMPTS}"
        )
        if not _click_once(session, snapshot.back_target, action="return_own_room", log=log):
            _diagnose(
                "QJNN_HomeRandomVisitBackTargetUnverified",
                snapshot.frame,
                snapshot.items,
                capture=capture,
                log=log,
            )
            return RandomVisitResult(False, start_state, successful_visits, "back_target_unverified")
        log("[HomeRandomVisit] return pending")
        session.sleep(HOME_ROOM_TRANSITION_WAIT_SECONDS)
        observed = _observe_state(
            session,
            allowed={
                RandomVisitState.ROOM_MAIN, RandomVisitState.VISITING_ROOM,
                RandomVisitState.HOME_P1, RandomVisitState.HOME_P2, RandomVisitState.HOME_P3,
            },
            capture=capture,
            failure_state="QJNN_HomeRandomVisitReturnNoTransition",
            log=log,
            detector_kwargs=detector_kwargs,
        )
        if observed is None:
            break
        if observed.state in {
            RandomVisitState.HOME_P1, RandomVisitState.HOME_P2, RandomVisitState.HOME_P3,
        }:
            # The visiting-room back control can exit directly to game HOME.
            # Six visits are already confirmed; verify HOME again without
            # clicking a room exit on the game toolbar.
            if str(getattr(home_page_detector(observed.frame), "page", "Unknown")) not in {"P1", "P2", "P3"}:
                break
            session.sleep(RANDOM_VISIT_POLL_SECONDS)
            fresh_frame = session.capture_frame()
            fresh_items = list(session.recognize_text(fresh_frame))
            fresh_page = str(getattr(home_page_detector(fresh_frame), "page", "Unknown"))
            if fresh_page not in {"P1", "P2", "P3"}:
                break
            final = RandomVisitSnapshot(
                RandomVisitState[f"HOME_{fresh_page}"], fresh_frame, fresh_items
            )
            log(f"[HomeRandomVisit] returned directly to HOME {fresh_page}; confirmed on fresh frame")
            return RandomVisitResult(True, start_state, successful_visits, "completed", final)
        if observed.state is RandomVisitState.ROOM_MAIN:
            returned = observed
            break
        # Only a freshly confirmed visiting room authorizes another return.
        # A HOME/unknown frame cannot cause a second back click.
        snapshot = observed
    else:
        _diagnose(
            "QJNN_HomeRandomVisitReturnNoTransition", snapshot.frame,
            snapshot.items, capture=capture, log=log,
        )
    if returned is None:
        return RandomVisitResult(False, start_state, successful_visits, "return_no_transition")
    log("[HomeRandomVisit] returned to own room")
    return _finish_from_own_room(
        session, returned, successful_visits, start_state,
        exit_to_home=exit_to_home, home_page_detector=home_page_detector,
        detector_kwargs=detector_kwargs, capture=capture, log=log,
    )


def _finish_from_own_room(session, returned, successful_visits, start_state, *,
                          exit_to_home, home_page_detector, detector_kwargs, capture, log):
    if not exit_to_home:
        return RandomVisitResult(
            True,
            start_state,
            successful_visits,
            "completed_in_room",
            returned,
        )
    exited = exit_home_room(
        session,
        returned,
        detect_state=lambda items, frame: detect_random_visit_state(
            items, frame, **detector_kwargs
        ),
        state_of=lambda value: value.state,
        room_state=RandomVisitState.ROOM_MAIN,
        page_detector=home_page_detector,
        capture=capture,
        failure_state="QJNN_HomeRandomVisitHomeNotConfirmed",
        log=log,
        log_prefix="HomeRandomVisit",
    )
    if exited is None:
        return RandomVisitResult(
            False, start_state, successful_visits, "home_not_confirmed"
        )
    return RandomVisitResult(True, start_state, successful_visits, "completed")


class HomeRandomVisitAction(CustomAction):
    def run(self, context, argv) -> bool:
        session = None
        capture = DiagnosticFrameStore(HOME_RANDOM_VISIT_DIAGNOSTIC_DIR)
        try:
            session = _make_runtime_session(context)
            progress = HomeTaskProgress()
            self.allow_back_to_home_recovery = True
            result = run_home_random_visit(session, capture=capture.capture, progress=progress)
            self.allow_back_to_home_recovery = not progress.visit_pending
            print(
                f"[HomeRandomVisit] result={result.reason}, "
                f"start_state={result.start_state}, "
                f"successful_visits={result.successful_visits}/{RANDOM_VISIT_TARGET}"
            )
            return result.success
        except Exception as exc:
            self.allow_back_to_home_recovery = False
            print(f"[HomeRandomVisit] SafeStop exception: {exc}")
            if session is not None:
                try:
                    capture.capture(
                        "QJNN_HomeRandomVisitException",
                        session.capture_frame(),
                    )
                except Exception as capture_exc:
                    print(f"[HomeRandomVisit] diagnostic unavailable: {capture_exc}")
            return False
