from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

from qjnn_home import HOME_PAGE_REFERENCE_SIZE, detect_home_page
from qjnn_alliance_commission import OcrItem, Rect
from qjnn_common_popup import GlobalTransientResult, handle_transient_if_present

try:
    from maa.custom_action import CustomAction
except ImportError:  # MaaFramework is optional for the offline state-machine test.
    CustomAction = object


HOME_LEFT_ARROW_REFERENCE_POINT = (35, 1100)
HOME_RIGHT_ARROW_REFERENCE_POINT = (865, 1100)
HOME_PAGE_TRANSITION_WAIT_SECONDS = 0.8
HOME_PAGE_UNKNOWN_RETRIES = 3
HOME_PAGE_UNKNOWN_RETRY_WAIT_SECONDS = 0.4
HOME_PAGE_MAX_STEPS = 2
HOME_PAGES = ("P1", "P2", "P3")


@dataclass(frozen=True)
class HomeNavigationResult:
    success: bool
    page: str
    clicks: int
    reason: str


def map_home_reference_point(
    point: tuple[int, int],
    frame_width: int,
    frame_height: int,
) -> tuple[int, int]:
    """Map a 900 x 1600 home coordinate to the current controller frame."""
    reference_width, reference_height = HOME_PAGE_REFERENCE_SIZE
    x, y = point
    return (
        round(x * frame_width / reference_width),
        round(y * frame_height / reference_height),
    )


def _recognize_home_with_retries(
    capture_frame: Callable[[], object],
    detect_page: Callable[[object], object],
    sleep: Callable[[float], None],
    *,
    unknown_retries: int,
    unknown_retry_wait: float,
) -> tuple[str, object | None]:
    for retry in range(unknown_retries + 1):
        frame = capture_frame()
        detection = detect_page(frame)
        page = str(getattr(detection, "page", "Unknown"))
        if page != "Unknown":
            return page, frame
        if retry < unknown_retries:
            sleep(unknown_retry_wait)
    return "Unknown", None


def navigate_home_page(
    target_page: str,
    capture_frame: Callable[[], object],
    click: Callable[[int, int], None],
    *,
    detect_page: Callable[[object], object] = detect_home_page,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
    transition_wait: float = HOME_PAGE_TRANSITION_WAIT_SECONDS,
    unknown_retries: int = HOME_PAGE_UNKNOWN_RETRIES,
    unknown_retry_wait: float = HOME_PAGE_UNKNOWN_RETRY_WAIT_SECONDS,
    max_steps: int = HOME_PAGE_MAX_STEPS,
) -> HomeNavigationResult:
    """Navigate between P1/P2/P3, recognizing again after every single click."""
    target_page = str(target_page).upper()
    if target_page not in HOME_PAGES:
        raise ValueError(f"无效主页目标：{target_page}")
    if unknown_retries < 0:
        raise ValueError("unknown_retries 不能为负数")
    if max_steps < 0:
        raise ValueError("max_steps 不能为负数")

    page, frame = _recognize_home_with_retries(
        capture_frame,
        detect_page,
        sleep,
        unknown_retries=unknown_retries,
        unknown_retry_wait=unknown_retry_wait,
    )
    if page == "Unknown" or frame is None:
        log("主页状态识别失败：Unknown")
        return HomeNavigationResult(False, "Unknown", 0, "unknown")
    if page not in HOME_PAGES:
        log(f"主页导航失败：意外页面 {page}")
        return HomeNavigationResult(False, page, 0, "unexpected_page")

    log(f"当前主页：{page}")
    clicks = 0
    while page != target_page:
        if clicks >= max_steps:
            log("主页导航失败：超过最大点击次数")
            return HomeNavigationResult(False, page, clicks, "max_clicks")

        current_index = HOME_PAGES.index(page)
        target_index = HOME_PAGES.index(target_page)
        direction = 1 if target_index > current_index else -1
        expected_page = HOME_PAGES[current_index + direction]
        arrow_point = (
            HOME_RIGHT_ARROW_REFERENCE_POINT
            if direction > 0
            else HOME_LEFT_ARROW_REFERENCE_POINT
        )
        frame_height, frame_width = frame.shape[:2]
        click_x, click_y = map_home_reference_point(
            arrow_point,
            frame_width,
            frame_height,
        )
        log("点击右箭头" if direction > 0 else "点击左箭头")
        click(click_x, click_y)
        clicks += 1
        sleep(transition_wait)

        next_page, next_frame = _recognize_home_with_retries(
            capture_frame,
            detect_page,
            sleep,
            unknown_retries=unknown_retries,
            unknown_retry_wait=unknown_retry_wait,
        )
        if next_page == "Unknown" or next_frame is None:
            log("主页状态识别失败：Unknown")
            return HomeNavigationResult(False, "Unknown", clicks, "unknown")
        log(f"切换后主页：{next_page}")

        if next_page != expected_page:
            log(f"主页导航失败：预期 {expected_page}，实际 {next_page}")
            return HomeNavigationResult(
                False,
                next_page,
                clicks,
                "unexpected_transition",
            )
        page, frame = next_page, next_frame

    log(f"主页导航完成：{target_page}")
    return HomeNavigationResult(True, target_page, clicks, "completed")


def navigate_home_to_p3(
    capture_frame: Callable[[], object],
    click: Callable[[int, int], None],
    **kwargs,
) -> HomeNavigationResult:
    """Backward-compatible thin wrapper around the shared navigator."""
    if "max_right_clicks" in kwargs:
        kwargs["max_steps"] = kwargs.pop("max_right_clicks")
    return navigate_home_page("P3", capture_frame, click, **kwargs)


class HomeNavigateAction(CustomAction):
    """Business-agnostic Maa wrapper for the shared home-page navigator."""

    def run(self, context, argv) -> bool:
        controller = context.tasker.controller

        def capture_frame():
            capture_job = controller.post_screencap()
            capture_job.wait()
            if hasattr(capture_job, "succeeded") and not capture_job.succeeded:
                raise RuntimeError("主页截图失败")
            cached_image = getattr(controller, "cached_image", None)
            frame = cached_image() if callable(cached_image) else cached_image
            if frame is None or getattr(frame, "size", 0) == 0:
                raise RuntimeError("主页截图为空")
            return frame

        def click(x: int, y: int) -> None:
            click_job = controller.post_click(x, y)
            click_job.wait()
            if hasattr(click_job, "succeeded") and not click_job.succeeded:
                raise RuntimeError("主页箭头点击失败")

        class TransientSession:
            sleep = staticmethod(time.sleep)
            monotonic = staticmethod(time.monotonic)

            @staticmethod
            def recognize_text(frame):
                detail = context.run_recognition("QJNN_AgentOcrBase", frame)
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
                    converted.append(
                        OcrItem(str(result.text), rect, float(getattr(result, "score", 0.0)))
                    )
                return converted

        transient_session = TransientSession()
        # Bind outside the class body: assigning the same name inside it
        # shadows the enclosing function and raises NameError at construction.
        transient_session.capture_frame = capture_frame
        transient_session.click = click

        def capture_after_business_miss():
            frame = capture_frame()
            if str(getattr(detect_home_page(frame), "page", "Unknown")) in HOME_PAGES:
                return frame
            items = transient_session.recognize_text(frame)
            result, fresh_frame, _ = handle_transient_if_present(
                transient_session,
                frame,
                items,
            )
            if result is GlobalTransientResult.ERROR:
                raise RuntimeError("主页识别失败且全局临时状态处理失败")
            if result is GlobalTransientResult.HANDLED_POPUP:
                return fresh_frame
            return frame

        try:
            param = json.loads(argv.custom_action_param or "{}")
            target_page = str(param.get("target_page", "P3")).upper()
            return navigate_home_page(
                target_page,
                capture_after_business_miss,
                click,
            ).success
        except Exception as exc:
            print(f"主页导航失败：{exc}")
            return False


# Python/API compatibility for existing imports; both names share one implementation.
HomeToP3Action = HomeNavigateAction
