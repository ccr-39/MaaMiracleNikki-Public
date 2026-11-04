from __future__ import annotations

from app_paths import DATA_ROOT

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol, Sequence

from qjnn_alliance_commission import OcrItem, Rect, find_text_box
from qjnn_common_popup import (
    GlobalTransientResult,
    guard_runtime_session,
    handle_transient_if_present,
)
from qjnn_home import detect_home_page
from qjnn_run_state import RunRecord, run_record
from qjnn_home_navigation import navigate_home_page
from qjnn_home_random_visit import home_random_visit_loading_classifier
from qjnn_reward import detect_reward_clothes_card, rect_contains, rects_intersect

try:
    from maa.custom_action import CustomAction
except ImportError:  # MaaFramework is optional for offline tests.
    CustomAction = object


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MYSTERY_HOUSE_REFERENCE_SIZE = (720, 1280)
MYSTERY_HOUSE_CHECKPOINT = (
    DATA_ROOT / "config" / "mystery_house_daily_state.json"
)
MYSTERY_HOUSE_DEBUG_DIR = (
    DATA_ROOT / "images" / "screenshots" / "mystery_house" / "debug"
)

# All geometry below is expressed in Maa's normalized 720 x 1280 coordinates.
HOME_ENTRY_ROI = (440, 330, 145, 140)
HOME_ENTRY_SAFE_RECT = (452, 345, 112, 100)
TITLE_ROI = (180, 42, 360, 75)
LEFT_STATUS_ROI = (35, 1170, 205, 85)
MYSTERY_REMAINING_PURCHASE_ROI = (300, 1035, 360, 55)
MYSTERY_REMAINING_SUFFIX_ROI = (500, 1035, 160, 55)
FANTASY_BUY_ONE_SAFE_RECT = (124, 1088, 205, 92)
MYSTERY_BUY_ONE_SAFE_RECT = (58, 1100, 168, 82)
PAVILION_SWITCH_SAFE_RECT = (676, 500, 42, 90)
FANTASY_TICKET_CONFIRM_TITLE_ROI = (110, 450, 500, 90)
FANTASY_TICKET_CANCEL_BUTTON_RECT = (160, 600, 160, 90)
FANTASY_TICKET_CANCEL_SAFE_RECT = (180, 615, 120, 50)
FANTASY_TICKET_CONFIRM_BUTTON_RECT = (400, 600, 160, 90)
REWARD_SAFE_RECT = (250, 420, 220, 200)
REWARD_TITLE_ROI = (160, 220, 400, 130)
REWARD_SHARE_FORBIDDEN_RECT = (520, 960, 190, 100)
PAGE_BACK_SAFE_RECT = (12, 8, 84, 88)

OCR_THRESHOLD = 0.70
PAGE_TIMEOUT_SECONDS = 15.0
DRAW_RESULT_TIMEOUT_SECONDS = 20.0
RETURN_HOME_TIMEOUT_SECONDS = 15.0
MAX_RETURN_HOME_CLICKS = 3
POLL_INTERVAL_SECONDS = 0.5
POST_CLICK_WAIT_SECONDS = 0.7
PAID_PAGE_SETTLE_SECONDS = 1.5
MAX_REWARD_DISMISS_CLICKS = 4
MYSTERY_INITIAL_REMAINING_PURCHASES = 1000000
MYSTERY_TARGET_DRAWS = 4


class FantasyState(str, Enum):
    FREE_1_OF_1 = "FREE_1_OF_1"
    DONE = "DONE"


class MysteryState(str, Enum):
    FREE_2_OF_2 = "FREE_2_OF_2"
    FREE_1_OF_2 = "FREE_1_OF_2"
    PAID_3000 = "PAID_3000"
    DONE = "MYSTERY_PAVILION_DONE"


class EntryKind(str, Enum):
    HOME = "HOME"
    FANTASY = "FANTASY"
    MYSTERY = "MYSTERY"
    FANTASY_REWARD = "FANTASY_REWARD"
    MYSTERY_REWARD = "MYSTERY_REWARD"


class CheckpointStatus(str, Enum):
    VALID = "VALID"
    MISSING = "MISSING"
    STALE = "STALE"
    INVALID = "INVALID"


@dataclass(frozen=True)
class PavilionDetection:
    title: Rect
    buy_one: Rect
    status_box: Rect
    state: FantasyState | MysteryState
    remaining_count: int | None = None
    remaining_box: Rect | None = None


@dataclass(frozen=True)
class MysteryEntryState:
    kind: EntryKind
    value: object


@dataclass(frozen=True)
class PaidCheckpoint:
    day: str
    paid_done: int
    paid_in_flight: bool = False
    remaining_before: int | None = None


@dataclass(frozen=True)
class MysteryPurchaseProgress:
    remaining_count: int
    draw_count: int
    remaining_target_draws: int
    paid_done: int
    completed: bool


@dataclass(frozen=True)
class CheckpointLoad:
    status: CheckpointStatus
    checkpoint: PaidCheckpoint | None
    reason: str


@dataclass(frozen=True)
class MysteryHouseResult:
    success: bool
    paid_done: int | None
    reason: str


class MysteryHouseSession(Protocol):
    def capture_frame(self): ...

    def recognize_text(self, frame) -> list[OcrItem]: ...

    def click(self, x: int, y: int) -> None: ...

    def sleep(self, seconds: float) -> None: ...

    def monotonic(self) -> float: ...


def normalize_mystery_text(value: str) -> str:
    table = str.maketrans("０１２３４５６７８９／", "0123456789/")
    return re.sub(r"\s+", "", str(value).translate(table)).replace("：", ":")


def _scaled_rect(rect: tuple[int, int, int, int], frame) -> Rect:
    height, width = frame.shape[:2]
    ref_width, ref_height = MYSTERY_HOUSE_REFERENCE_SIZE
    x, y, w, h = rect
    left = round(x * width / ref_width)
    top = round(y * height / ref_height)
    right = round((x + w) * width / ref_width)
    bottom = round((y + h) * height / ref_height)
    return Rect(left, top, max(1, right - left), max(1, bottom - top))


def _box_tuple(box: Rect) -> tuple[int, int, int, int]:
    return box.x, box.y, box.width, box.height


def _box_in_rect(box: Rect, roi: Rect) -> bool:
    x, y = box.center
    return roi.x <= x <= roi.right and roi.y <= y <= roi.bottom


def _confident(items: Sequence[OcrItem]) -> list[OcrItem]:
    return [item for item in items if float(item.score) >= OCR_THRESHOLD]


def _find_text(items: Sequence[OcrItem], target: str, roi: Rect) -> Rect | None:
    return find_text_box(_confident(items), target, roi=roi)


def _status_match(
    items: Sequence[OcrItem], frame, pattern: re.Pattern[str]
) -> Rect | None:
    roi = _scaled_rect(LEFT_STATUS_ROI, frame)
    candidates = [item for item in _confident(items) if _box_in_rect(item.box, roi)]
    for item in candidates:
        if pattern.fullmatch(normalize_mystery_text(item.text)):
            return item.box
    combined = "".join(
        normalize_mystery_text(item.text)
        for item in sorted(candidates, key=lambda item: item.box.x)
    )
    if pattern.fullmatch(combined) and candidates:
        box = candidates[0].box
        for item in candidates[1:]:
            box = box.union(item.box)
        return box
    return None


def find_mystery_house_home_entry(
    items: Sequence[OcrItem], frame
) -> Rect | None:
    roi = _scaled_rect(HOME_ENTRY_ROI, frame)
    return _find_text(items, "谜之屋", roi)


def _find_title(items: Sequence[OcrItem], frame, title: str) -> Rect | None:
    return _find_text(items, title, _scaled_rect(TITLE_ROI, frame))


def find_fantasy_ticket_confirm_title(
    items: Sequence[OcrItem], frame
) -> Rect | None:
    return _find_text(
        items,
        "是否使用幻阁券去获取稀有服装？",
        _scaled_rect(FANTASY_TICKET_CONFIRM_TITLE_ROI, frame),
    )


def detect_fantasy_ticket_confirm(
    items: Sequence[OcrItem], frame
) -> Rect | None:
    """Return the verified left Cancel OCR box, never the Confirm button."""
    if find_fantasy_ticket_confirm_title(items, frame) is None:
        return None
    button = _scaled_rect(FANTASY_TICKET_CANCEL_BUTTON_RECT, frame)
    cancel = _find_text(items, "取消", button)
    if cancel is None or not rect_contains(_box_tuple(button), _box_tuple(cancel)):
        return None
    return cancel


def _find_safe_buy_one(
    items: Sequence[OcrItem], frame, safe_reference: tuple[int, int, int, int]
) -> Rect | None:
    safe = _scaled_rect(safe_reference, frame)
    box = _find_text(items, "买一个", safe)
    if box is None or not rect_contains(_box_tuple(safe), _box_tuple(box)):
        return None
    return box


def detect_fantasy_free_status(
    items: Sequence[OcrItem], frame
) -> tuple[FantasyState, Rect] | None:
    free = _status_match(items, frame, re.compile(r"免费领取1/1"))
    if free is not None:
        return FantasyState.FREE_1_OF_1, free
    spent = _status_match(items, frame, re.compile(r"花费60"))
    if spent is not None:
        return FantasyState.DONE, spent
    return None


def detect_mystery_free_status(
    items: Sequence[OcrItem], frame
) -> tuple[MysteryState, Rect] | None:
    free_two = _status_match(items, frame, re.compile(r"免费领取2/2"))
    if free_two is not None:
        return MysteryState.FREE_2_OF_2, free_two
    free_one = _status_match(items, frame, re.compile(r"免费领取1/2"))
    if free_one is not None:
        return MysteryState.FREE_1_OF_2, free_one
    return None


def detect_mystery_paid_status(
    items: Sequence[OcrItem], frame
) -> tuple[MysteryState, Rect] | None:
    # Exact full match deliberately rejects nearby 27000/270000 purchase prices.
    paid = _status_match(items, frame, re.compile(r"花费3000"))
    return (MysteryState.PAID_3000, paid) if paid is not None else None


def detect_mystery_remaining_purchase_count(
    items: Sequence[OcrItem], frame
) -> tuple[int, Rect] | None:
    """Parse only the labelled remaining-purchase line in its narrow ROI."""
    roi = _scaled_rect(MYSTERY_REMAINING_PURCHASE_ROI, frame)
    candidates = [item for item in _confident(items) if _box_in_rect(item.box, roi)]
    pattern = re.compile(r"今日剩余购买次数:([0-9]{1,7})")
    for item in candidates:
        match = pattern.fullmatch(normalize_mystery_text(item.text))
        if match is not None:
            value = int(match.group(1))
            if 0 <= value <= MYSTERY_INITIAL_REMAINING_PURCHASES:
                return value, item.box

    ordered = sorted(candidates, key=lambda item: item.box.x)
    combined = "".join(normalize_mystery_text(item.text) for item in ordered)
    match = pattern.fullmatch(combined)
    if match is None or not ordered:
        # A clothing-progress toast can cover the left half of this label.
        # Accept only its labelled right-hand suffix on a confirmed paid
        # Mystery Pavilion page; bare digits and unrelated counters never pass.
        if (_find_title(items, frame, "谜之屋·谜之阁") is None
                or _find_safe_buy_one(items, frame, MYSTERY_BUY_ONE_SAFE_RECT) is None
                or detect_mystery_paid_status(items, frame) is None
                or detect_reward_surface(frame, items)):
            return None
        suffix_roi = _scaled_rect(MYSTERY_REMAINING_SUFFIX_ROI, frame)
        if len(candidates) != 1:
            return None
        item = candidates[0]
        suffix = re.fullmatch(r"数:([0-9]{6,7})", normalize_mystery_text(item.text))
        if (suffix is None or item.score < .90
                or not rect_contains(_box_tuple(suffix_roi), _box_tuple(item.box))):
            return None
        value = int(suffix.group(1))
        return (value, item.box) if 0 <= value <= MYSTERY_INITIAL_REMAINING_PURCHASES else None
    value = int(match.group(1))
    if not 0 <= value <= MYSTERY_INITIAL_REMAINING_PURCHASES:
        return None
    box = ordered[0].box
    for item in ordered[1:]:
        box = box.union(item.box)
    return value, box


def derive_mystery_purchase_progress(
    remaining_count: int,
) -> MysteryPurchaseProgress | None:
    if not 0 <= remaining_count <= MYSTERY_INITIAL_REMAINING_PURCHASES:
        return None
    draw_count = MYSTERY_INITIAL_REMAINING_PURCHASES - remaining_count
    completed = draw_count >= MYSTERY_TARGET_DRAWS
    return MysteryPurchaseProgress(
        remaining_count=remaining_count,
        draw_count=draw_count,
        remaining_target_draws=max(0, MYSTERY_TARGET_DRAWS - draw_count),
        paid_done=min(2, max(0, draw_count - 2)),
        completed=completed,
    )


def detect_fantasy_pavilion_state(
    items: Sequence[OcrItem], frame
) -> PavilionDetection | None:
    # Reward overlays can leave the title, buy button and old cost visible behind
    # them.  The stable card-structure gate must win before active-page OCR.
    if detect_reward_surface(frame, items):
        return None
    title = _find_title(items, frame, "谜之屋·幻之阁")
    buy = _find_safe_buy_one(items, frame, FANTASY_BUY_ONE_SAFE_RECT)
    status = detect_fantasy_free_status(items, frame)
    if title is None or buy is None or status is None:
        return None
    state, status_box = status
    return PavilionDetection(title, buy, status_box, state)


def detect_mystery_pavilion_state(
    items: Sequence[OcrItem], frame
) -> PavilionDetection | None:
    if detect_reward_surface(frame, items):
        return None
    title = _find_title(items, frame, "谜之屋·谜之阁")
    buy = _find_safe_buy_one(items, frame, MYSTERY_BUY_ONE_SAFE_RECT)
    remaining = detect_mystery_remaining_purchase_count(items, frame)
    if title is None or buy is None:
        return None
    if remaining is not None:
        remaining_count, remaining_box = remaining
        progress = derive_mystery_purchase_progress(remaining_count)
        if progress is not None and progress.completed:
            return PavilionDetection(
                title,
                buy,
                remaining_box,
                MysteryState.DONE,
                remaining_count,
                remaining_box,
            )
    status = detect_mystery_free_status(items, frame)
    if status is None:
        status = detect_mystery_paid_status(items, frame)
    if status is None:
        return None
    state, status_box = status
    return PavilionDetection(
        title,
        buy,
        status_box,
        state,
        remaining[0] if remaining is not None else None,
        remaining[1] if remaining is not None else None,
    )


def mystery_reward_structure_score(frame) -> float:
    """Use stable pale-card geometry, never reward names or rarity text."""
    detection = detect_reward_clothes_card(frame)
    probes = detection.probe_scores
    if not probes:
        return 0.0
    return float(
        (probes.get("upper_card", 0.0) + probes.get("right_frame", 0.0)) / 2
    )


def detect_reward_surface(frame, items: Sequence[OcrItem] = ()) -> bool:
    # Ticket cards can have a translucent body that fails the pale-card probe.
    # Their heading still identifies a reward; names/icons never gate closing.
    title_roi = _scaled_rect(REWARD_TITLE_ROI, frame)
    heading = any(
        _find_text(items, title, title_roi) is not None
        for title in ("恭喜获得", "获得奖励")
    )
    return heading or mystery_reward_structure_score(frame) >= 0.68


def detect_mystery_house_entry_state(
    items: Sequence[OcrItem],
    frame,
    *,
    detect_page: Callable[[object], object] = detect_home_page,
) -> MysteryEntryState | None:
    fantasy = detect_fantasy_pavilion_state(items, frame)
    if fantasy is not None:
        return MysteryEntryState(EntryKind.FANTASY, fantasy)
    mystery = detect_mystery_pavilion_state(items, frame)
    if mystery is not None:
        return MysteryEntryState(EntryKind.MYSTERY, mystery)
    if detect_reward_surface(frame, items):
        if _find_title(items, frame, "谜之屋·幻之阁") is not None:
            return MysteryEntryState(EntryKind.FANTASY_REWARD, None)
        if _find_title(items, frame, "谜之屋·谜之阁") is not None:
            return MysteryEntryState(EntryKind.MYSTERY_REWARD, None)
    page = str(getattr(detect_page(frame), "page", "Unknown"))
    if page in {"P1", "P2", "P3"}:
        return MysteryEntryState(EntryKind.HOME, page)
    return None


def _today_text(today: date | None = None) -> str:
    return (today or date.today()).isoformat()


def load_mystery_paid_checkpoint(
    path: Path = MYSTERY_HOUSE_CHECKPOINT,
    *,
    today: date | None = None,
) -> CheckpointLoad:
    if isinstance(path, RunRecord):
        payload = path.read()
        if payload is None:
            return CheckpointLoad(CheckpointStatus.MISSING, None, "run_record_empty")
    elif not path.is_file():
        return CheckpointLoad(CheckpointStatus.MISSING, None, "file_missing")
    try:
        if not isinstance(path, RunRecord):
            payload = json.loads(path.read_text(encoding="utf-8"))
        day = payload["date"]
        paid_done = payload["mystery_pavilion_paid_done"]
        in_flight = payload.get("paid_in_flight", False)
        remaining_before = payload.get("remaining_before")
        if (
            not isinstance(day, str)
            or not isinstance(paid_done, int)
            or isinstance(paid_done, bool)
            or paid_done not in {0, 1, 2}
            or not isinstance(in_flight, bool)
            or (
                remaining_before is not None
                and (
                    not isinstance(remaining_before, int)
                    or isinstance(remaining_before, bool)
                    or not 0
                    <= remaining_before
                    <= MYSTERY_INITIAL_REMAINING_PURCHASES
                )
            )
        ):
            raise ValueError("invalid checkpoint schema")
        checkpoint = PaidCheckpoint(day, paid_done, in_flight, remaining_before)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError) as exc:
        return CheckpointLoad(CheckpointStatus.INVALID, None, str(exc))
    if checkpoint.day != _today_text(today):
        return CheckpointLoad(CheckpointStatus.STALE, checkpoint, "date_mismatch")
    return CheckpointLoad(CheckpointStatus.VALID, checkpoint, "ok")


def save_mystery_paid_checkpoint(
    checkpoint: PaidCheckpoint,
    path: Path = MYSTERY_HOUSE_CHECKPOINT,
) -> None:
    if checkpoint.paid_done not in {0, 1, 2}:
        raise ValueError("paid_done must be 0, 1 or 2")
    payload = {
        "date": checkpoint.day,
        "mystery_pavilion_paid_done": checkpoint.paid_done,
        "paid_in_flight": checkpoint.paid_in_flight,
        "remaining_before": checkpoint.remaining_before,
    }
    if isinstance(path, RunRecord):
        path.write(payload)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _checkpoint_from_free_ui(
    path: Path, *, today: date | None, log: Callable[[str], None]
) -> PaidCheckpoint | None:
    loaded = load_mystery_paid_checkpoint(path, today=today)
    if loaded.status in {CheckpointStatus.MISSING, CheckpointStatus.STALE}:
        checkpoint = PaidCheckpoint(_today_text(today), 0, False)
        save_mystery_paid_checkpoint(checkpoint, path)
        log("[MysteryHouse] paid checkpoint initialized from confirmed free UI: 0/2")
        return checkpoint
    if loaded.status is CheckpointStatus.INVALID:
        log(f"[MysteryHouse] checkpoint invalid: {loaded.reason}; SafeStop")
        return None
    checkpoint = loaded.checkpoint
    assert checkpoint is not None
    if checkpoint.paid_done != 0 or checkpoint.paid_in_flight:
        log("[MysteryHouse] free UI conflicts with today's paid checkpoint; SafeStop")
        return None
    return checkpoint


def _log_mystery_progress(
    remaining_count: int | None, *, log: Callable[[str], None]
) -> MysteryPurchaseProgress | None:
    if remaining_count is None:
        log("[MysteryHouse] remaining purchase count: UNKNOWN")
        return None
    progress = derive_mystery_purchase_progress(remaining_count)
    if progress is None:
        log(f"[MysteryHouse] remaining purchase count invalid: {remaining_count}")
        return None
    log(f"[MysteryHouse] remaining purchase count: {remaining_count}")
    if progress.completed:
        log(f"[MysteryHouse] derived draw count: >={progress.draw_count}")
        log("[MysteryHouse] mystery pavilion already completed today")
    else:
        log(f"[MysteryHouse] derived draw count: {progress.draw_count}")
        log(
            "[MysteryHouse] remaining target draws: "
            f"{progress.remaining_target_draws}"
        )
    return progress


def _checkpoint_from_reliable_ui(
    remaining_count: int,
    path: Path,
    *,
    today: date | None,
    log: Callable[[str], None],
) -> PaidCheckpoint | None:
    """Reconcile auxiliary state from authoritative game UI progress."""
    progress = derive_mystery_purchase_progress(remaining_count)
    if progress is None:
        return None
    loaded = load_mystery_paid_checkpoint(path, today=today)
    if (
        loaded.status is CheckpointStatus.VALID
        and loaded.checkpoint is not None
        and loaded.checkpoint.paid_in_flight
    ):
        old = loaded.checkpoint
        if old.remaining_before is not None:
            if remaining_count == old.remaining_before:
                log(
                    "[MysteryHouse] paid_in_flight count has not decreased; "
                    "SafeStop"
                )
                return None
            if remaining_count != old.remaining_before - 1:
                log(
                    "[MysteryHouse] paid_in_flight count changed unexpectedly: "
                    f"before={old.remaining_before}, now={remaining_count}; SafeStop"
                )
                return None
        elif progress.paid_done <= old.paid_done:
            log(
                "[MysteryHouse] legacy paid_in_flight is not proven by UI; SafeStop"
            )
            return None
        log("[MysteryHouse] UI count proves the in-flight paid draw completed")

    checkpoint = PaidCheckpoint(
        _today_text(today), progress.paid_done, False, None
    )
    save_mystery_paid_checkpoint(checkpoint, path)
    log(
        "[MysteryHouse] checkpoint reconciled from UI: "
        f"paid_done={checkpoint.paid_done}/2"
    )
    return checkpoint


def save_mystery_debug_frame(
    stage: str,
    frame,
    *,
    output_dir: Path = MYSTERY_HOUSE_DEBUG_DIR,
) -> Path | None:
    try:
        import cv2

        if frame is None or getattr(frame, "size", 0) == 0:
            return None
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe_stage = re.sub(r"[^A-Za-z0-9_-]+", "_", stage).strip("_") or "unknown"
        destination = output_dir / f"mystery_house_{safe_stage}_{stamp}.png"
        suffix = 1
        while destination.exists():
            destination = output_dir / (
                f"mystery_house_{safe_stage}_{stamp}_{suffix:02d}.png"
            )
            suffix += 1
        encoded, data = cv2.imencode(".png", frame)
        if not encoded:
            return None
        data.tofile(str(destination))
        return destination
    except Exception as exc:
        print(f"[MysteryHouse] diagnostic capture failed: {exc}")
        return None


def log_mystery_failure(
    stage: str,
    frame,
    items: Sequence[OcrItem],
    *,
    retry: int,
    paid_risk: bool,
    capture: Callable[[str, object], Path | None] = save_mystery_debug_frame,
    log: Callable[[str], None] = print,
) -> None:
    path = capture(stage, frame)
    log(
        f"[MysteryHouse] SafeStop stage={stage}, retry={retry}, "
        f"paid_risk={paid_risk}, screenshot={path}"
    )
    for item in items:
        log(
            f"[MysteryHouse] OCR text={item.text!r}, score={float(item.score):.6f}, "
            f"box=({item.box.x},{item.box.y},{item.box.width},{item.box.height})"
        )


def _snapshot(session: MysteryHouseSession):
    frame = session.capture_frame()
    return frame, list(session.recognize_text(frame))


def _poll(
    session: MysteryHouseSession,
    selector: Callable[[Sequence[OcrItem], object], object | None],
    *,
    timeout: float,
    stage: str,
    paid_risk: bool = False,
    log: Callable[[str], None] = print,
):
    deadline = session.monotonic() + timeout
    retries = 0
    last_frame = None
    last_items: list[OcrItem] = []
    while session.monotonic() < deadline:
        last_frame, last_items = _snapshot(session)
        value = selector(last_items, last_frame)
        if value is not None:
            return last_frame, last_items, value
        transient, fresh_frame, fresh_items = handle_transient_if_present(
            session, last_frame, last_items, log=log
        )
        if transient is GlobalTransientResult.ERROR:
            break
        if transient is GlobalTransientResult.HANDLED_POPUP:
            last_frame, last_items = fresh_frame, list(fresh_items)
            value = selector(last_items, last_frame)
            if value is not None:
                return last_frame, last_items, value
        retries += 1
        remaining = deadline - session.monotonic()
        if remaining > 0:
            session.sleep(min(POLL_INTERVAL_SECONDS, remaining))
    if last_frame is not None:
        log_mystery_failure(
            stage,
            last_frame,
            last_items,
            retry=retries,
            paid_risk=paid_risk,
            log=log,
        )
    return None


def _click_fixed_safe_rect(
    session: MysteryHouseSession,
    frame,
    reference_rect: tuple[int, int, int, int],
    *,
    label: str,
    log: Callable[[str], None],
) -> Rect:
    safe = _scaled_rect(reference_rect, frame)
    log(f"[MysteryHouse] {label} SafeRect={safe}, point={safe.center}")
    if session.click(*safe.center) is False:
        raise RuntimeError(f"[MysteryHouse] {label} input was rejected")
    return safe


def click_left_buy_one_safe(
    session: MysteryHouseSession,
    *,
    pavilion: str,
    allowed_states: set[FantasyState | MysteryState],
    required_remaining_count: int | None = None,
    before_click: Callable[[], None] | None = None,
    log: Callable[[str], None] = print,
) -> tuple[object, PavilionDetection] | None:
    frame, items = _snapshot(session)
    detector = (
        detect_fantasy_pavilion_state
        if pavilion == "fantasy"
        else detect_mystery_pavilion_state
    )
    state = detector(items, frame)
    if (
        state is None
        or state.state not in allowed_states
        or (
            required_remaining_count is not None
            and state.remaining_count != required_remaining_count
        )
    ):
        log_mystery_failure(
            f"{pavilion}_buy_gate",
            frame,
            items,
            retry=0,
            paid_risk=MysteryState.PAID_3000 in allowed_states,
            log=log,
        )
        return None
    safe_reference = (
        FANTASY_BUY_ONE_SAFE_RECT
        if pavilion == "fantasy"
        else MYSTERY_BUY_ONE_SAFE_RECT
    )
    safe = _scaled_rect(safe_reference, frame)
    if not rect_contains(_box_tuple(safe), _box_tuple(state.buy_one)):
        log("[MysteryHouse] buy-one OCR box escaped SafeRect; zero clicks")
        return None
    if before_click is not None:
        before_click()
    _click_fixed_safe_rect(
        session, frame, safe_reference, label=f"{pavilion} buy one", log=log
    )
    return frame, state


def _wait_draw_surface(
    session: MysteryHouseSession,
    pavilion: str,
    *,
    previous_state: FantasyState | MysteryState,
    paid_risk: bool,
    log: Callable[[str], None],
):
    def selector(items: Sequence[OcrItem], frame):
        # The cost/count changes before the reward animation opens.  An active
        # page with a new price is not evidence that the reward was dismissed.
        # Observe the reward before allowing any later purchase or pavilion switch.
        if detect_reward_surface(frame, items):
            return ("reward", None)
        return None

    session.sleep(POST_CLICK_WAIT_SECONDS)
    return _poll(
        session,
        selector,
        timeout=DRAW_RESULT_TIMEOUT_SECONDS,
        stage=f"{pavilion}_draw_wait",
        paid_risk=paid_risk,
        log=log,
    )


def dismiss_mystery_reward_until_return(
    session: MysteryHouseSession,
    pavilion: str,
    *,
    initial_transition=None,
    paid_risk: bool = False,
    log: Callable[[str], None] = print,
) -> tuple[object, PavilionDetection, int] | None:
    detector = (
        detect_fantasy_pavilion_state
        if pavilion == "fantasy"
        else detect_mystery_pavilion_state
    )
    transition = initial_transition
    clicks = 0
    while clicks <= MAX_REWARD_DISMISS_CLICKS:
        if transition is None:
            transition = _poll(
                session,
                lambda items, frame: (
                    ("active", active)
                    if (active := detector(items, frame)) is not None
                    else (("reward", None) if detect_reward_surface(frame, items) else None)
                ),
                timeout=DRAW_RESULT_TIMEOUT_SECONDS,
                stage=f"{pavilion}_reward_wait",
                paid_risk=paid_risk,
                log=log,
            )
        if transition is None:
            return None
        frame, items, (kind, active) = transition
        if kind == "active":
            return frame, active, clicks
        if clicks >= MAX_REWARD_DISMISS_CLICKS:
            log_mystery_failure(
                f"{pavilion}_reward_dismiss_limit",
                frame,
                items,
                retry=clicks,
                paid_risk=paid_risk,
                log=log,
            )
            return None
        safe = _scaled_rect(REWARD_SAFE_RECT, frame)
        forbidden = _scaled_rect(REWARD_SHARE_FORBIDDEN_RECT, frame)
        if rects_intersect(_box_tuple(safe), _box_tuple(forbidden)):
            log("[MysteryHouse] reward SafeRect intersects Share forbidden rect")
            return None
        clicks += 1
        log(
            f"[MysteryHouse] dismiss reward page: "
            f"{clicks}/{MAX_REWARD_DISMISS_CLICKS}"
        )
        _click_fixed_safe_rect(
            session, frame, REWARD_SAFE_RECT, label="reward card", log=log
        )
        session.sleep(POST_CLICK_WAIT_SECONDS)
        transition = None
    return None


def _resolve_fantasy_ticket_confirm(
    session: MysteryHouseSession,
    *,
    log: Callable[[str], None],
) -> tuple[object, list[OcrItem], PavilionDetection] | None:
    frame, items = _snapshot(session)
    if detect_reward_surface(frame, items):
        # A second reward can appear after an intermediate active pavilion
        # frame. Finish that layer before allowing the pavilion switch.
        log("[MysteryHouse] delayed fantasy reward before switch; dismissing")
        returned = dismiss_mystery_reward_until_return(session, "fantasy", log=log)
        if returned is None:
            return None
        frame, items = _snapshot(session)
    title = find_fantasy_ticket_confirm_title(items, frame)
    if title is None:
        active = detect_fantasy_pavilion_state(items, frame)
        return (frame, items, active) if active is not None else None

    cancel = detect_fantasy_ticket_confirm(items, frame)
    safe = _scaled_rect(FANTASY_TICKET_CANCEL_SAFE_RECT, frame)
    cancel_button = _scaled_rect(FANTASY_TICKET_CANCEL_BUTTON_RECT, frame)
    confirm_button = _scaled_rect(FANTASY_TICKET_CONFIRM_BUTTON_RECT, frame)
    if (
        cancel is None
        or not rect_contains(_box_tuple(cancel_button), _box_tuple(cancel))
        or not rect_contains(_box_tuple(cancel_button), _box_tuple(safe))
        or rects_intersect(_box_tuple(safe), _box_tuple(confirm_button))
    ):
        log_mystery_failure(
            "fantasy_ticket_confirm_cancel_unsafe",
            frame,
            items,
            retry=0,
            paid_risk=False,
            log=log,
        )
        return None

    _click_fixed_safe_rect(
        session,
        frame,
        FANTASY_TICKET_CANCEL_SAFE_RECT,
        label="fantasy ticket confirm cancel",
        log=log,
    )
    session.sleep(POST_CLICK_WAIT_SECONDS)
    returned = _poll(
        session,
        lambda current_items, current_frame: (
            active
            if find_fantasy_ticket_confirm_title(current_items, current_frame) is None
            and (active := detect_fantasy_pavilion_state(current_items, current_frame))
            is not None
            else None
        ),
        timeout=PAGE_TIMEOUT_SECONDS,
        stage="fantasy_ticket_confirm_cancel_return",
        log=log,
    )
    if returned is None:
        return None
    returned_frame, returned_items, active = returned
    log("[MysteryHouse] fantasy ticket confirmation cancelled")
    return returned_frame, list(returned_items), active


def switch_fantasy_to_mystery(
    session: MysteryHouseSession,
    *,
    log: Callable[[str], None] = print,
) -> tuple[object, PavilionDetection] | None:
    ready = _resolve_fantasy_ticket_confirm(session, log=log)
    if ready is None:
        frame, items = _snapshot(session)
        log_mystery_failure(
            "switch_gate", frame, items, retry=0, paid_risk=False, log=log
        )
        return None
    frame, items, _ = ready
    _click_fixed_safe_rect(
        session,
        frame,
        PAVILION_SWITCH_SAFE_RECT,
        label="fixed right pavilion switch",
        log=log,
    )
    found = _poll(
        session,
        detect_mystery_pavilion_state,
        timeout=PAGE_TIMEOUT_SECONDS,
        stage="switch_to_mystery",
        log=log,
    )
    if found is None:
        return None
    mystery_frame, _, mystery = found
    log("[MysteryHouse] switched to Mystery Pavilion")
    return mystery_frame, mystery


def _confirm_mystery_count_decreased(
    session: MysteryHouseSession,
    state: PavilionDetection,
    remaining_before: int,
    *,
    paid_risk: bool,
    log: Callable[[str], None],
) -> PavilionDetection | None:
    expected = remaining_before - 1
    if state.remaining_count == expected:
        return state
    confirmed = _poll(
        session,
        lambda items, frame: (
            candidate
            if (candidate := detect_mystery_pavilion_state(items, frame)) is not None
            and candidate.remaining_count == expected
            else None
        ),
        timeout=PAGE_TIMEOUT_SECONDS,
        stage="remaining_count_not_decreased",
        paid_risk=paid_risk,
        log=log,
    )
    if confirmed is None:
        return None
    _, _, refreshed = confirmed
    log(
        "[MysteryHouse] remaining purchase count verified: "
        f"{remaining_before} -> {refreshed.remaining_count}"
    )
    return refreshed


def _perform_free_draw(
    session: MysteryHouseSession,
    pavilion: str,
    current: PavilionDetection,
    *,
    expected_after: FantasyState | MysteryState,
    log: Callable[[str], None],
) -> PavilionDetection | None:
    clicked = click_left_buy_one_safe(
        session,
        pavilion=pavilion,
        allowed_states={current.state},
        required_remaining_count=(
            current.remaining_count if pavilion == "mystery" else None
        ),
        log=log,
    )
    if clicked is None:
        return None
    transition = _wait_draw_surface(
        session,
        pavilion,
        previous_state=current.state,
        paid_risk=False,
        log=log,
    )
    if transition is None:
        return None
    returned = dismiss_mystery_reward_until_return(
        session, pavilion, initial_transition=transition, log=log
    )
    if returned is None:
        return None
    _, state, _ = returned
    if pavilion == "mystery" and current.remaining_count is not None:
        state = _confirm_mystery_count_decreased(
            session,
            state,
            current.remaining_count,
            paid_risk=False,
            log=log,
        )
        if state is None:
            return None
    if state.state != expected_after:
        log(f"[MysteryHouse] unexpected state after free draw: {state.state}")
        return None
    return state


def _wait_paid_buy_ready(session, current, *, log):
    """Wait out reward dismissal before arming the next billable input.

    The pavilion's price/count can be visible under a fading reward. Require
    the owned page and unchanged button geometry to stay settled, with no
    loading/reward overlay. This wait never submits or retries a purchase.
    """
    stable_since = None
    previous_box = None

    def ready(items, frame):
        nonlocal stable_since, previous_box
        state = detect_mystery_pavilion_state(items, frame)
        if (state is None or state.state is not MysteryState.PAID_3000
                or state.remaining_count != current.remaining_count
                or home_random_visit_loading_classifier(items, frame)):
            stable_since, previous_box = None, None
            return None
        box = state.buy_one
        coordinates = _box_tuple(box)
        if previous_box is None or any(abs(a - b) > 3 for a, b in zip(previous_box, coordinates)):
            stable_since = session.monotonic()
        previous_box = coordinates
        if session.monotonic() - stable_since >= PAID_PAGE_SETTLE_SECONDS:
            return state
        return None

    log("[MysteryHouse] waiting for settled paid button/count before input")
    return _poll(session, ready, timeout=PAGE_TIMEOUT_SECONDS,
                 stage="paid_button_not_settled", log=log)


def _perform_paid_draw(
    session: MysteryHouseSession,
    checkpoint: PaidCheckpoint,
    current: PavilionDetection,
    checkpoint_path: Path,
    *,
    today: date | None,
    log: Callable[[str], None],
    max_coin_draws: int = 2,
) -> tuple[PaidCheckpoint, PavilionDetection] | None:
    if (type(max_coin_draws) is not int or not 1 <= max_coin_draws <= 2
            or checkpoint.paid_in_flight or checkpoint.paid_done >= max_coin_draws):
        log("[MysteryHouse] paid budget prevents another purchase; zero clicks")
        return None
    # The price/count may become readable before the free-reward transition
    # has finished accepting input. Settle before the existing fresh-page,
    # exact-price/count gate; never retry an ambiguous paid submission.
    if _wait_paid_buy_ready(session, current, log=log) is None:
        return None
    in_flight = PaidCheckpoint(
        checkpoint.day,
        checkpoint.paid_done,
        True,
        current.remaining_count,
    )

    def mark_in_flight() -> None:
        save_mystery_paid_checkpoint(in_flight, checkpoint_path)
        log(
            "[MysteryHouse] paid click armed: "
            f"completed={checkpoint.paid_done}/2, paid_in_flight=true"
        )

    clicked = click_left_buy_one_safe(
        session,
        pavilion="mystery",
        allowed_states={MysteryState.PAID_3000},
        required_remaining_count=current.remaining_count,
        before_click=mark_in_flight,
        log=log,
    )
    if clicked is None:
        return None
    transition = _wait_draw_surface(
        session,
        "mystery",
        previous_state=MysteryState.PAID_3000,
        paid_risk=True,
        log=log,
    )
    if transition is None:
        log("[MysteryHouse] paid click result unconfirmed; retaining paid_in_flight; no repeat purchase")
        return None
    returned = dismiss_mystery_reward_until_return(
        session,
        "mystery",
        initial_transition=transition,
        paid_risk=True,
        log=log,
    )
    if returned is None:
        return None
    returned_state = returned[1]
    if current.remaining_count is not None:
        returned_state = _confirm_mystery_count_decreased(
            session,
            returned_state,
            current.remaining_count,
            paid_risk=True,
            log=log,
        )
        if returned_state is None:
            return None
        progress = derive_mystery_purchase_progress(returned_state.remaining_count)
        if progress is None:
            return None
        paid_done = progress.paid_done
    else:
        if returned_state.state is not MysteryState.PAID_3000:
            return None
        paid_done = checkpoint.paid_done + 1
    if paid_done != checkpoint.paid_done + 1 or paid_done > max_coin_draws:
        log("[MysteryHouse] paid result conflicts with purchase budget; SafeStop")
        return None
    committed = PaidCheckpoint(_today_text(today), paid_done, False, None)
    save_mystery_paid_checkpoint(committed, checkpoint_path)
    log(f"[MysteryHouse] paid draw completed: {committed.paid_done}/2")
    return committed, returned_state


def _return_home_p1(
    session: MysteryHouseSession,
    *,
    log: Callable[[str], None],
) -> bool:
    def home_selector(_items: Sequence[OcrItem], candidate):
        page = str(getattr(detect_home_page(candidate), "page", "Unknown"))
        return page if page in {"P1", "P2", "P3"} else None

    home = None
    for attempt in range(MAX_RETURN_HOME_CLICKS):
        # A missed navigation tap is retryable only while a fresh frame still
        # proves the original pavilion. Never send another Back on HOME or an
        # unknown overlay, and never repeat a purchase as part of returning.
        frame, items = _snapshot(session)
        page = home_selector(items, frame)
        if page is not None:
            home = frame, items, page
            break
        state = detect_mystery_pavilion_state(items, frame)
        if state is None or state.state not in {MysteryState.PAID_3000, MysteryState.DONE}:
            log_mystery_failure("return_gate", frame, items, retry=attempt,
                                paid_risk=False, log=log)
            return False
        log(f"[MysteryHouse] return HOME attempt: {attempt + 1}/{MAX_RETURN_HOME_CLICKS}")
        _click_fixed_safe_rect(session, frame, PAGE_BACK_SAFE_RECT, label="in-page back", log=log)
        session.sleep(POST_CLICK_WAIT_SECONDS)
        home = _poll(session, home_selector,
                     timeout=RETURN_HOME_TIMEOUT_SECONDS / MAX_RETURN_HOME_CLICKS,
                     stage="return_home", log=log)
        if home is not None:
            break
    if home is None:
        return False
    _, _, page = home
    if page != "P1":
        navigation = navigate_home_page(
            "P1",
            session.capture_frame,
            session.click,
            detect_page=detect_home_page,
            sleep=session.sleep,
            log=log,
        )
        if not navigation.success:
            return False
    log("[MysteryHouse] returned HOME P1")
    return True


def run_mystery_house_daily(
    session: MysteryHouseSession,
    *,
    checkpoint_path: Path | RunRecord | None = None,
    today: date | None = None,
    detect_page: Callable[[object], object] = detect_home_page,
    log: Callable[[str], None] = print,
    allow_coin_draws: bool = True,
    max_coin_draws: int = 2,
) -> MysteryHouseResult:
    if type(allow_coin_draws) is not bool:
        raise ValueError("金币抽取设置必须为布尔值")
    if type(max_coin_draws) is not int or not 1 <= max_coin_draws <= 2:
        raise ValueError("金币抽取上限必须为 1–2 次")
    if checkpoint_path is None:
        checkpoint_path = RunRecord("mystery_house_daily_state.json")
    def fail(
        reason: str,
        paid_done: int | None = None,
        *,
        paid_risk: bool = False,
    ) -> MysteryHouseResult:
        try:
            failed_frame, failed_items = _snapshot(session)
            log_mystery_failure(
                reason,
                failed_frame,
                failed_items,
                retry=0,
                paid_risk=paid_risk,
                log=log,
            )
        except Exception as exc:
            log(f"[MysteryHouse] failure diagnostic unavailable: {exc}")
        return MysteryHouseResult(False, paid_done, reason)

    frame, items = _snapshot(session)
    start = detect_mystery_house_entry_state(
        items, frame, detect_page=detect_page
    )
    if start is None:
        transient, fresh_frame, fresh_items = handle_transient_if_present(
            session, frame, items, log=log
        )
        if transient is GlobalTransientResult.HANDLED_POPUP:
            frame, items = fresh_frame, list(fresh_items)
            start = detect_mystery_house_entry_state(
                items, frame, detect_page=detect_page
            )
    if start is None:
        log_mystery_failure(
            "unknown_start", frame, items, retry=0, paid_risk=False, log=log
        )
        return fail("start_state_unknown")
    log(f"[MysteryHouse] restart state: {start.kind.value}")

    fantasy: PavilionDetection | None = None
    mystery: PavilionDetection | None = None

    if start.kind is EntryKind.HOME:
        navigation = navigate_home_page(
            "P1",
            session.capture_frame,
            session.click,
            detect_page=detect_page,
            sleep=session.sleep,
            log=log,
        )
        if not navigation.success:
            return fail("home_navigation_failed")
        log("[MysteryHouse] current page: HOME_P1")
        entry = _poll(
            session,
            find_mystery_house_home_entry,
            timeout=PAGE_TIMEOUT_SECONDS,
            stage="home_entry",
            log=log,
        )
        if entry is None:
            return fail("home_entry_not_found")
        entry_frame, entry_items, entry_box = entry
        safe = _scaled_rect(HOME_ENTRY_SAFE_RECT, entry_frame)
        if not rect_contains(_box_tuple(safe), _box_tuple(entry_box)):
            log_mystery_failure(
                "home_entry_unsafe",
                entry_frame,
                entry_items,
                retry=0,
                paid_risk=False,
                log=log,
            )
            return fail("home_entry_unsafe")
        _click_fixed_safe_rect(
            session,
            entry_frame,
            HOME_ENTRY_SAFE_RECT,
            label="HOME P1 Mystery House entry",
            log=log,
        )
        entered = _poll(
            session,
            detect_fantasy_pavilion_state,
            timeout=PAGE_TIMEOUT_SECONDS,
            stage="enter_fantasy",
            log=log,
        )
        if entered is None:
            return fail("fantasy_entry_failed")
        _, _, fantasy = entered
        log("[MysteryHouse] entered Fantasy Pavilion")
    elif start.kind is EntryKind.FANTASY:
        fantasy = start.value
    elif start.kind is EntryKind.FANTASY_REWARD:
        returned = dismiss_mystery_reward_until_return(session, "fantasy", log=log)
        if returned is None:
            return fail("fantasy_reward_resume_failed")
        _, fantasy, _ = returned
    elif start.kind is EntryKind.MYSTERY:
        mystery = start.value
    else:
        loaded = (load_mystery_paid_checkpoint(checkpoint_path, today=today) if allow_coin_draws
                  else CheckpointLoad(CheckpointStatus.MISSING, None, "free_only"))
        was_paid = bool(
            loaded.status is CheckpointStatus.VALID
            and loaded.checkpoint is not None
            and loaded.checkpoint.paid_in_flight
        )
        returned = dismiss_mystery_reward_until_return(
            session, "mystery", paid_risk=was_paid, log=log
        )
        if returned is None:
            return fail("mystery_reward_resume_failed", paid_risk=was_paid)
        _, mystery, _ = returned
        if was_paid and mystery.remaining_count is None:
            checkpoint = loaded.checkpoint
            assert checkpoint is not None
            checkpoint = PaidCheckpoint(
                checkpoint.day, checkpoint.paid_done + 1, False
            )
            save_mystery_paid_checkpoint(checkpoint, checkpoint_path)
            log(
                "[MysteryHouse] recovered in-flight paid reward and committed: "
                f"{checkpoint.paid_done}/2"
            )

    if fantasy is not None:
        log(f"[MysteryHouse] fantasy free status: {fantasy.state.value}")
        if fantasy.state is FantasyState.FREE_1_OF_1:
            fantasy = _perform_free_draw(
                session,
                "fantasy",
                fantasy,
                expected_after=FantasyState.DONE,
                log=log,
            )
            if fantasy is None:
                return fail("fantasy_free_failed")
            log("[MysteryHouse] fantasy free draw completed")
        if fantasy.state is not FantasyState.DONE:
            return fail("fantasy_state_unexpected")
        switched = switch_fantasy_to_mystery(session, log=log)
        if switched is None:
            return fail("mystery_switch_failed")
        _, mystery = switched

    assert mystery is not None
    log(f"[MysteryHouse] mystery free status: {mystery.state.value}")

    if not allow_coin_draws:
        # Free-only mode neither needs nor changes the paid checkpoint. Old or
        # uncertain paid records cannot authorize a purchase in this mode.
        free_completed = 1 if mystery.state is MysteryState.FREE_1_OF_2 else 0
        progress = derive_mystery_purchase_progress(mystery.remaining_count) if mystery.remaining_count is not None else None
        if progress is not None and mystery.state in {MysteryState.FREE_2_OF_2, MysteryState.FREE_1_OF_2}:
            expected_count = 0 if mystery.state is MysteryState.FREE_2_OF_2 else 1
            if progress.draw_count != expected_count:
                return fail("remaining_count_state_conflict")
        while mystery.state in {MysteryState.FREE_2_OF_2, MysteryState.FREE_1_OF_2}:
            expected = (MysteryState.FREE_1_OF_2 if mystery.state is MysteryState.FREE_2_OF_2
                        else MysteryState.PAID_3000)
            mystery = _perform_free_draw(session, "mystery", mystery, expected_after=expected, log=log)
            if mystery is None:
                return fail("mystery_free_failed")
            free_completed += 1
            log(f"[MysteryHouse] free draw completed: {free_completed}/2")
        if not _return_home_p1(session, log=log):
            return fail("return_home_failed")
        log("[MysteryHouse] free-only completed; coin draws disabled")
        log("[MysteryHouse] completed")
        return MysteryHouseResult(True, None, "free_only_completed")

    progress = _log_mystery_progress(mystery.remaining_count, log=log)
    if progress is not None and progress.completed:
        # A previous run may have stopped after payment when a toast hid the
        # counter. Reconcile that recorded attempt from the now-visible count.
        loaded = load_mystery_paid_checkpoint(checkpoint_path, today=today)
        if (loaded.status is CheckpointStatus.VALID and loaded.checkpoint is not None
                and loaded.checkpoint.paid_in_flight):
            if _checkpoint_from_reliable_ui(mystery.remaining_count, checkpoint_path,
                                            today=today, log=log) is None:
                return fail("checkpoint_reconcile_failed", paid_risk=True)
        if not _return_home_p1(session, log=log):
            return fail("return_home_failed", progress.paid_done)
        log("[MysteryHouse] completed")
        return MysteryHouseResult(True, progress.paid_done, "completed")

    if progress is not None:
        expected_state = {
            0: MysteryState.FREE_2_OF_2,
            1: MysteryState.FREE_1_OF_2,
            2: MysteryState.PAID_3000,
            3: MysteryState.PAID_3000,
        }.get(progress.draw_count)
        if expected_state is None or mystery.state is not expected_state:
            log(
                "[MysteryHouse] remaining count conflicts with unfinished UI state: "
                f"draw_count={progress.draw_count}, state={mystery.state.value}; SafeStop"
            )
            return fail("remaining_count_state_conflict", paid_risk=True)

        loaded_before_reconcile = load_mystery_paid_checkpoint(
            checkpoint_path, today=today
        )
        if (
            loaded_before_reconcile.status is CheckpointStatus.VALID
            and loaded_before_reconcile.checkpoint is not None
            and loaded_before_reconcile.checkpoint.paid_in_flight
            and loaded_before_reconcile.checkpoint.remaining_before
            == mystery.remaining_count
        ):
            expected_remaining = mystery.remaining_count - 1
            changed = _poll(
                session,
                lambda items, candidate_frame: (
                    candidate
                    if (
                        candidate := detect_mystery_pavilion_state(
                            items, candidate_frame
                        )
                    )
                    is not None
                    and candidate.remaining_count == expected_remaining
                    else None
                ),
                timeout=PAGE_TIMEOUT_SECONDS,
                stage="paid_in_flight_count_wait",
                paid_risk=True,
                log=log,
            )
            if changed is None:
                return fail("paid_in_flight_ambiguous", paid_risk=True)
            _, _, mystery = changed
            progress = _log_mystery_progress(mystery.remaining_count, log=log)
            assert progress is not None

    checkpoint: PaidCheckpoint | None = None
    free_completed = min(2, progress.draw_count) if progress is not None else {
        MysteryState.FREE_2_OF_2: 0,
        MysteryState.FREE_1_OF_2: 1,
        MysteryState.PAID_3000: 2,
    }[mystery.state]
    if progress is not None:
        checkpoint = _checkpoint_from_reliable_ui(
            mystery.remaining_count,
            checkpoint_path,
            today=today,
            log=log,
        )
        if checkpoint is None:
            return fail("checkpoint_reconcile_failed", paid_risk=True)
    if mystery.state in {MysteryState.FREE_2_OF_2, MysteryState.FREE_1_OF_2}:
        if checkpoint is None:
            checkpoint = _checkpoint_from_free_ui(
                checkpoint_path, today=today, log=log
            )
        if checkpoint is None:
            return fail("checkpoint_invalid", paid_risk=True)

    while mystery.state in {
        MysteryState.FREE_2_OF_2,
        MysteryState.FREE_1_OF_2,
    }:
        expected = (
            MysteryState.FREE_1_OF_2
            if mystery.state is MysteryState.FREE_2_OF_2
            else MysteryState.PAID_3000
        )
        mystery = _perform_free_draw(
            session,
            "mystery",
            mystery,
            expected_after=expected,
            log=log,
        )
        if mystery is None:
            return fail("mystery_free_failed")
        free_completed += 1
        log(f"[MysteryHouse] free draw completed: {free_completed}/2")
        if mystery.remaining_count is not None:
            progress = _log_mystery_progress(mystery.remaining_count, log=log)
            checkpoint = _checkpoint_from_reliable_ui(
                mystery.remaining_count,
                checkpoint_path,
                today=today,
                log=log,
            )
            if progress is None or checkpoint is None:
                return fail("free_count_reconcile_failed", paid_risk=True)

    if checkpoint is None:
        loaded = load_mystery_paid_checkpoint(checkpoint_path, today=today)
        if loaded.status is not CheckpointStatus.VALID or loaded.checkpoint is None:
            log(
                "[MysteryHouse] paid UI without a valid same-day checkpoint: "
                f"{loaded.status.value}/{loaded.reason}; SafeStop"
            )
            return fail("paid_checkpoint_untrusted", paid_risk=True)
        checkpoint = loaded.checkpoint

    if checkpoint.paid_in_flight:
        log(
            "[MysteryHouse] paid_in_flight on active paid UI is ambiguous; "
            "zero-click SafeStop"
        )
        return fail(
            "paid_in_flight_ambiguous",
            checkpoint.paid_done,
            paid_risk=True,
        )

    log(f"[MysteryHouse] paid progress: {checkpoint.paid_done}/2")
    while checkpoint.paid_done < max_coin_draws:
        paid_result = _perform_paid_draw(
            session,
            checkpoint,
            mystery,
            checkpoint_path,
            today=today,
            log=log,
            max_coin_draws=max_coin_draws,
        )
        if paid_result is None:
            return fail("paid_draw_failed", checkpoint.paid_done, paid_risk=True)
        checkpoint, mystery = paid_result
        progress = _log_mystery_progress(mystery.remaining_count, log=log)

    if mystery.remaining_count is not None:
        progress = derive_mystery_purchase_progress(mystery.remaining_count)
        if progress is None or progress.paid_done < max_coin_draws:
            return fail("mystery_target_not_completed", checkpoint.paid_done)

    if not _return_home_p1(session, log=log):
        return fail("return_home_failed", checkpoint.paid_done)
    log("[MysteryHouse] completed")
    log(f"[MysteryHouse] coin budget completed: {checkpoint.paid_done}/{max_coin_draws}")
    return MysteryHouseResult(True, checkpoint.paid_done, "completed")


def _make_runtime_session(context):
    controller = context.tasker.controller

    class RuntimeSession:
        @staticmethod
        def capture_frame():
            job = controller.post_screencap()
            job.wait()
            if hasattr(job, "succeeded") and not job.succeeded:
                raise RuntimeError("谜之屋截图失败")
            cached = getattr(controller, "cached_image", None)
            frame = cached() if callable(cached) else cached
            if frame is None or getattr(frame, "size", 0) == 0:
                raise RuntimeError("谜之屋截图为空")
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
                raise RuntimeError(f"谜之屋点击失败：({x}, {y})")

        sleep = staticmethod(time.sleep)
        monotonic = staticmethod(time.monotonic)

    return guard_runtime_session(context, RuntimeSession())


class MysteryHouseDailyAction(CustomAction):
    def __init__(self, run_state=None):
        super().__init__()
        self._run_state = run_state

    def run(self, context, argv) -> bool:
        try:
            params = json.loads(getattr(argv, "custom_action_param", "{}") or "{}") or {}
            if not isinstance(params, dict):
                raise ValueError("谜之屋任务参数必须为对象")
            allow = params.get("allow_coin_draws", True)
            if type(allow) is not bool:
                raise ValueError("金币抽取设置必须为布尔值")
            limit = params.get("max_coin_draws", 2)
            if type(limit) is not int or not 1 <= limit <= 2:
                raise ValueError("金币抽取上限必须为 1–2 次")
            session = _make_runtime_session(context)
            checkpoint_path = run_record(self._run_state, "mystery_house_daily_state.json")
            return run_mystery_house_daily(session, allow_coin_draws=allow,
                                          max_coin_draws=limit, checkpoint_path=checkpoint_path).success
        except Exception as exc:
            print(f"[MysteryHouse] unhandled failure; SafeStop: {exc}")
            return False
