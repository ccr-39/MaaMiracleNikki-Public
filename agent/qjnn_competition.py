from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Callable, Protocol, Sequence

from qjnn_alliance_commission import OcrItem, Rect, find_text_box
from qjnn_common_popup import guard_runtime_session, poll_target_with_common_popups
from qjnn_competition_capture import CompetitionCaptureManager
from qjnn_competition_schedule import competition_time_restricted, COMPETITION_TIME_NOTICE
from qjnn_home import detect_home_page
from qjnn_home_navigation import navigate_home_page
from qjnn_state_recovery import detect_entry_state

try:
    from maa.custom_action import CustomAction
except ImportError:  # MaaFramework is optional for offline state-machine tests.
    CustomAction = object


PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPETITION_REFERENCE_SIZE = (900, 1600)
COMPETITION_ENTRY_ROI = (520, 650, 320, 260)
COMPETITION_HOME_THEME_ROI = (120, 0, 660, 520)
COMPETITION_JUDGE_BUTTON_ROI = (180, 1050, 540, 500)
COMPETITION_LEFT_LIKE_ROI = (20, 850, 400, 620)
COMPETITION_RIGHT_LIKE_ROI = (480, 850, 400, 620)
COMPETITION_REMAINING_ROI = (450, 1480, 430, 120)
COMPETITION_LEFT_PLAYER_NAME_ROI = (100, 1280, 250, 120)
COMPETITION_RIGHT_PLAYER_NAME_ROI = (550, 1280, 250, 120)
COMPETITION_JUDGE_BACK_ROI = (0, 0, 150, 150)
COMPETITION_HOME_BACK_ROI = (0, 0, 150, 150)

COMPETITION_JUDGE_BACK_TEMPLATE = (
    PROJECT_ROOT
    / "assets"
    / "resource"
    / "image"
    / "competition"
    / "judge_back_button.png"
)
COMPETITION_HOME_BACK_TEMPLATE = (
    PROJECT_ROOT
    / "assets"
    / "resource"
    / "image"
    / "competition"
    / "home_back_button.png"
)
COMPETITION_BACK_TEMPLATE_REFERENCE_WIDTH = 720
COMPETITION_LIKE_TEMPLATE = PROJECT_ROOT / "assets/resource/image/competition/like_thumb.png"
COMPETITION_BACK_CLICK_POINT = (70, 65)

COMPETITION_OCR_THRESHOLD = 0.70
COMPETITION_BACK_THRESHOLD = 0.90
COMPETITION_POLL_INTERVAL_SECONDS = 0.5
COMPETITION_PAGE_TIMEOUT_SECONDS = 20.0
# Elaborate outfits can take over ten seconds to load the next player pair.
# Allow time for both the transition and two stable name observations.
COMPETITION_VOTE_REFRESH_TIMEOUT_SECONDS = 20.0
COMPETITION_VOTE_READY_TIMEOUT_SECONDS = 10.0
COMPETITION_HOME_RETURN_TIMEOUT_SECONDS = 12.0
COMPETITION_PAIR_STABLE_FRAMES = 2
NEXT_VOTE_DEBOUNCE_MIN_SECONDS = 1.0
NEXT_VOTE_DEBOUNCE_MAX_SECONDS = 1.5
SUCCESSFUL_VOTES_TARGET = 20
MAX_VOTE_ATTEMPTS = 20

PlayerPair = tuple[str, str]


@dataclass(frozen=True)
class CompetitionHomeState:
    theme: Rect | None
    judge: Rect


@dataclass(frozen=True)
class CompetitionJudgeState:
    left_like: Rect
    right_like: Rect
    remaining: int
    left_player_name: str | None
    right_player_name: str | None
    left_identity_confirmed: bool = False
    right_identity_confirmed: bool = False

    @property
    def player_pair(self) -> PlayerPair | None:
        if self.left_player_name is None or self.right_player_name is None:
            return None
        return self.left_player_name, self.right_player_name


@dataclass(frozen=True)
class CompetitionRefreshObservation:
    remaining: int | None
    left_like: bool
    right_like: bool
    left_player_name: str | None
    right_player_name: str | None
    left_changed: bool
    left_stable_count: int
    right_changed: bool
    right_stable_count: int
    player_group_changed: bool
    phase: str
    remaining_raw_text: str | None = None
    remaining_raw_confidence: float | None = None
    remaining_raw_box: Rect | None = None


@dataclass(frozen=True)
class CompetitionResult:
    success: bool
    remaining: int | None
    votes: int
    reason: str


class CompetitionEntryKind(str, Enum):
    HOME = "HOME"
    COMPETITION_HOME = "COMPETITION_HOME"
    JUDGE_READY = "JUDGE_READY"


@dataclass(frozen=True)
class CompetitionEntryState:
    kind: CompetitionEntryKind
    value: object


class CompetitionSession(Protocol):
    def capture_frame(self): ...

    def recognize_text(self, frame) -> list[OcrItem]: ...

    def click(self, x: int, y: int) -> None: ...

    def sleep(self, seconds: float) -> None: ...

    def monotonic(self) -> float: ...

    def judge_back_score(self, frame) -> float: ...

    def home_back_score(self, frame) -> float: ...


def _map_point(
    point: tuple[int, int], frame_width: int, frame_height: int
) -> tuple[int, int]:
    return (
        round(point[0] * frame_width / COMPETITION_REFERENCE_SIZE[0]),
        round(point[1] * frame_height / COMPETITION_REFERENCE_SIZE[1]),
    )


def _scaled_rect(
    rect: tuple[int, int, int, int], frame_width: int, frame_height: int
) -> Rect:
    left, top = _map_point((rect[0], rect[1]), frame_width, frame_height)
    right, bottom = _map_point(
        (rect[0] + rect[2], rect[1] + rect[3]), frame_width, frame_height
    )
    return Rect(left, top, max(1, right - left), max(1, bottom - top))


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("：", ":")


def _confident_items(items: list[OcrItem]) -> list[OcrItem]:
    return [item for item in items if item.score >= COMPETITION_OCR_THRESHOLD]


def find_competition_entry(items: list[OcrItem], frame) -> Rect | None:
    height, width = frame.shape[:2]
    return find_text_box(
        _confident_items(items),
        "搭配评选赛",
        roi=_scaled_rect(COMPETITION_ENTRY_ROI, width, height),
    )


def find_competition_theme(items: list[OcrItem], frame) -> Rect | None:
    height, width = frame.shape[:2]
    return find_text_box(
        _confident_items(items),
        "本期搭配主题",
        roi=_scaled_rect(COMPETITION_HOME_THEME_ROI, width, height),
    )


def find_competition_judge_button(items: list[OcrItem], frame) -> Rect | None:
    height, width = frame.shape[:2]
    return find_text_box(
        [i for i in _confident_items(items) if i.score >= .85 and ''.join(i.text.split()) == '我当评委'],
        "我当评委",
        roi=_scaled_rect(COMPETITION_JUDGE_BUTTON_ROI, width, height),
    )


def find_competition_home(
    items: list[OcrItem], frame
) -> CompetitionHomeState | None:
    theme = find_competition_theme(items, frame)
    judge = find_competition_judge_button(items, frame)
    if judge is None:
        return None
    return CompetitionHomeState(theme, judge)


def detect_competition_entry_state(
    items: list[OcrItem],
    frame,
    *,
    detect_page: Callable[[object], object] = detect_home_page,
) -> CompetitionEntryState | None:
    judge = find_judge_page(items, frame)
    if judge is not None:
        return CompetitionEntryState(CompetitionEntryKind.JUDGE_READY, judge)
    home = find_competition_home(items, frame)
    if home is not None:
        return CompetitionEntryState(CompetitionEntryKind.COMPETITION_HOME, home)
    page = str(getattr(detect_page(frame), "page", "Unknown"))
    if page in {"P1", "P2", "P3"}:
        return CompetitionEntryState(CompetitionEntryKind.HOME, page)
    return None


def log_competition_home_diagnostics(
    items: list[OcrItem], frame, *, log: Callable[[str], None] = print
) -> None:
    height, width = frame.shape[:2]
    components = (
        ("theme", COMPETITION_HOME_THEME_ROI),
        ("judge", COMPETITION_JUDGE_BUTTON_ROI),
    )
    log(f"[CompetitionHomeDebug] frame: {width}x{height}")
    for name, reference_roi in components:
        roi = _scaled_rect(reference_roi, width, height)
        log(
            f"[CompetitionHomeDebug] {name} ROI: "
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
                f"[CompetitionHomeDebug] {name} OCR: {item.text!r}, "
                f"confidence={item.score:.6f}, box="
                f"({item.box.x}, {item.box.y}, {item.box.width}, {item.box.height})"
            )
    theme = find_competition_theme(items, frame)
    judge = find_competition_judge_button(items, frame)
    log(
        "[CompetitionHomeDebug] matched: "
        f"theme={theme is not None}, judge={judge is not None}"
    )


def parse_competition_remaining(items: list[OcrItem], frame) -> int | None:
    height, width = frame.shape[:2]
    roi = _scaled_rect(COMPETITION_REMAINING_ROI, width, height)
    candidates = [
        item
        for item in _confident_items(items)
        if item.box.right >= roi.x
        and item.box.x <= roi.right
        and item.box.bottom >= roi.y
        and item.box.y <= roi.bottom
    ]
    pattern = re.compile(r"次数:?([0-9]{1,2})/30")
    for item in candidates:
        match = pattern.search(_normalize_text(item.text))
        if match and 0 <= int(match.group(1)) <= 30:
            return int(match.group(1))
    combined = "".join(
        _normalize_text(item.text)
        for item in sorted(candidates, key=lambda item: (item.box.y, item.box.x))
    )
    match = pattern.search(combined)
    if match and 0 <= int(match.group(1)) <= 30:
        return int(match.group(1))
    return None


def find_remaining_ocr_detail(items: list[OcrItem], frame) -> OcrItem | None:
    height, width = frame.shape[:2]
    roi = _scaled_rect(COMPETITION_REMAINING_ROI, width, height)
    candidates = [
        item
        for item in items
        if item.box.right >= roi.x
        and item.box.x <= roi.right
        and item.box.bottom >= roi.y
        and item.box.y <= roi.bottom
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: ("次数" in _normalize_text(item.text), item.score),
    )


def normalize_player_name(text: str) -> str | None:
    normalized = re.sub(r"\s+", "", text).strip(
        ".。,，·•:：;；'\"“”‘’|丨_—-~～[]【】()（）<>《》"
    )
    if not 1 <= len(normalized) <= 24:
        return None
    if normalized in {"赞", "上赞", "自赞", "奖励", "次数", "分享"}:
        return None
    if any(marker in normalized for marker in ("奖励", "次数", "搭配主题")):
        return None
    if re.fullmatch(r"[0-9]{1,2}:[0-9]{2}", normalized):
        return None
    return normalized


def find_player_name(
    items: list[OcrItem], frame, reference_roi: tuple[int, int, int, int]
) -> str | None:
    height, width = frame.shape[:2]
    roi = _scaled_rect(reference_roi, width, height)
    candidates = [
        item
        for item in _confident_items(items)
        if item.box.right >= roi.x
        and item.box.x <= roi.right
        and item.box.bottom >= roi.y
        and item.box.y <= roi.bottom
    ]
    names = [
        name
        for item in sorted(candidates, key=lambda value: (value.box.y, value.box.x))
        if (name := normalize_player_name(item.text)) is not None
    ]
    return "".join(names) or None


def find_player_pair(items: list[OcrItem], frame) -> PlayerPair | None:
    left = find_left_player_name(items, frame)
    right = find_right_player_name(items, frame)
    if left is None or right is None:
        return None
    return left, right


def find_left_player_name(items: list[OcrItem], frame) -> str | None:
    return find_player_name(items, frame, COMPETITION_LEFT_PLAYER_NAME_ROI)


def find_right_player_name(items: list[OcrItem], frame) -> str | None:
    return find_player_name(items, frame, COMPETITION_RIGHT_PLAYER_NAME_ROI)


def find_left_like(items: list[OcrItem], frame) -> Rect | None:
    return _find_like(items, frame, COMPETITION_LEFT_LIKE_ROI)


def find_right_like(items: list[OcrItem], frame) -> Rect | None:
    return _find_like(items, frame, COMPETITION_RIGHT_LIKE_ROI)


def _find_like(items, frame, reference_roi):
    height, width = frame.shape[:2]
    roi = _scaled_rect(reference_roi, width, height)
    target = find_text_box(_confident_items(items), "赞", roi=roi)
    if target is not None:
        return target
    # The thumb is often merged into “自赞” with low OCR confidence. Match
    # the actual button icon instead of lowering the OCR gate for the page.
    import cv2
    template = _load_back_template(COMPETITION_LIKE_TEMPLATE)
    if template is None:
        return None
    scale = width / COMPETITION_BACK_TEMPLATE_REFERENCE_WIDTH
    template = cv2.resize(template, None, fx=scale, fy=scale)
    search = frame[roi.y:roi.bottom, roi.x:roi.right]
    if search.shape[0] < template.shape[0] or search.shape[1] < template.shape[1]:
        return None
    _, score, _, location = cv2.minMaxLoc(cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED))
    if score < .90:
        return None
    return Rect(roi.x + location[0], roi.y + location[1], template.shape[1], template.shape[0])


def find_like_buttons(items: list[OcrItem], frame) -> tuple[Rect, Rect] | None:
    left = find_left_like(items, frame)
    right = find_right_like(items, frame)
    if left is None or right is None:
        return None
    return left, right


def find_judge_page(items: list[OcrItem], frame) -> CompetitionJudgeState | None:
    left = find_left_like(items, frame)
    right = find_right_like(items, frame)
    remaining = parse_competition_remaining(items, frame)
    if left is None or right is None or remaining is None:
        return None
    left_player_name = find_left_player_name(items, frame)
    right_player_name = find_right_player_name(items, frame)
    if remaining > 0 and left_player_name is None and right_player_name is None:
        return None
    return CompetitionJudgeState(
        left,
        right,
        remaining,
        left_player_name,
        right_player_name,
        left_player_name is not None,
        right_player_name is not None,
    )


def find_vote_refresh_state(
    items: list[OcrItem],
    frame,
    *,
    left_changed_confirmed: bool,
    right_changed_confirmed: bool,
) -> CompetitionJudgeState | None:
    refreshed = find_judge_page(items, frame)
    if (
        refreshed is None
        or not (left_changed_confirmed or right_changed_confirmed)
    ):
        return None
    return CompetitionJudgeState(
        refreshed.left_like,
        refreshed.right_like,
        refreshed.remaining,
        refreshed.left_player_name,
        refreshed.right_player_name,
        left_changed_confirmed,
        right_changed_confirmed,
    )


class CompetitionVoteRefreshTracker:
    def __init__(self, previous: CompetitionJudgeState) -> None:
        if (
            previous.remaining > 0
            and previous.left_player_name is None
            and previous.right_player_name is None
        ):
            raise ValueError("positive remaining requires at least one old player name")
        self.previous = previous
        self.left_changed_name: str | None = None
        self.left_stable_count = 0
        self.right_changed_name: str | None = None
        self.right_stable_count = 0
        self.phase = "waiting_pair_change"
        self.last_observation = CompetitionRefreshObservation(
            None, False, False, None, None, False, 0, False, 0, False, self.phase
        )

    @staticmethod
    def _track_side_change(
        current: str | None,
        old: str | None,
        candidate: str | None,
        stable_count: int,
    ) -> tuple[str | None, int, bool]:
        if current is None or old is None or current == old:
            return None, 0, False
        if current == candidate:
            stable_count += 1
        else:
            candidate = current
            stable_count = 1
        return (
            candidate,
            stable_count,
            stable_count >= COMPETITION_PAIR_STABLE_FRAMES,
        )

    def observe(
        self, items: list[OcrItem], frame
    ) -> CompetitionJudgeState | None:
        remaining = parse_competition_remaining(items, frame)
        left = find_left_like(items, frame)
        right = find_right_like(items, frame)
        left_player_name = find_left_player_name(items, frame)
        right_player_name = find_right_player_name(items, frame)
        remaining_detail = find_remaining_ocr_detail(items, frame)
        (
            self.left_changed_name,
            self.left_stable_count,
            left_changed,
        ) = self._track_side_change(
            left_player_name,
            self.previous.left_player_name,
            self.left_changed_name,
            self.left_stable_count,
        )
        (
            self.right_changed_name,
            self.right_stable_count,
            right_changed,
        ) = self._track_side_change(
            right_player_name,
            self.previous.right_player_name,
            self.right_changed_name,
            self.right_stable_count,
        )
        if left_changed or right_changed:
            self.phase = "ready"
        elif self.left_stable_count > 0 or self.right_stable_count > 0:
            self.phase = "waiting_pair_stable"
        else:
            self.phase = "waiting_pair_change"

        player_group_changed = left_changed or right_changed

        self.last_observation = CompetitionRefreshObservation(
            remaining,
            left is not None,
            right is not None,
            left_player_name,
            right_player_name,
            left_changed,
            self.left_stable_count,
            right_changed,
            self.right_stable_count,
            player_group_changed,
            self.phase,
            remaining_raw_text=(remaining_detail.text if remaining_detail else None),
            remaining_raw_confidence=(
                remaining_detail.score if remaining_detail else None
            ),
            remaining_raw_box=(remaining_detail.box if remaining_detail else None),
        )
        ready = find_vote_refresh_state(
            items,
            frame,
            left_changed_confirmed=left_changed,
            right_changed_confirmed=right_changed,
        )
        if ready is not None:
            self.last_observation = CompetitionRefreshObservation(
                remaining,
                left is not None,
                right is not None,
                left_player_name,
                right_player_name,
                left_changed,
                self.left_stable_count,
                right_changed,
                self.right_stable_count,
                player_group_changed,
                self.phase,
                remaining_raw_text=(remaining_detail.text if remaining_detail else None),
                remaining_raw_confidence=(
                    remaining_detail.score if remaining_detail else None
                ),
                remaining_raw_box=(remaining_detail.box if remaining_detail else None),
            )
        return ready


def log_competition_judge_diagnostics(
    items: list[OcrItem], frame, *, log: Callable[[str], None] = print
) -> None:
    height, width = frame.shape[:2]
    components = (
        ("left_like", COMPETITION_LEFT_LIKE_ROI),
        ("right_like", COMPETITION_RIGHT_LIKE_ROI),
        ("left_player_name", COMPETITION_LEFT_PLAYER_NAME_ROI),
        ("right_player_name", COMPETITION_RIGHT_PLAYER_NAME_ROI),
        ("remaining", COMPETITION_REMAINING_ROI),
    )
    log(f"[CompetitionJudgeDebug] frame: {width}x{height}")
    for name, reference_roi in components:
        roi = _scaled_rect(reference_roi, width, height)
        log(
            f"[CompetitionJudgeDebug] {name} ROI: "
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
                f"[CompetitionJudgeDebug] {name} OCR: {item.text!r}, "
                f"confidence={item.score:.6f}, box="
                f"({item.box.x}, {item.box.y}, {item.box.width}, {item.box.height})"
            )
    left = find_left_like(items, frame)
    right = find_right_like(items, frame)
    player_pair = find_player_pair(items, frame)
    remaining = parse_competition_remaining(items, frame)
    log(
        "[CompetitionJudgeDebug] matched: "
        f"left={left is not None}, right={right is not None}, "
        f"remaining={remaining!r}, player_pair={player_pair!r}"
    )


def log_vote_refresh_diagnostics(
    items: list[OcrItem],
    frame,
    *,
    old_remaining: int,
    successful_votes: int,
    old_left_available: bool,
    old_right_available: bool,
    observations: list[CompetitionRefreshObservation],
    log: Callable[[str], None] = print,
) -> None:
    height, width = frame.shape[:2]
    roi = _scaled_rect(COMPETITION_REMAINING_ROI, width, height)
    log(f"[CompetitionVoteRefreshDebug] old_remaining={old_remaining}")
    log(f"[CompetitionVoteRefreshDebug] successful_votes={successful_votes}")
    log(
        "[CompetitionVoteRefreshDebug] remaining ROI: "
        f"({roi.x}, {roi.y}, {roi.width}, {roi.height})"
    )
    for index, observation in enumerate(observations, start=1):
        raw_box = observation.remaining_raw_box
        log(
            "[CompetitionRemainingRefresh] "
            f"poll={index}, before_remaining={old_remaining}, "
            f"observed_remaining={observation.remaining!r}, "
            f"raw_text={observation.remaining_raw_text!r}, "
            f"confidence={observation.remaining_raw_confidence!r}, "
            f"box={(raw_box.x, raw_box.y, raw_box.width, raw_box.height) if raw_box else None!r}"
        )
        log(
            f"[CompetitionVoteRefreshDebug] poll#{index}: "
            f"phase={observation.phase}, "
            f"remaining={observation.remaining!r}, "
            f"old_left_available={old_left_available}, "
            f"old_right_available={old_right_available}, "
            f"left_changed={observation.left_changed}, "
            f"left_stable_count={observation.left_stable_count}, "
            f"right_changed={observation.right_changed}, "
            f"right_stable_count={observation.right_stable_count}, "
            f"player_group_changed={observation.player_group_changed}, "
            f"left_like={observation.left_like}, "
            f"right_like={observation.right_like}, "
            f"left_player_name={observation.left_player_name!r}, "
            f"right_player_name={observation.right_player_name!r}"
        )
    candidates = [
        item
        for item in items
        if item.box.right >= roi.x
        and item.box.x <= roi.right
        and item.box.bottom >= roi.y
        and item.box.y <= roi.bottom
    ]
    for item in candidates:
        log(
            f"[CompetitionVoteRefreshDebug] remaining OCR: {item.text!r}, "
            f"confidence={item.score:.6f}, box="
            f"({item.box.x}, {item.box.y}, {item.box.width}, {item.box.height})"
        )
    remaining = parse_competition_remaining(items, frame)
    left = find_left_like(items, frame)
    right = find_right_like(items, frame)
    left_player_name = find_left_player_name(items, frame)
    right_player_name = find_right_player_name(items, frame)
    player_pair = (
        (left_player_name, right_player_name)
        if left_player_name is not None and right_player_name is not None
        else None
    )
    final = observations[-1] if observations else None
    log(
        "[CompetitionVoteRefreshDebug] final: "
        f"before_remaining={old_remaining}, remaining={remaining!r}, "
        f"phase={final.phase if final else 'waiting_pair_change'}, "
        f"old_left_available={old_left_available}, "
        f"old_right_available={old_right_available}, "
        f"left_changed={final.left_changed if final else False}, "
        f"left_stable_count={final.left_stable_count if final else 0}, "
        f"right_changed={final.right_changed if final else False}, "
        f"right_stable_count={final.right_stable_count if final else 0}, "
        f"player_group_changed={final.player_group_changed if final else False}, "
        f"left_like={left is not None}, right_like={right is not None}, "
        f"left_player_name={left_player_name!r}, "
        f"right_player_name={right_player_name!r}, "
        f"new_pair={player_pair!r}"
    )


def _poll_snapshot(
    session: CompetitionSession,
    selector: Callable[[list[OcrItem], object], object | None],
    *,
    timeout: float,
    on_timeout: Callable[[object, list[OcrItem]], None] | None = None,
) -> tuple[object, list[OcrItem], object] | None:
    return poll_target_with_common_popups(
        session,
        selector,
        timeout=timeout,
        interval=COMPETITION_POLL_INTERVAL_SECONDS,
        on_timeout=on_timeout,
    )


def _player_identity_still_valid(
    expected: CompetitionJudgeState, current: CompetitionJudgeState
) -> bool:
    return bool(
        (
            expected.left_identity_confirmed
            and
            expected.left_player_name is not None
            and current.left_player_name == expected.left_player_name
        )
        or (
            expected.right_identity_confirmed
            and
            expected.right_player_name is not None
            and current.right_player_name == expected.right_player_name
        )
    )


def random_vote_debounce_seconds() -> float:
    return random.uniform(
        NEXT_VOTE_DEBOUNCE_MIN_SECONDS,
        NEXT_VOTE_DEBOUNCE_MAX_SECONDS,
    )


def _debounce_and_revalidate_vote(
    session: CompetitionSession,
    expected: CompetitionJudgeState,
    *,
    timeout: float = COMPETITION_VOTE_READY_TIMEOUT_SECONDS,
    delay_provider: Callable[[], float] = random_vote_debounce_seconds,
    expected_frame=None,
    expected_items: list[OcrItem] | None = None,
    log: Callable[[str], None] = print,
) -> tuple[object, list[OcrItem], CompetitionJudgeState] | None:
    deadline = session.monotonic() + timeout
    failed_attempts: list[tuple[float, object, list[OcrItem], CompetitionJudgeState | None]] = []
    while session.monotonic() < deadline:
        delay = float(delay_provider())
        if not (
            NEXT_VOTE_DEBOUNCE_MIN_SECONDS
            <= delay
            <= NEXT_VOTE_DEBOUNCE_MAX_SECONDS
        ):
            raise ValueError("Competition vote debounce delay is outside its safe range")
        remaining_time = deadline - session.monotonic()
        if remaining_time < delay:
            break
        session.sleep(delay)
        frame = session.capture_frame()
        items = session.recognize_text(frame)
        current = find_judge_page(items, frame)
        # remaining may recover naturally while no vote is being clicked.
        # Revalidate the existing Judge/like-button gates and availability only.
        if current is not None and current.remaining > 0:
            return frame, items, current
        failed_attempts.append((delay, frame, items, current))

    log("[CompetitionVoteReady] debounce revalidation failed")
    if expected_frame is not None and expected_items is not None:
        _log_vote_ready_snapshot(
            "first",
            expected_items,
            expected_frame,
            expected,
            expected_state=expected,
            log=log,
        )
    for index, (delay, frame, items, current) in enumerate(failed_attempts, start=1):
        log(f"[CompetitionVoteReady] revalidation#{index}: delay={delay:.3f}s")
        _log_vote_ready_snapshot(
            f"revalidation#{index}",
            items,
            frame,
            current,
            expected_state=expected,
            log=log,
        )
    return None


def _log_vote_ready_snapshot(
    label: str,
    items: list[OcrItem],
    frame,
    state: CompetitionJudgeState | None,
    *,
    expected_state: CompetitionJudgeState,
    log: Callable[[str], None],
) -> None:
    height, width = frame.shape[:2]

    def candidates(reference_roi: tuple[int, int, int, int]) -> list[OcrItem]:
        roi = _scaled_rect(reference_roi, width, height)
        return [
            item
            for item in items
            if item.box.right >= roi.x
            and item.box.x <= roi.right
            and item.box.bottom >= roi.y
            and item.box.y <= roi.bottom
        ]

    for name, reference_roi in (
        ("left_like", COMPETITION_LEFT_LIKE_ROI),
        ("right_like", COMPETITION_RIGHT_LIKE_ROI),
        ("left_player", COMPETITION_LEFT_PLAYER_NAME_ROI),
        ("right_player", COMPETITION_RIGHT_PLAYER_NAME_ROI),
        ("remaining", COMPETITION_REMAINING_ROI),
    ):
        values = candidates(reference_roi)
        if not values:
            log(f"[CompetitionVoteReady] {label} {name}: no OCR candidates")
            continue
        for item in values:
            log(
                f"[CompetitionVoteReady] {label} {name}: "
                f"text={item.text!r}, confidence={item.score:.6f}, box="
                f"({item.box.x}, {item.box.y}, {item.box.width}, {item.box.height})"
            )

    identity_same = (
        state is not None
        and _player_identity_still_valid(expected_state, state)
    )
    log(
        f"[CompetitionVoteReady] {label} matched: "
        f"judge={state is not None}, "
        f"left_like={state is not None}, right_like={state is not None}, "
        f"remaining={state.remaining if state is not None else None!r}, "
        f"positive={state is not None and state.remaining > 0}, "
        f"first_remaining={expected_state.remaining}, "
        f"left_player={state.left_player_name if state is not None else None!r}, "
        f"right_player={state.right_player_name if state is not None else None!r}, "
        f"identity_available={state is not None and (state.left_player_name is not None or state.right_player_name is not None)}, "
        f"identity_same_diagnostic={identity_same}"
    )


@lru_cache(maxsize=2)
def _load_back_template(path: Path):
    import cv2
    import numpy as np

    if not path.is_file():
        return None
    data = np.fromfile(path, dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def _back_template_score(frame, path: Path, roi_rect) -> float:
    import cv2

    template = _load_back_template(path)
    if template is None:
        return 0.0
    height, width = frame.shape[:2]
    roi = _scaled_rect(roi_rect, width, height)
    search = frame[roi.y : roi.bottom, roi.x : roi.right]
    scale = width / COMPETITION_BACK_TEMPLATE_REFERENCE_WIDTH
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


def competition_judge_back_score(frame) -> float:
    return _back_template_score(
        frame, COMPETITION_JUDGE_BACK_TEMPLATE, COMPETITION_JUDGE_BACK_ROI
    )


def competition_home_back_score(frame) -> float:
    return _back_template_score(
        frame, COMPETITION_HOME_BACK_TEMPLATE, COMPETITION_HOME_BACK_ROI
    )


def _log_back_diagnostics(
    label: str,
    frame,
    roi_rect,
    score: float,
    *,
    threshold: float,
    page_gate: bool,
    log: Callable[[str], None],
) -> None:
    height, width = frame.shape[:2]
    roi = _scaled_rect(roi_rect, width, height)
    log(
        f"[Competition{label}Debug] frame={width}x{height}, "
        f"roi=({roi.x}, {roi.y}, {roi.width}, {roi.height}), "
        f"score={score:.6f}, threshold={threshold:.2f}, "
        f"matched={score >= threshold}, page_gate={page_gate}"
    )


def run_competition_home_exit(
    session: CompetitionSession,
    home_frame,
    home_items: list[OcrItem],
    *,
    remaining: int | None,
    successful_votes: int,
    capture: Callable[[str, object], bool] | None = None,
    detect_page: Callable[[object], object] = detect_home_page,
    log: Callable[[str], None] = print,
) -> CompetitionResult:
    home_gate = find_competition_home(home_items, home_frame) is not None
    home_back_score = session.home_back_score(home_frame)
    if not home_gate or home_back_score < COMPETITION_BACK_THRESHOLD:
        _log_back_diagnostics(
            "HomeBack",
            home_frame,
            COMPETITION_HOME_BACK_ROI,
            home_back_score,
            threshold=COMPETITION_BACK_THRESHOLD,
            page_gate=home_gate,
            log=log,
        )
        if capture is not None:
            capture("QJNN_CompetitionHomeBackFailure", home_frame)
        return CompetitionResult(
            False, remaining, successful_votes, "home_back_not_found"
        )

    height, width = home_frame.shape[:2]
    session.click(*_map_point(COMPETITION_BACK_CLICK_POINT, width, height))
    returned_p3 = _poll_snapshot(
        session,
        lambda items, frame: (
            "P3" if str(getattr(detect_page(frame), "page", "Unknown")) == "P3" else None
        ),
        timeout=COMPETITION_HOME_RETURN_TIMEOUT_SECONDS,
    )
    if returned_p3 is None:
        log("Competition exit did not return to Home P3")
        return CompetitionResult(
            False, remaining, successful_votes, "home_p3_not_confirmed"
        )
    home_p3_frame, _, _ = returned_p3
    if capture is not None:
        capture("QJNN_CompetitionBackHome", home_p3_frame)
    return CompetitionResult(True, remaining, successful_votes, "completed")


def run_competition_judge_exit(
    session: CompetitionSession,
    judge_frame,
    judge_items: list[OcrItem],
    *,
    successful_votes: int,
    required_successful_votes: int = SUCCESSFUL_VOTES_TARGET,
    capture: Callable[[str, object], bool] | None = None,
    detect_page: Callable[[object], object] = detect_home_page,
    log: Callable[[str], None] = print,
) -> CompetitionResult:
    judge = find_judge_page(judge_items, judge_frame)
    if successful_votes < required_successful_votes or judge is None:
        log("Competition judge exit refused: vote target or JudgePage gate missing")
        return CompetitionResult(
            False,
            judge.remaining if judge is not None else None,
            successful_votes,
            "judge_exit_gate_failed",
        )

    judge_back_score = session.judge_back_score(judge_frame)
    if judge_back_score < COMPETITION_BACK_THRESHOLD:
        _log_back_diagnostics(
            "JudgeBack",
            judge_frame,
            COMPETITION_JUDGE_BACK_ROI,
            judge_back_score,
            threshold=COMPETITION_BACK_THRESHOLD,
            page_gate=True,
            log=log,
        )
        if capture is not None:
            capture("QJNN_CompetitionJudgeBackFailure", judge_frame)
        return CompetitionResult(
            False, judge.remaining, successful_votes, "judge_back_not_found"
        )

    height, width = judge_frame.shape[:2]
    session.click(*_map_point(COMPETITION_BACK_CLICK_POINT, width, height))

    def competition_home_timeout(frame, items: list[OcrItem]) -> None:
        if capture is None:
            return
        log_competition_home_diagnostics(items, frame, log=log)
        capture("QJNN_CompetitionHomeWaitFailure", frame)

    returned_home = _poll_snapshot(
        session,
        find_competition_home,
        timeout=COMPETITION_PAGE_TIMEOUT_SECONDS,
        on_timeout=competition_home_timeout,
    )
    if returned_home is None:
        log("Competition did not return to CompetitionHome")
        return CompetitionResult(
            False,
            judge.remaining,
            successful_votes,
            "return_competition_home_failed",
        )
    competition_home_frame, competition_home_items, _ = returned_home
    return run_competition_home_exit(
        session,
        competition_home_frame,
        competition_home_items,
        remaining=judge.remaining,
        successful_votes=successful_votes,
        capture=capture,
        detect_page=detect_page,
        log=log,
    )


def run_competition_daily(
    session: CompetitionSession,
    *,
    choose_like: Callable[[Sequence[Rect]], Rect] = random.choice,
    capture: Callable[[str, object], bool] | None = None,
    detect_page: Callable[[object], object] = detect_home_page,
    log: Callable[[str], None] = print,
    debounce_delay_provider: Callable[[], float] = random_vote_debounce_seconds,
    successful_votes_target: int = SUCCESSFUL_VOTES_TARGET,
    max_vote_attempts: int = MAX_VOTE_ATTEMPTS,
    enter: bool = False,
    progress=None,
) -> CompetitionResult:
    if type(enter) is not bool:
        raise ValueError("我要参赛设置必须为布尔值")
    if competition_time_restricted():
        log(f"[Competition] {COMPETITION_TIME_NOTICE} 本次正常跳过。")
        return CompetitionResult(True, None, 0, "competition_time_restricted")
    def capture_failure(state: str):
        return lambda frame, items: capture(state, frame) if capture is not None else None

    def competition_home_timeout(frame, items: list[OcrItem]) -> None:
        if capture is None:
            return
        log_competition_home_diagnostics(items, frame, log=log)
        capture("QJNN_CompetitionHomeWaitFailure", frame)

    def competition_judge_timeout(frame, items: list[OcrItem]) -> None:
        if capture is None:
            return
        log_competition_judge_diagnostics(items, frame, log=log)
        capture("QJNN_CompetitionJudgeWaitFailure", frame)

    recovery = detect_entry_state(
        session,
        lambda items, frame: detect_competition_entry_state(
            list(items), frame, detect_page=detect_page
        ),
        task_name="QJNN_CompetitionDaily",
        capture_unknown=capture,
        unknown_capture_state="QJNN_CompetitionUnknownStart",
        log=log,
    )
    if recovery is None:
        return CompetitionResult(False, None, 0, "start_state_unknown")
    start = recovery.state
    log(f"[Competition] restart state: {start.kind.value}")
    registration_record_path=None
    if enter and getattr(session,'account_scoped_records',False):
        from qjnn_account_scope import account_record_path
        from qjnn_competition_enter import RECORD
        if start.kind is CompetitionEntryKind.HOME:
            registration_record_path=account_record_path(session,RECORD)

    if start.kind is CompetitionEntryKind.HOME:
        entry_box = find_competition_entry(recovery.items, recovery.frame)
        if start.value != "P3" or entry_box is None:
            navigation = navigate_home_page(
                "P3",
                session.capture_frame,
                session.click,
                sleep=session.sleep,
                log=log,
            )
            if not navigation.success:
                return CompetitionResult(False, None, 0, "home_navigation_failed")
            entry = _poll_snapshot(
                session,
                find_competition_entry,
                timeout=COMPETITION_PAGE_TIMEOUT_SECONDS,
            )
            if entry is None:
                log("Competition entry was not found on Home P3")
                return CompetitionResult(False, None, 0, "entry_not_found")
            _, _, entry_box = entry
        session.click(*entry_box.center)
        home_result = _poll_snapshot(
            session,
            find_competition_home,
            timeout=COMPETITION_PAGE_TIMEOUT_SECONDS,
            on_timeout=competition_home_timeout,
        )
        if home_result is None:
            log("Competition home did not become ready")
            return CompetitionResult(False, None, 0, "competition_home_not_found")
        home_frame, _, home = home_result
    elif start.kind is CompetitionEntryKind.COMPETITION_HOME:
        home_frame = recovery.frame
        home = start.value
    else:
        judge_frame = recovery.frame
        judge_items = recovery.items
        judge = start.value
        home = None

    if home is not None:
        if enter:
            from qjnn_competition_enter import run_registration
            registration_options={'record_path':registration_record_path} if registration_record_path is not None else {}
            if not run_registration(session, capture=capture, log=log,**registration_options):
                return CompetitionResult(False, None, 0, "registration_unconfirmed")
            # Signup can replace the page; never reuse its earlier judge box.
            signup_home = _poll_snapshot(session, find_competition_home, timeout=COMPETITION_PAGE_TIMEOUT_SECONDS)
            if signup_home is None:
                return CompetitionResult(False, None, 0, "competition_home_after_registration_unknown")
            home_frame, _, home = signup_home
        if competition_time_restricted():
            log(f"[Competition] {COMPETITION_TIME_NOTICE} 返回主页。")
            current_frame = session.capture_frame()
            current_items = session.recognize_text(current_frame)
            return run_competition_home_exit(
                session, current_frame, current_items, remaining=None,
                successful_votes=0, capture=capture, detect_page=detect_page, log=log)
        if capture is not None:
            capture("QJNN_CompetitionHome", home_frame)
        session.click(*home.judge.center)
        judge_result = _poll_snapshot(
            session,
            find_judge_page,
            timeout=COMPETITION_PAGE_TIMEOUT_SECONDS,
            on_timeout=competition_judge_timeout,
        )
        if judge_result is None:
            log("Competition judge page did not become ready")
            return CompetitionResult(False, None, 0, "judge_page_not_found")
        judge_frame, judge_items, judge = judge_result

    successful_votes = 0
    vote_attempts = 0
    saved = progress.read() if progress is not None else None
    pending = None
    if saved is not None:
        if (not isinstance(saved, dict) or type(saved.get('successful_votes')) is not int
                or type(saved.get('vote_attempts')) is not int
                or not 0 <= saved['successful_votes'] <= saved['vote_attempts'] <= 30):
            raise ValueError('invalid competition continuation state')
        successful_votes, vote_attempts = saved['successful_votes'], saved['vote_attempts']
        pending = saved.get('pending')
        if pending is not None:
            if (not isinstance(pending, dict) or type(pending.get('remaining')) is not int
                    or not 1 <= pending['remaining'] <= 30
                    or any(pending.get(side) is not None and not isinstance(pending[side], str)
                           for side in ('left', 'right'))):
                raise ValueError('invalid pending competition vote')
            previous = CompetitionJudgeState(judge.left_like, judge.right_like,
                pending['remaining'], pending.get('left'), pending.get('right'))
            tracker = CompetitionVoteRefreshTracker(previous)
            refreshed = _poll_snapshot(session, tracker.observe,
                                      timeout=COMPETITION_VOTE_REFRESH_TIMEOUT_SECONDS)
            if refreshed is None:
                return CompetitionResult(False, judge.remaining, successful_votes, 'pending_vote_unconfirmed')
            judge_frame, judge_items, judge = refreshed
            successful_votes += 1
            pending = None

    def save_progress():
        if progress is not None:
            progress.write({'successful_votes': successful_votes,
                            'vote_attempts': vote_attempts, 'pending': pending})
    save_progress()
    log(f"[Competition] remaining: {judge.remaining}/30")
    log(f"[Competition] run target: {successful_votes}/{successful_votes_target}")

    exhausted = judge.remaining == 0
    time_restricted = False
    for _ in range(max(0, max_vote_attempts - vote_attempts)):
        if successful_votes >= successful_votes_target:
            break
        if competition_time_restricted():
            time_restricted = True
            break
        if judge.remaining == 0:
            exhausted = True
            log(
                f"[Competition] remaining: 0/30; "
                f"ending normally at {successful_votes}/{successful_votes_target}"
            )
            break
        revalidated_vote = _debounce_and_revalidate_vote(
            session,
            judge,
            delay_provider=debounce_delay_provider,
            expected_frame=judge_frame,
            expected_items=judge_items,
            log=log,
        )
        if revalidated_vote is None:
            log("Competition vote-ready state did not survive debounce revalidation")
            return CompetitionResult(
                False, judge.remaining, successful_votes, "vote_revalidate_failed"
            )
        judge_frame, judge_items, judge = revalidated_vote
        if capture is not None:
            capture("QJNN_CompetitionJudgeActive", judge_frame)

        choices = (judge.left_like, judge.right_like)
        selected = choose_like(choices)
        if selected not in choices:
            log("Competition RNG returned a point outside the two confirmed like buttons")
            return CompetitionResult(
                False, judge.remaining, successful_votes, "invalid_rng_choice"
            )
        old_remaining = judge.remaining
        old_left_name = judge.left_player_name
        old_right_name = judge.right_player_name
        if old_left_name is None and old_right_name is None:
            log("Competition player identity was not ready before voting")
            return CompetitionResult(
                False, old_remaining, successful_votes, "player_pair_not_ready"
            )
        # Recheck after debounce/OCR/selection: a run can cross 02:00 while
        # preparing a vote. Navigation back out remains allowed.
        if competition_time_restricted():
            time_restricted = True
            break
        vote_attempts += 1
        pending = {'remaining': old_remaining, 'left': old_left_name, 'right': old_right_name}
        save_progress()
        session.click(*selected.center)
        selected_side = "left" if selected == judge.left_like else "right"
        log(
            f"[Competition] vote click: {vote_attempts}/{successful_votes_target}; "
            f"max_attempts={max_vote_attempts}, "
            f"side={selected_side}, box="
            f"({selected.x}, {selected.y}, {selected.width}, {selected.height}), "
            f"point={selected.center}"
        )

        refresh_tracker = CompetitionVoteRefreshTracker(judge)
        refresh_observations: list[CompetitionRefreshObservation] = []

        def select_vote_refresh(items: list[OcrItem], frame):
            outcome = refresh_tracker.observe(items, frame)
            if capture is not None:
                refresh_observations.append(refresh_tracker.last_observation)
            return outcome

        def vote_refresh_timeout(frame, items: list[OcrItem]) -> None:
            if capture is None:
                return
            log_vote_refresh_diagnostics(
                items,
                frame,
                old_remaining=old_remaining,
                successful_votes=successful_votes,
                old_left_available=old_left_name is not None,
                old_right_available=old_right_name is not None,
                observations=refresh_observations,
                log=log,
            )
            capture("QJNN_CompetitionNextPairWaitFailure", frame)

        refreshed = _poll_snapshot(
            session,
            select_vote_refresh,
            timeout=COMPETITION_VOTE_REFRESH_TIMEOUT_SECONDS,
            on_timeout=vote_refresh_timeout,
        )
        if refreshed is None:
            if refresh_tracker.phase == "waiting_pair_change":
                log("Competition next player pair did not change")
                reason = "player_pair_not_changed"
            else:
                log("Competition next player pair did not become stable")
                reason = "player_pair_not_stable"
            return CompetitionResult(
                False, judge.remaining, successful_votes, reason
            )
        judge_frame, judge_items, judge = refreshed
        successful_votes += 1
        pending = None
        save_progress()
        log(
            f"[Competition] successful votes: "
            f"{successful_votes}/{successful_votes_target}"
        )
        if judge.remaining == 0:
            exhausted = True
            log(
                f"[Competition] remaining: 0/30; "
                f"ending normally at {successful_votes}/{successful_votes_target}"
            )
            break
        if successful_votes >= successful_votes_target:
            break

    if time_restricted:
        log(f"[Competition] {COMPETITION_TIME_NOTICE} 停止点赞并返回主页。")
    if successful_votes < successful_votes_target and not exhausted and not time_restricted:
        log("Competition did not reach the successful vote target")
        return CompetitionResult(
            False, judge.remaining, successful_votes, "vote_target_not_reached"
        )
    if capture is not None:
        capture("QJNN_CompetitionJudgeDone", judge_frame)
    return run_competition_judge_exit(
        session,
        judge_frame,
        judge_items,
        successful_votes=successful_votes,
        required_successful_votes=(
            successful_votes if exhausted or time_restricted else successful_votes_target
        ),
        capture=capture,
        detect_page=detect_page,
        log=log,
    )


def run_competition_exit_debug(
    session: CompetitionSession,
    *,
    capture: Callable[[str, object], bool] | None = None,
    detect_page: Callable[[object], object] = detect_home_page,
    log: Callable[[str], None] = print,
) -> CompetitionResult:
    judge_frame = session.capture_frame()
    judge_items = session.recognize_text(judge_frame)
    judge = find_judge_page(judge_items, judge_frame)
    if judge is None:
        log("Competition exit debug refused: current screen is not JudgePage")
        return CompetitionResult(False, None, 0, "judge_page_not_found")
    if capture is not None:
        capture("QJNN_CompetitionJudgeDone", judge_frame)
    return run_competition_judge_exit(
        session,
        judge_frame,
        judge_items,
        successful_votes=SUCCESSFUL_VOTES_TARGET,
        capture=capture,
        detect_page=detect_page,
        log=log,
    )


def _recognize_competition_text(context, frame) -> list[OcrItem]:
    def read(image, offset=(0, 0)):
        detail = context.run_recognition("QJNN_AgentOcrBase", image)
        if detail is None:
            return []
        results = list(getattr(detail, "all_results", []) or [])
        if not results and getattr(detail, "best_result", None) is not None:
            results = [detail.best_result]
        converted = []
        for result in results:
            box = result.box
            if all(hasattr(box, name) for name in ("x", "y", "w", "h")):
                x, y, w, h = (int(getattr(box, name)) for name in ("x", "y", "w", "h"))
            else:
                x, y, w, h = (int(value) for value in list(box)[:4])
            converted.append(OcrItem(str(result.text),
                Rect(x + offset[0], y + offset[1], w, h), float(getattr(result, "score", 0.0))))
        return converted

    items = read(frame)
    # Full-screen OCR can miss white names over animated outfits. Retry only
    # missing names in their existing ROIs on this same captured frame. This
    # does not relax confidence, consecutive-frame stability or click gates.
    if find_like_buttons(items, frame) is not None and parse_competition_remaining(items, frame) is not None:
        height, width = frame.shape[:2]
        for roi in (COMPETITION_LEFT_PLAYER_NAME_ROI, COMPETITION_RIGHT_PLAYER_NAME_ROI):
            if find_player_name(items, frame, roi) is not None:
                continue
            rect = _scaled_rect(roi, width, height)
            crop = frame[rect.y:rect.bottom, rect.x:rect.right].copy()
            if crop.size:
                items.extend(read(crop, (rect.x, rect.y)))
    return items


def _make_runtime_session(context):
    controller = context.tasker.controller

    class RuntimeSession:
        account_scoped_records=True
        @staticmethod
        def capture_frame():
            job = controller.post_screencap()
            job.wait()
            if hasattr(job, "succeeded") and not job.succeeded:
                raise RuntimeError("搭配评选赛截图失败")
            cached = getattr(controller, "cached_image", None)
            frame = cached() if callable(cached) else cached
            if frame is None or getattr(frame, "size", 0) == 0:
                raise RuntimeError("搭配评选赛截图为空")
            return frame

        @staticmethod
        def recognize_text(frame) -> list[OcrItem]:
            return _recognize_competition_text(context, frame)

        @staticmethod
        def click(x: int, y: int) -> None:
            job = controller.post_click(x, y)
            job.wait()
            if hasattr(job, "succeeded") and not job.succeeded:
                raise RuntimeError(f"搭配评选赛点击失败：({x}, {y})")

        sleep = staticmethod(time.sleep)
        monotonic = staticmethod(time.monotonic)
        judge_back_score = staticmethod(competition_judge_back_score)
        home_back_score = staticmethod(competition_home_back_score)

    return guard_runtime_session(context, RuntimeSession())


class CompetitionDailyAction(CustomAction):
    def __init__(
        self, competition_capture: CompetitionCaptureManager | None = None, run_state=None
    ) -> None:
        super().__init__()
        self._competition_capture = competition_capture
        self._run_state = run_state

    def run(self, context, argv) -> bool:
        try:
            params = json.loads(getattr(argv, "custom_action_param", "{}") or "{}")
            if params is None:params={}
            if not isinstance(params,dict):raise ValueError('评选赛设置必须为对象')
            votes = params.get("votes", SUCCESSFUL_VOTES_TARGET)
            enter = params.get("enter", False)
            if type(enter) is not bool:
                raise ValueError("我要参赛设置必须为布尔值")
            if type(votes) is not int or not 1 <= votes <= 30:
                raise ValueError("评选赛投票次数必须为 1–30 的整数")
            capture = (
                self._competition_capture.capture_once
                if self._competition_capture is not None
                else None
            )
            from qjnn_run_state import run_record
            return run_competition_daily(
                _make_runtime_session(context), capture=capture,
                successful_votes_target=votes, max_vote_attempts=votes,
                **({"enter": True} if enter else {}),
                **({'progress': run_record(self._run_state, 'competition_daily_state.json')}
                   if self._run_state is not None else {}),
            ).success
        except Exception as exc:
            print(f"搭配评选赛任务失败：{exc}")
            return False


class CompetitionExitDebugAction(CustomAction):
    def __init__(
        self, competition_capture: CompetitionCaptureManager | None = None
    ) -> None:
        super().__init__()
        self._competition_capture = competition_capture

    def run(self, context, argv) -> bool:
        try:
            capture = (
                self._competition_capture.capture_once
                if self._competition_capture is not None
                else None
            )
            return run_competition_exit_debug(
                _make_runtime_session(context), capture=capture
            ).success
        except Exception as exc:
            print(f"搭配评选赛退出调试失败：{exc}")
            return False
