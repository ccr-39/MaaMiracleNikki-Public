from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from qjnn_alliance_click import alliance_business_click
from qjnn_alliance_capture import (
    AllianceCaptureManager,
)
from qjnn_common_popup import guard_runtime_session, poll_target_with_common_popups

try:
    from maa.custom_action import CustomAction
except ImportError:  # MaaFramework is optional for offline OCR/state-machine tests.
    CustomAction = object


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALLIANCE_TEMPLATE_ROOT = PROJECT_ROOT / "assets" / "resource" / "image" / "alliance"
COMMISSION_BACK_TEMPLATE = ALLIANCE_TEMPLATE_ROOT / "commission_back_arrow.png"

ALLIANCE_REFERENCE_SIZE = (900, 1600)
ALLIANCE_ENTRY_ROI = (430, 300, 300, 230)
ALLIANCE_TITLE_ROI = (240, 15, 420, 120)
ALLIANCE_COMMISSION_ENTRY_ROI = (300, 950, 300, 200)
COMMISSION_REMAINING_ROI = (550, 1450, 350, 150)
COMMISSION_CHAPTER_SWITCH_ROI = (0, 1320, 240, 280)
COMMISSION_IN_PROGRESS_ROI = (160, 350, 650, 950)
COMMISSION_COMPLETE_ROI = (40, 1080, 760, 180)
COMMISSION_BACK_ROI = (0, 180, 150, 170)

COMMISSION_BACK_CLICK_POINT = (70, 265)
SETTLEMENT_OUTSIDE_FIRST_POINT = (450, 200)
SETTLEMENT_OUTSIDE_SECOND_POINT = (450, 1400)
COMMISSION_NODE_Y_OFFSET = 75

ALLIANCE_OCR_ATTEMPTS = 3
ALLIANCE_OCR_RETRY_WAIT_SECONDS = 0.5
ALLIANCE_PAGE_WAIT_SECONDS = 0.8
ALLIANCE_SETTLEMENT_TIMEOUT_SECONDS = 20.0
ALLIANCE_SETTLEMENT_POLL_SECONDS = 0.5
ALLIANCE_POST_SETTLEMENT_TIMEOUT_SECONDS = 10.0
ALLIANCE_MAX_COMMISSION_LOOPS = 5
COMMISSION_BACK_THRESHOLD = 0.80
COMMISSION_SEARCH_SWIPES = (
    (450, 1150, 450, 550), (450, 550, 450, 1150),
    (450, 550, 450, 1150), (450, 1150, 450, 550),
    (700, 800, 200, 800), (200, 800, 700, 800),
    (200, 800, 700, 800), (700, 800, 200, 800),
)


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    def union(self, other: Rect) -> Rect:
        left = min(self.x, other.x)
        top = min(self.y, other.y)
        right = max(self.right, other.right)
        bottom = max(self.bottom, other.bottom)
        return Rect(left, top, right - left, bottom - top)


@dataclass(frozen=True)
class OcrItem:
    text: str
    box: Rect
    score: float = 0.0


@dataclass(frozen=True)
class AllianceCommissionResult:
    success: bool
    remaining_count: int | None
    loops: int
    reason: str


class AllianceSession(Protocol):
    def capture_frame(self): ...

    def recognize_text(self, frame) -> list[OcrItem]: ...

    def click(self, x: int, y: int) -> None: ...

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int): ...

    def sleep(self, seconds: float) -> None: ...

    def monotonic(self) -> float: ...

    def back_arrow_score(self, frame) -> float: ...


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("：", ":")


def map_alliance_reference_point(
    point: tuple[int, int],
    frame_width: int,
    frame_height: int,
) -> tuple[int, int]:
    reference_width, reference_height = ALLIANCE_REFERENCE_SIZE
    return (
        round(point[0] * frame_width / reference_width),
        round(point[1] * frame_height / reference_height),
    )


def _scaled_roi(
    roi: tuple[int, int, int, int],
    frame_width: int,
    frame_height: int,
) -> Rect:
    left, top = map_alliance_reference_point((roi[0], roi[1]), frame_width, frame_height)
    right, bottom = map_alliance_reference_point(
        (roi[0] + roi[2], roi[1] + roi[3]),
        frame_width,
        frame_height,
    )
    return Rect(left, top, max(1, right - left), max(1, bottom - top))


def _box_in_roi(box: Rect, roi: Rect) -> bool:
    center_x, center_y = box.center
    return roi.x <= center_x <= roi.right and roi.y <= center_y <= roi.bottom


def find_text_box(
    items: list[OcrItem],
    target: str,
    *,
    roi: Rect | None = None,
) -> Rect | None:
    normalized_target = _normalize_text(target)
    matches = [
        item
        for item in items
        if normalized_target in _normalize_text(item.text)
        and (roi is None or _box_in_roi(item.box, roi))
    ]
    if not matches:
        candidates = [
            item for item in items if roi is None or _box_in_roi(item.box, roi)
        ]
        for first in candidates:
            for second in candidates:
                if first is second:
                    continue
                vertical_distance = abs(first.box.center[1] - second.box.center[1])
                horizontal_gap = max(
                    0,
                    max(first.box.x, second.box.x)
                    - min(first.box.right, second.box.right),
                )
                if vertical_distance > max(first.box.height, second.box.height):
                    continue
                if horizontal_gap > 2 * max(first.box.height, second.box.height):
                    continue
                ordered = sorted((first, second), key=lambda item: item.box.x)
                combined_text = "".join(_normalize_text(item.text) for item in ordered)
                if normalized_target in combined_text:
                    return first.box.union(second.box)
        return None
    return max(matches, key=lambda item: item.score).box


def find_alliance_entry_box(items: list[OcrItem], frame) -> Rect | None:
    height, width = frame.shape[:2]
    roi = _scaled_roi(ALLIANCE_ENTRY_ROI, width, height)
    whole = find_text_box(items, "搭配师联盟", roi=roi)
    if whole is not None:
        return whole

    stylist = find_text_box(items, "搭配师", roi=roi)
    alliance = find_text_box(items, "联盟", roi=roi)
    if stylist is None or alliance is None:
        return None
    combined = stylist.union(alliance)
    max_gap = round(90 * min(width, height) / 900)
    horizontal_gap = max(0, max(stylist.x, alliance.x) - min(stylist.right, alliance.right))
    vertical_gap = max(0, max(stylist.y, alliance.y) - min(stylist.bottom, alliance.bottom))
    return combined if max(horizontal_gap, vertical_gap) <= max_gap else None


def find_alliance_home(items: list[OcrItem], frame) -> tuple[Rect, Rect] | None:
    height, width = frame.shape[:2]
    title_roi = _scaled_roi(ALLIANCE_TITLE_ROI, width, height)
    entry_roi = _scaled_roi(ALLIANCE_COMMISSION_ENTRY_ROI, width, height)
    title = find_text_box(items, "搭配师联盟", roi=title_roi)
    commission = find_text_box(items, "委托", roi=entry_roi)
    if title is None or commission is None:
        return None
    return title, commission


def parse_remaining_count(items: list[OcrItem], frame) -> int | None:
    height, width = frame.shape[:2]
    roi = _scaled_roi(COMMISSION_REMAINING_ROI, width, height)
    pattern = re.compile(r"今日剩余次数:?([0-5])/5(?!\d)")
    candidates = [item for item in items if item.score >= .8 and _box_in_roi(item.box, roi)]
    counts = set()
    for item in candidates:
        match = pattern.search(_normalize_text(item.text))
        if match:
            counts.add(int(match.group(1)))
    if counts:
        return next(iter(counts)) if len(counts) == 1 else None
    combined = "".join(
        _normalize_text(item.text)
        for item in sorted(candidates, key=lambda item: (item.box.y, item.box.x))
    )
    match = pattern.search(combined)
    if match:
        return int(match.group(1))
    return None


def detect_commission_map(items: list[OcrItem], frame) -> int | None:
    """Identify the stable chapter map, never a detail or settlement overlay."""
    height, width = frame.shape[:2]
    chapter_switch = find_text_box(
        items,
        "切换章节",
        roi=_scaled_roi(COMMISSION_CHAPTER_SWITCH_ROI, width, height),
    )
    remaining = parse_remaining_count(items, frame)
    if chapter_switch is None or remaining is None:
        return None
    if find_text_box(items, "过关完成") is not None or detail_popup_visible(items):
        return None
    return remaining


def find_in_progress_box(items: list[OcrItem], frame) -> Rect | None:
    height, width = frame.shape[:2]
    roi = _scaled_roi(COMMISSION_IN_PROGRESS_ROI, width, height)
    boxes = [i.box for i in items if i.score >= .8
             and _normalize_text(i.text) == "进行中" and _box_in_roi(i.box, roi)]
    return min(boxes, key=lambda b: (b.y, b.x)) if boxes else None


def find_complete_box(items: list[OcrItem], frame, remaining_count: int) -> Rect | None:
    if remaining_count not in {1, 2, 3, 4, 5}:
        return None
    height, width = frame.shape[:2]
    roi = _scaled_roi(COMMISSION_COMPLETE_ROI, width, height)
    boxes = [i.box for i in items if i.score >= .8 and _normalize_text(i.text) == f"完成{remaining_count}次"
             and _box_in_roi(i.box,roi)]
    return boxes[0] if len(boxes)==1 else None


def complete_one_is_gray(frame, box: Rect) -> bool:
    """Conservatively reject the known disabled 完成1次 visual state."""
    import cv2

    frame_height, frame_width = frame.shape[:2]
    padding = max(2, round(10 * min(frame_width, frame_height) / 900))
    left = max(0, box.x - padding)
    top = max(0, box.y - padding)
    right = min(frame_width, box.right + padding)
    bottom = min(frame_height, box.bottom + padding)
    region = frame[top:bottom, left:right]
    if region.size == 0:
        return True
    saturation = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)[:, :, 1]
    return float(saturation.mean()) <= 15.0


def detail_popup_visible(items: list[OcrItem]) -> bool:
    return special_confirmation_visible(items) or any(
        find_text_box(items, target) is not None
        for target in ("完成1次", "完成2次", "完成3次", "完成4次", "完成5次", "接受", "委托已完成")
    )


def special_confirmation_visible(items):
    return any(_normalize_text(i.text).startswith(("快速过关", "是否只快速完成")) for i in items)


def parse_short_completion(items, frame, available):
    """Two independent counts and a geometrically confirmed cancel/OK modal."""
    if not 1 <= available <= 5:
        return None
    h, w = frame.shape[:2]
    roi = _scaled_roi((120, 400, 660, 830), w, h)
    values = [i for i in items if i.score >= .85 and roi.x <= i.box.x < i.box.right <= roi.right
              and roi.y <= i.box.y < i.box.bottom <= roi.bottom]
    lines = []
    for value in sorted(values, key=lambda i: (i.box.center[1], i.box.x)):
        row = next((row for row in lines if abs(row[0].box.center[1] - value.box.center[1])
                    <= min(row[0].box.height, value.box.height) / 2), None)
        if row is None:
            lines.append([value])
        else:
            row.append(value)
    texts = [("".join(_normalize_text(v.text) for v in sorted(row, key=lambda i:i.box.x)), row)
             for row in lines]
    first = [(int(m[1]), row) for text, row in texts
             if (m := re.fullmatch(r"快速过关([1-5])次即可完成委托[。！!]?", text))]
    second = [(int(m[1]), row) for text, row in texts
              if (m := re.fullmatch(r"是否只快速完成([1-5])次[?？]?", text))]
    confirms = [i for i in values if _normalize_text(i.text) == "确定"]
    cancels = [i for i in values if _normalize_text(i.text) == "取消"]
    if len(first) != 1 or len(second) != 1 or len(confirms) != 1 or len(cancels) != 1:
        return None
    count = first[0][0]
    ok, cancel = confirms[0].box, cancels[0].box
    if (count != second[0][0] or count > available or cancel.right >= ok.x
            or abs(cancel.center[1] - ok.center[1]) > max(cancel.height, ok.height)
            or max(v.box.bottom for v in first[0][1] + second[0][1]) >= min(ok.y, cancel.y)):
        return None
    return count, _inset(ok)


def completed_commission_detail(items, frame):
    h, w = frame.shape[:2]
    roi = _scaled_roi((100, 350, 700, 950), w, h)
    return any(i.score >= .85 and _normalize_text(i.text) == "委托已完成"
               and _box_in_roi(i.box, roi) for i in items)


def _inset(box):
    margin = max(1, min(box.width, box.height) // 6)
    return Rect(box.x + margin, box.y + margin, box.width - 2*margin, box.height - 2*margin)


def _stable_commission_click(session, selector, *, deadline=None, position_tolerance=0):
    """Stable pair followed by a fresh recheck after the unchanged random delay."""
    from qjnn_common_popup import detect_common_safe_popup, handle_transient_if_present, GlobalTransientResult
    from qjnn_home_random_visit import home_random_visit_loading_classifier
    capture = getattr(session, "capture_confirmation_frame", session.capture_frame)
    def observe():
        if deadline is not None and session.monotonic() >= deadline:
            return None
        frame = capture()
        items = session.recognize_text(frame)
        if detect_common_safe_popup(frame, items).matched:
            transient, frame, items = handle_transient_if_present(session, frame, items)
            if transient is not GlobalTransientResult.HANDLED_POPUP:
                return None
        if (deadline is not None and session.monotonic() >= deadline
                or home_random_visit_loading_classifier(items, frame)):
            return None
        result = selector(items, frame)
        if result is not None:
            box = result[0]
            h,w = frame.shape[:2]
            if not (0 <= box.x < box.right <= w and 0 <= box.y < box.bottom <= h):
                return None
        return result
    def same(a, b):
        if a is None or b is None or a[1] != b[1]:
            return False
        x, y = a[0], b[0]
        tolerance = max(1, min(x.width, x.height, y.width, y.height) * .05)
        # Map labels have measured 2px OCR edge jitter at 720p. Only that
        # caller supplies a small screen-scaled position allowance; button
        # dimensions, semantic identity and all other callers stay strict.
        position_limit = max(tolerance, position_tolerance)
        return (min(x.width, x.height, y.width, y.height) > 0
                and abs(x.x-y.x) <= position_limit and abs(x.y-y.y) <= position_limit
                and abs(x.width-y.width) <= tolerance and abs(x.height-y.height) <= tolerance
                and x.x < y.right and y.x < x.right and x.y < y.bottom and y.y < x.bottom)
    first = observe()
    if first is None:
        return False
    session.sleep(ALLIANCE_SETTLEMENT_POLL_SECONDS)
    second = observe()
    if not same(first, second):
        return False
    def confirm():
        fresh = observe()
        return fresh[0] if same(second, fresh) else None
    return alliance_business_click(session, *second[0].center, confirm=confirm)


def _outside_target(items, frame, point, predicate):
    if not predicate(items, frame):
        return None
    h, w = frame.shape[:2]
    x, y = map_alliance_reference_point(point, w, h)
    return Rect(x-2, y-2, 4, 4), "confirmed_panel_close"


def search_active_commission(session, initial, remaining, *, log):
    """Eight bounded map-only pans. Never switch chapters or reset the map."""
    current = initial
    for index in range(len(COMMISSION_SEARCH_SWIPES) + 1):
        frame, items, _ = current
        if detect_commission_map(items, frame) != remaining:
            return None, "map_search_state_changed"
        target = find_in_progress_box(items, frame)
        if target is not None:
            return (frame, items, target), "active"
        if index == len(COMMISSION_SEARCH_SWIPES):
            return None, "no_in_progress"
        swipe = getattr(session, "swipe", None)
        if swipe is None:
            return None, "map_swipe_unavailable"
        # A second map observation authorizes navigation only, never a node tap.
        session.sleep(ALLIANCE_OCR_RETRY_WAIT_SECONDS)
        fresh = session.capture_frame()
        values = session.recognize_text(fresh)
        from qjnn_home_random_visit import home_random_visit_loading_classifier
        from qjnn_common_popup import detect_common_safe_popup
        if (home_random_visit_loading_classifier(values,fresh)
                or detect_common_safe_popup(fresh,values).matched
                or detect_commission_map(values, fresh) != remaining):
            return None, "map_search_state_changed"
        if find_in_progress_box(values, fresh) is not None:
            current = fresh, values, remaining
            continue
        h, w = fresh.shape[:2]
        x1,y1,x2,y2 = COMMISSION_SEARCH_SWIPES[index]
        a,b = map_alliance_reference_point((x1,y1),w,h)
        c,d = map_alliance_reference_point((x2,y2),w,h)
        log(f"[AllianceCommission] map search swipe={index+1}/{len(COMMISSION_SEARCH_SWIPES)}")
        if swipe(a,b,c,d,400) is False:
            return None, "map_swipe_failed"
        session.sleep(ALLIANCE_PAGE_WAIT_SECONDS)
        current = _retry_snapshot(session, lambda i,f: detect_commission_map(i,f))
        if current is None:
            return None, "map_search_state_changed"
    return None, "no_in_progress"


def _retry_snapshot(
    session: AllianceSession,
    selector: Callable[[list[OcrItem], object], object | None],
) -> tuple[object, list[OcrItem], object] | None:
    return poll_target_with_common_popups(
        session,
        selector,
        timeout=(ALLIANCE_OCR_ATTEMPTS - 1) * ALLIANCE_OCR_RETRY_WAIT_SECONDS,
        interval=ALLIANCE_OCR_RETRY_WAIT_SECONDS,
    )


def _poll_snapshot(
    session: AllianceSession,
    selector: Callable[[list[OcrItem], object], object | None],
    *,
    timeout: float,
    interval: float,
) -> tuple[object, list[OcrItem], object] | None:
    """Poll a read-only screen predicate for a finite amount of time."""
    if timeout <= 0 or interval <= 0:
        raise ValueError("poll timeout and interval must be positive")
    return poll_target_with_common_popups(
        session,
        selector,
        timeout=timeout,
        interval=interval,
    )


def wait_for_commission_settlement(
    session: AllianceSession,
    *,
    log: Callable[[str], None] = print,
    available_count=None,
    confirmed_runs=None,
) -> tuple[object, list[OcrItem], Rect] | None:
    """Wait for the stable settlement title without clicking during animation."""
    log(
        "[QJNN_AllianceCommissionWaitSettlement] "
        f"polling for 过关完成 (timeout={ALLIANCE_SETTLEMENT_TIMEOUT_SECONDS:.0f}s)"
    )
    deadline = session.monotonic() + ALLIANCE_SETTLEMENT_TIMEOUT_SECONDS
    def result_state(items, frame):
        if special_confirmation_visible(items):
            return "short_confirmation" if available_count is not None else None
        return find_text_box(items, "过关完成")
    result = _poll_snapshot(
        session,
        result_state,
        timeout=ALLIANCE_SETTLEMENT_TIMEOUT_SECONDS,
        interval=ALLIANCE_SETTLEMENT_POLL_SECONDS,
    )
    if result is not None and result[2] == "short_confirmation":
        parsed = parse_short_completion(result[1], result[0], available_count)
        if parsed is None:
            log("[AllianceCommission] short completion counts/buttons unconfirmed; SafeStop")
            return None
        count, _ = parsed
        def confirm(items, frame):
            current = parse_short_completion(items, frame, available_count)
            return (current[1], count) if current is not None and current[0] == count else None
        if not _stable_commission_click(session, confirm, deadline=deadline):
            return None
        if confirmed_runs is not None:
            confirmed_runs(count)
        log(f"[AllianceCommission] confirmed short completion once: runs={count}")
        remaining_time = deadline - session.monotonic()
        if remaining_time <= 0:
            return None
        # The same confirmation may linger during settlement. Never submit it again.
        result = _poll_snapshot(session, lambda i,f: None if special_confirmation_visible(i)
                                else find_text_box(i, "过关完成"), timeout=remaining_time,
                                interval=ALLIANCE_SETTLEMENT_POLL_SECONDS)
    if result is None:
        log(
            "[QJNN_AllianceCommissionWaitSettlement] "
            "timeout; no click was performed while the state was unknown"
        )
    return result


def _post_settlement_state(items: list[OcrItem], frame) -> str | None:
    # If the first close click has not taken effect yet, keep waiting. Treating
    # the still-visible settlement as a second popup would cause a premature tap.
    if find_text_box(items, "过关完成") is not None:
        return None
    if detail_popup_visible(items):
        return "popup"
    if parse_remaining_count(items, frame) is not None:
        return "map"
    return None


def _commission_map_state(items: list[OcrItem], frame) -> str | None:
    if find_text_box(items, "过关完成") is not None or detail_popup_visible(items):
        return None
    return "map" if parse_remaining_count(items, frame) is not None else None


def run_alliance_commission_settlement_flow(
    session: AllianceSession,
    *,
    log: Callable[[str], None] = print,
    capture: Callable[[str, object], bool] | None = None,
    available_count=None,
    confirmed_runs=None,
) -> str | None:
    """Close a ready settlement and its optional second layer, ending on map."""
    passed = wait_for_commission_settlement(session, log=log, available_count=available_count,
                                           confirmed_runs=confirmed_runs)
    if passed is None:
        return None

    settlement_frame, _, _ = passed
    log("已识别过关完成")
    if capture is not None:
        capture("QJNN_AllianceCommissionSettlement", settlement_frame)
    if not _stable_commission_click(session, lambda i,f: _outside_target(
            i,f,SETTLEMENT_OUTSIDE_FIRST_POINT,lambda i,f:find_text_box(i,"过关完成") is not None)):
        return None

    layer = _poll_snapshot(
        session,
        _post_settlement_state,
        timeout=ALLIANCE_POST_SETTLEMENT_TIMEOUT_SECONDS,
        interval=ALLIANCE_SETTLEMENT_POLL_SECONDS,
    )
    if layer is None:
        log("关闭结算弹窗后页面状态超时；未点击未知页面")
        return None

    layer_frame, _, layer_state = layer
    if layer_state == "map":
        return "map"

    if capture is not None:
        capture("QJNN_AllianceCommissionPostSettlementPopup", layer_frame)
    if not _stable_commission_click(session, lambda i,f: _outside_target(
            i,f,SETTLEMENT_OUTSIDE_SECOND_POINT,lambda i,f:_post_settlement_state(i,f)=="popup")):
        return None

    confirmed_map = _poll_snapshot(
        session,
        _commission_map_state,
        timeout=ALLIANCE_POST_SETTLEMENT_TIMEOUT_SECONDS,
        interval=ALLIANCE_SETTLEMENT_POLL_SECONDS,
    )
    if confirmed_map is None:
        log("关闭第二层结算弹窗后未在时限内确认委托地图")
        return None
    return "map"


def _click_reference_point(session: AllianceSession, frame, point: tuple[int, int]) -> None:
    height, width = frame.shape[:2]
    alliance_business_click(session, *map_alliance_reference_point(point, width, height))


def exit_commission_map(
    session: AllianceSession,
    *,
    required_remaining_count: int | None = None,
    log: Callable[[str], None],
) -> bool:
    frame = None
    for attempt in range(ALLIANCE_OCR_ATTEMPTS):
        candidate = session.capture_frame()
        candidate_items = session.recognize_text(candidate)
        remaining_count = parse_remaining_count(candidate_items, candidate)
        is_commission_map = remaining_count is not None and not detail_popup_visible(
            candidate_items
        )
        if required_remaining_count is not None:
            is_commission_map = (
                is_commission_map and remaining_count == required_remaining_count
            )
        score = session.back_arrow_score(candidate)
        if is_commission_map and score >= COMMISSION_BACK_THRESHOLD:
            frame = candidate
            break
        if attempt + 1 < ALLIANCE_OCR_ATTEMPTS:
            session.sleep(ALLIANCE_OCR_RETRY_WAIT_SECONDS)
    if frame is None:
        log("委托地图返回箭头识别失败")
        return False

    _click_reference_point(session, frame, COMMISSION_BACK_CLICK_POINT)
    session.sleep(ALLIANCE_PAGE_WAIT_SECONDS)
    home = _retry_snapshot(session, find_alliance_home)
    if home is None:
        log("返回后未确认搭配师联盟首页")
        return False
    log("已返回搭配师联盟首页")
    return True


def run_alliance_commission_map(
    session: AllianceSession,
    *,
    log: Callable[[str], None] = print,
    capture: Callable[[str, object], bool] | None = None,
) -> AllianceCommissionResult:
    last_remaining: int | None = None
    expected_remaining = None
    def map_or_locked(items, frame):
        remaining = parse_remaining_count(items, frame)
        if remaining is not None:
            return remaining
        if find_alliance_home(items, frame) is not None:
            h, w = frame.shape[:2]
            roi = _scaled_roi((150, 740, 620, 130), w, h)
            if any(i.score >= .7 and roi.x <= i.box.center[0] <= roi.right
                   and roi.y <= i.box.center[1] <= roi.bottom
                   and re.fullmatch(r"联盟等级达到\d+级开放本功能", re.sub(r"\s+", "", i.text)) for i in items):
                return "feature_locked"
        return None
    for loop_index in range(1, ALLIANCE_MAX_COMMISSION_LOOPS + 2):
        map_state = _retry_snapshot(session, map_or_locked)
        if map_state is None:
            log("委托地图未识别到今日剩余次数")
            return AllianceCommissionResult(False, last_remaining, loop_index - 1, "remaining_not_found")
        map_frame, map_items, remaining_count = map_state
        if remaining_count == "feature_locked":
            log("[AllianceDaily] commission locked by alliance level; continue library")
            return AllianceCommissionResult(True, None, 0, "feature_locked")
        last_remaining = int(remaining_count)
        if expected_remaining is not None and last_remaining != expected_remaining:
            log("[AllianceCommission] remaining count changed unexpectedly; no repeat clear")
            return AllianceCommissionResult(False,last_remaining,loop_index-1,"remaining_mismatch")
        log(f"今日剩余次数：{last_remaining}/5")

        if last_remaining == 0:
            if detail_popup_visible(map_items):
                log("检测到 0/5 详情页：不点击灰色完成1次，先关闭弹窗")
                if not _stable_commission_click(session,lambda i,f:_outside_target(
                        i,f,SETTLEMENT_OUTSIDE_SECOND_POINT,
                        lambda i,f:parse_remaining_count(i,f)==0 and detail_popup_visible(i)
                        and not special_confirmation_visible(i))):
                    return AllianceCommissionResult(False,0,loop_index-1,"zero_detail_not_closed")
                session.sleep(ALLIANCE_PAGE_WAIT_SECONDS)
                confirmed_map = _retry_snapshot(
                    session,
                    lambda items, frame: (
                        0
                        if parse_remaining_count(items, frame) == 0
                        and not detail_popup_visible(items)
                        else None
                    ),
                )
                if confirmed_map is None:
                    log("0/5 详情弹窗关闭后未确认回到委托地图")
                    return AllianceCommissionResult(False, 0, loop_index - 1, "zero_detail_not_closed")
            if not exit_commission_map(
                session,
                required_remaining_count=0,
                log=log,
            ):
                return AllianceCommissionResult(False, 0, loop_index - 1, "return_home_failed")
            return AllianceCommissionResult(True, 0, loop_index - 1, "completed")

        if loop_index > ALLIANCE_MAX_COMMISSION_LOOPS:
            break
        # A completed panel can still expose stale quick-clear controls behind it.
        if completed_commission_detail(map_items, map_frame):
            if not _stable_commission_click(session,lambda i,f:_outside_target(
                    i,f,SETTLEMENT_OUTSIDE_SECOND_POINT,completed_commission_detail)):
                return AllianceCommissionResult(False,last_remaining,loop_index-1,"completed_panel_not_closed")
            map_state = _retry_snapshot(session, detect_commission_map)
            if map_state is None or map_state[2] != last_remaining:
                return AllianceCommissionResult(False,last_remaining,loop_index-1,"completed_panel_not_closed")
        selected, reason = search_active_commission(session, map_state, last_remaining, log=log)
        if selected is None:
            if reason == "no_in_progress":
                log("[AllianceCommission] no active stage after bounded search; no chapter reset")
                exited = exit_commission_map(session, required_remaining_count=last_remaining, log=log)
                return AllianceCommissionResult(exited,last_remaining,loop_index-1,
                                                "no_in_progress" if exited else "return_home_failed")
            return AllianceCommissionResult(False,last_remaining,loop_index-1,reason)
        map_frame, _, in_progress = selected

        if capture is not None:
            capture("QJNN_AllianceCommissionMapActive", map_frame)
        log("点击进行中关卡节点")
        def active_target(items, frame):
            if detect_commission_map(items,frame) != last_remaining:
                return None
            box = find_in_progress_box(items,frame)
            if box is None:
                return None
            h,w = frame.shape[:2]
            x,_ = box.center
            y = box.bottom + round(COMMISSION_NODE_Y_OFFSET * min(w,h)/900)
            area = _scaled_roi(COMMISSION_IN_PROGRESS_ROI,w,h)
            radius = max(2,round(12*w/900))
            if not (area.x+radius <= x <= area.right-radius and area.y+radius <= y <= area.bottom-radius):
                return None
            return Rect(x-radius,y-radius,2*radius,2*radius),last_remaining
        # A 5-reference-pixel allowance (4px at 720p) fits well inside the
        # 12-reference-pixel node safety radius. The final click still uses
        # the newly recognized position, never the original search frame.
        node_tolerance = max(1, round(5 * min(map_frame.shape[:2]) / 900))
        if not _stable_commission_click(session, active_target, position_tolerance=node_tolerance):
            log("[AllianceCommission] active_stage_changed: map/count/target stability check rejected; no click")
            return AllianceCommissionResult(False,last_remaining,loop_index-1,"active_stage_changed")
        session.sleep(ALLIANCE_PAGE_WAIT_SECONDS)

        detail = _retry_snapshot(
            session,
            lambda items, frame: None if completed_commission_detail(items,frame)
                else find_complete_box(items, frame, last_remaining),
        )
        if detail is None:
            log(f"关卡详情未识别到完成{last_remaining}次；不点击其他完成按钮")
            return AllianceCommissionResult(False, last_remaining, loop_index - 1, "complete_button_not_found")
        detail_frame, _, complete_box = detail
        if last_remaining == 1:
            if complete_one_is_gray(detail_frame, complete_box):
                log("完成1次按钮为灰色不可用状态；拒绝点击")
                return AllianceCommissionResult(
                    False,
                    last_remaining,
                    loop_index - 1,
                    "complete_one_disabled",
                )
            log("已确认地图剩余 1/5，点击完成1次")
        else:
            log(f"点击完成{last_remaining}次")
        if capture is not None:
            capture("QJNN_AllianceCommissionDetailActive", detail_frame)
        def quick_target(items, frame):
            if (completed_commission_detail(items,frame) or special_confirmation_visible(items)
                    or parse_remaining_count(items,frame) != last_remaining):
                return None
            box = find_complete_box(items,frame,last_remaining)
            if box is None or (last_remaining==1 and complete_one_is_gray(frame,box)):
                return None
            return _inset(box),last_remaining
        if not _stable_commission_click(session, quick_target):
            return AllianceCommissionResult(False,last_remaining,loop_index-1,"quick_target_changed")
        spent = [last_remaining]
        settlement_state = run_alliance_commission_settlement_flow(
            session,
            log=log,
            capture=capture,
            available_count=last_remaining,
            confirmed_runs=lambda n:spent.__setitem__(0,n),
        )
        if settlement_state != "map":
            log("结算流程未能安全返回委托地图")
            return AllianceCommissionResult(False, last_remaining, loop_index, "settlement_not_found")
        expected_remaining = last_remaining - spent[0]

    log("委托任务失败：超过最大执行循环次数")
    return AllianceCommissionResult(False, last_remaining, ALLIANCE_MAX_COMMISSION_LOOPS, "max_loops")


def run_alliance_commission_from_home(
    session: AllianceSession,
    *,
    log: Callable[[str], None] = print,
    capture: Callable[[str, object], bool] | None = None,
) -> AllianceCommissionResult:
    home = _poll_snapshot(session, find_alliance_home, timeout=15.0,
                          interval=ALLIANCE_OCR_RETRY_WAIT_SECONDS)
    if home is None:
        log("未确认当前为搭配师联盟首页")
        return AllianceCommissionResult(False, None, 0, "alliance_home_not_found")
    _, _, (_, commission_box) = home
    log("当前页面：搭配师联盟首页；进入委托")
    alliance_business_click(session, *commission_box.center)
    session.sleep(ALLIANCE_PAGE_WAIT_SECONDS)
    return run_alliance_commission_map(session, log=log, capture=capture)


def run_alliance_commission_resume(
    session: AllianceSession,
    *,
    log: Callable[[str], None] = print,
    capture: Callable[[str, object], bool] | None = None,
) -> AllianceCommissionResult:
    map_state = _retry_snapshot(session, detect_commission_map)
    if map_state is None:
        log("无法确认当前为可恢复的委托章节页；零点击停止")
        return AllianceCommissionResult(False, None, 0, "commission_resume_unknown")
    _, _, remaining = map_state
    log(f"恢复入口：已在委托章节页，今日剩余次数 {remaining}/5")
    return run_alliance_commission_map(session, log=log, capture=capture)


def run_alliance_commission(
    session: AllianceSession,
    *,
    log: Callable[[str], None] = print,
    capture: Callable[[str, object], bool] | None = None,
) -> AllianceCommissionResult:
    entry = _retry_snapshot(session, find_alliance_entry_box)
    if entry is None:
        log("P3 未识别到搭配师联盟")
        return AllianceCommissionResult(False, None, 0, "alliance_entry_not_found")
    entry_frame, _, entry_box = entry
    alliance_business_click(session, *entry_box.center)
    session.sleep(ALLIANCE_PAGE_WAIT_SECONDS)
    return run_alliance_commission_from_home(
        session,
        log=log,
        capture=capture,
    )


def commission_back_arrow_score(frame) -> float:
    import cv2
    import numpy as np

    data = np.fromfile(COMMISSION_BACK_TEMPLATE, dtype=np.uint8)
    template = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if template is None:
        raise RuntimeError(f"无法读取委托地图返回箭头模板：{COMMISSION_BACK_TEMPLATE}")

    frame_height, frame_width = frame.shape[:2]
    roi = _scaled_roi(COMMISSION_BACK_ROI, frame_width, frame_height)
    search = frame[roi.y : roi.bottom, roi.x : roi.right]
    scale = min(frame_height, frame_width) / 900
    template = cv2.resize(
        template,
        (max(1, round(template.shape[1] * scale)), max(1, round(template.shape[0] * scale))),
        interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC,
    )
    if template.shape[1] > search.shape[1] or template.shape[0] > search.shape[0]:
        return 0.0
    return float(cv2.minMaxLoc(cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED))[1])


def _make_runtime_session(context):
    controller = context.tasker.controller

    class RuntimeSession:
        @staticmethod
        def capture_frame():
            job = controller.post_screencap()
            job.wait()
            if hasattr(job, "succeeded") and not job.succeeded:
                raise RuntimeError("联盟任务截图失败")
            cached = getattr(controller, "cached_image", None)
            frame = cached() if callable(cached) else cached
            if frame is None or getattr(frame, "size", 0) == 0:
                raise RuntimeError("联盟任务截图为空")
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
                    values = list(box)
                    rect = Rect(*(int(value) for value in values[:4]))
                converted.append(
                    OcrItem(
                        text=str(result.text),
                        box=rect,
                        score=float(getattr(result, "score", 0.0)),
                    )
                )
            return converted

        @staticmethod
        def click(x: int, y: int) -> None:
            job = controller.post_click(x, y)
            job.wait()
            if hasattr(job, "succeeded") and not job.succeeded:
                raise RuntimeError(f"联盟任务点击失败：({x}, {y})")

        @staticmethod
        def swipe(x1, y1, x2, y2, duration):
            job = controller.post_swipe(x1,y1,x2,y2,duration).wait()
            if hasattr(job,"succeeded") and not job.succeeded:
                raise RuntimeError("联盟委托地图滑动失败")
            return True

        sleep = staticmethod(time.sleep)
        monotonic = staticmethod(time.monotonic)
        back_arrow_score = staticmethod(commission_back_arrow_score)

    return guard_runtime_session(context, RuntimeSession())


class AllianceCommissionAction(CustomAction):
    def __init__(self, alliance_capture: AllianceCaptureManager | None = None) -> None:
        super().__init__()
        self._alliance_capture = alliance_capture

    def run(self, context, argv) -> bool:
        try:
            capture = (
                self._alliance_capture.capture_once
                if self._alliance_capture is not None
                else None
            )
            return run_alliance_commission(
                _make_runtime_session(context), capture=capture
            ).success
        except Exception as exc:
            print(f"搭配师联盟委托任务失败：{exc}")
            return False


class AllianceCommissionSettlementDebugAction(CustomAction):
    """Thin entry that resumes only the shared settlement state machine."""

    def __init__(self, alliance_capture: AllianceCaptureManager | None = None) -> None:
        super().__init__()
        self._alliance_capture = alliance_capture

    def run(self, context, argv) -> bool:
        try:
            capture = (
                self._alliance_capture.capture_once
                if self._alliance_capture is not None
                else None
            )
            return run_alliance_commission_settlement_flow(
                _make_runtime_session(context), capture=capture
            ) == "map"
        except Exception as exc:
            print(f"联盟委托结算调试失败：{exc}")
            return False


class AllianceCommissionMapExitAction(CustomAction):
    """Exit only a confirmed 0/5 commission map using the shared safe helper."""

    def __init__(self) -> None:
        super().__init__()

    def run(self, context, argv) -> bool:
        try:
            return exit_commission_map(
                _make_runtime_session(context),
                required_remaining_count=0,
                log=print,
            )
        except Exception as exc:
            print(f"联盟委托地图安全返回失败：{exc}")
            return False
