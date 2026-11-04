from __future__ import annotations

import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Protocol

from qjnn_alliance_click import alliance_business_click
from qjnn_alliance_capture import AllianceCaptureManager
from qjnn_common_popup import (
    GlobalTransientResult,
    guard_runtime_session,
    handle_transient_if_present,
)

from qjnn_alliance_commission import (
    OcrItem,
    Rect,
    find_alliance_home,
    find_text_box,
    map_alliance_reference_point,
)

try:
    from maa.custom_action import CustomAction
except ImportError:  # MaaFramework is optional for offline OCR/state-machine tests.
    CustomAction = object


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOP_BACK_TEMPLATE = (
    PROJECT_ROOT / "assets" / "resource" / "image" / "alliance" / "top_back_arrow.png"
)

LIBRARY_TITLE_ROI = (240, 70, 420, 130)
LIBRARY_ENTRY_ROI = (600, 1300, 250, 180)
LIBRARY_ORGANIZE_BUTTON_ROI = (120, 500, 300, 120)
LIBRARY_FREE_STATUS_ROI = (130, 620, 250, 80)
LIBRARY_REWARD_TITLE_ROI = (170, 220, 560, 520)
LIBRARY_TOP_BACK_ROI = (0, 0, 150, 150)

LIBRARY_TOP_BACK_CLICK_POINT = (70, 65)
LIBRARY_REWARD_DISMISS_POINT = (450, 1000)
LIBRARY_TOP_BACK_THRESHOLD = 0.80
LIBRARY_OCR_ATTEMPTS = 3
LIBRARY_OCR_RETRY_WAIT_SECONDS = 0.5
LIBRARY_PAGE_WAIT_SECONDS = 0.8
LIBRARY_REWARD_WAIT_SECONDS = 1.0


@dataclass(frozen=True)
class LibraryResult:
    success: bool
    free_claimed: bool
    reason: str


class LibrarySession(Protocol):
    def capture_frame(self): ...

    def recognize_text(self, frame) -> list[OcrItem]: ...

    def click(self, x: int, y: int) -> None: ...

    def sleep(self, seconds: float) -> None: ...

    def back_arrow_score(self, frame) -> float: ...


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


def _in_roi(box: Rect, roi: Rect) -> bool:
    center_x, center_y = box.center
    return roi.x <= center_x <= roi.right and roi.y <= center_y <= roi.bottom


def find_library_page(items: list[OcrItem], frame) -> Rect | None:
    height, width = frame.shape[:2]
    return find_text_box(
        items,
        "图书馆",
        roi=_scaled_roi(LIBRARY_TITLE_ROI, width, height),
    )


def find_library_entry(items: list[OcrItem], frame) -> Rect | None:
    height, width = frame.shape[:2]
    return find_text_box(
        items,
        "图书馆",
        roi=_scaled_roi(LIBRARY_ENTRY_ROI, width, height),
    )


def find_organize_button(items: list[OcrItem], frame) -> Rect | None:
    height, width = frame.shape[:2]
    return find_text_box(
        items,
        "整理资料",
        roi=_scaled_roi(LIBRARY_ORGANIZE_BUTTON_ROI, width, height),
    )


def find_free_complete(items: list[OcrItem], frame) -> Rect | None:
    """Find 免费完成 only in its tight status ROI; cost text is never a positive signal."""
    height, width = frame.shape[:2]
    roi = _scaled_roi(LIBRARY_FREE_STATUS_ROI, width, height)
    exact = find_text_box(items, "免费完成", roi=roi)
    if exact is not None:
        return exact

    target = "免费完成"
    candidates = [item for item in items if _in_roi(item.box, roi)]
    for candidate in candidates:
        normalized = "".join(candidate.text.split())
        if len(normalized) >= 4 and SequenceMatcher(None, target, normalized).ratio() >= 0.72:
            return candidate.box
    return None


def find_reward_popup(items: list[OcrItem], frame) -> Rect | None:
    height, width = frame.shape[:2]
    return find_text_box(
        items,
        "恭喜获得",
        roi=_scaled_roi(LIBRARY_REWARD_TITLE_ROI, width, height),
    )


def _retry_snapshot(
    session: LibrarySession,
    selector: Callable[[list[OcrItem], object], object | None],
) -> tuple[object, list[OcrItem], object] | None:
    for attempt in range(LIBRARY_OCR_ATTEMPTS):
        frame = session.capture_frame()
        items = session.recognize_text(frame)
        value = selector(items, frame)
        if value is not None:
            return frame, items, value
        transient, fresh_frame, fresh_items = handle_transient_if_present(
            session,
            frame,
            items,
        )
        if transient is GlobalTransientResult.ERROR:
            return None
        if transient is GlobalTransientResult.HANDLED_POPUP:
            value = selector(list(fresh_items), fresh_frame)
            if value is not None:
                return fresh_frame, list(fresh_items), value
            continue
        if attempt + 1 < LIBRARY_OCR_ATTEMPTS:
            session.sleep(LIBRARY_OCR_RETRY_WAIT_SECONDS)
    return None


def _click_reference_point(session: LibrarySession, frame, point: tuple[int, int]) -> None:
    height, width = frame.shape[:2]
    alliance_business_click(session, *map_alliance_reference_point(point, width, height))


def _detect_start_page(items: list[OcrItem], frame):
    library = find_library_page(items, frame)
    if library is not None:
        return "library", library
    alliance = find_alliance_home(items, frame)
    entry = find_library_entry(items, frame)
    if alliance is not None and entry is not None:
        return "alliance_home", entry
    return None


def _return_to_alliance_home(
    session: LibrarySession,
    *,
    log: Callable[[str], None],
) -> bool:
    frame = None
    for attempt in range(LIBRARY_OCR_ATTEMPTS):
        candidate = session.capture_frame()
        candidate_items = session.recognize_text(candidate)
        is_library = find_library_page(candidate_items, candidate) is not None
        has_back_arrow = (
            session.back_arrow_score(candidate) >= LIBRARY_TOP_BACK_THRESHOLD
        )
        if is_library and has_back_arrow:
            frame = candidate
            break
        if attempt + 1 < LIBRARY_OCR_ATTEMPTS:
            session.sleep(LIBRARY_OCR_RETRY_WAIT_SECONDS)
    if frame is None:
        log("图书馆返回箭头识别失败")
        return False

    _click_reference_point(session, frame, LIBRARY_TOP_BACK_CLICK_POINT)
    session.sleep(LIBRARY_PAGE_WAIT_SECONDS)
    if _retry_snapshot(session, find_alliance_home) is None:
        log("返回后未确认搭配师联盟首页")
        return False
    log("已返回搭配师联盟首页")
    return True


def run_library(
    session: LibrarySession,
    *,
    log: Callable[[str], None] = print,
    capture: Callable[[str, object], bool] | None = None,
    organize_three: bool = False,
) -> LibraryResult:
    if type(organize_three) is not bool:
        raise ValueError("整理资料3次设置必须为布尔值")
    start = _retry_snapshot(session, _detect_start_page)
    if start is None:
        log("无法确认当前处于搭配师联盟首页或图书馆")
        return LibraryResult(False, False, "start_page_unknown")
    frame, items, (page, target_box) = start

    if page == "alliance_home":
        log("当前页面：搭配师联盟首页")
        alliance_business_click(session, *target_box.center)
        session.sleep(LIBRARY_PAGE_WAIT_SECONDS)
        entered = _retry_snapshot(session, find_library_page)
        if entered is None:
            log("点击图书馆入口后未确认进入图书馆")
            return LibraryResult(False, False, "library_not_entered")
        frame, items, _ = entered
    else:
        log("当前页面：图书馆")

    if organize_three:
        from qjnn_library_three import run_library_three, RECORD
        return run_library_three(session, record_path=getattr(session,'library_record_path',RECORD), log=log, capture=capture)
    free_status = find_free_complete(items, frame)
    free_claimed = False
    if free_status is not None:
        organize = find_organize_button(items, frame)
        if organize is None:
            refreshed = _retry_snapshot(session, find_organize_button)
            if refreshed is None:
                log("已识别免费完成，但未找到整理资料按钮；拒绝盲点")
                return LibraryResult(False, False, "organize_button_not_found")
            frame, _, organize = refreshed

        if capture is not None:
            capture("QJNN_AllianceLibraryFree", frame)
        log("已确认免费完成，点击整理资料一次")
        alliance_business_click(session, *organize.center)
        free_claimed = True
        session.sleep(LIBRARY_REWARD_WAIT_SECONDS)
        reward = _retry_snapshot(session, find_reward_popup)
        if reward is None:
            log("点击整理资料后未识别到恭喜获得；不重复点击整理资料")
            return LibraryResult(False, True, "reward_not_found")
        reward_frame, _, _ = reward
        log("已识别恭喜获得")
        if capture is not None:
            capture("QJNN_AllianceLibraryReward", reward_frame)
        _click_reference_point(session, reward_frame, LIBRARY_REWARD_DISMISS_POINT)
        session.sleep(LIBRARY_PAGE_WAIT_SECONDS)
        library_after_reward = _retry_snapshot(
            session,
            lambda current_items, current_frame: (
                find_library_page(current_items, current_frame)
                if find_reward_popup(current_items, current_frame) is None
                else None
            ),
        )
        if library_after_reward is None:
            log("关闭奖励后未确认回到图书馆")
            return LibraryResult(False, True, "library_after_reward_not_found")
    else:
        log("未检测到免费完成：不点击整理资料")

    if not _return_to_alliance_home(session, log=log):
        return LibraryResult(False, free_claimed, "return_home_failed")
    return LibraryResult(True, free_claimed, "completed")


def top_back_arrow_score(frame) -> float:
    import cv2
    import numpy as np

    data = np.fromfile(TOP_BACK_TEMPLATE, dtype=np.uint8)
    template = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if template is None:
        raise RuntimeError(f"无法读取联盟顶部返回箭头模板：{TOP_BACK_TEMPLATE}")

    frame_height, frame_width = frame.shape[:2]
    roi = _scaled_roi(LIBRARY_TOP_BACK_ROI, frame_width, frame_height)
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


class LibraryAction(CustomAction):
    def __init__(self, alliance_capture: AllianceCaptureManager | None = None) -> None:
        super().__init__()
        self._alliance_capture = alliance_capture

    def run(self, context, argv) -> bool:
        controller = context.tasker.controller

        class RuntimeSession:
            @staticmethod
            def capture_frame():
                job = controller.post_screencap()
                job.wait()
                if hasattr(job, "succeeded") and not job.succeeded:
                    raise RuntimeError("图书馆任务截图失败")
                cached = getattr(controller, "cached_image", None)
                frame = cached() if callable(cached) else cached
                if frame is None or getattr(frame, "size", 0) == 0:
                    raise RuntimeError("图书馆任务截图为空")
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
                    raise RuntimeError(f"图书馆任务点击失败：({x}, {y})")

            sleep = staticmethod(time.sleep)
            monotonic = staticmethod(time.monotonic)
            back_arrow_score = staticmethod(top_back_arrow_score)

        try:
            capture = (
                self._alliance_capture.capture_once
                if self._alliance_capture is not None
                else None
            )
            return run_library(
                guard_runtime_session(context, RuntimeSession()),
                capture=capture,
            ).success
        except Exception as exc:
            print(f"搭配师联盟图书馆任务失败：{exc}")
            return False
