from __future__ import annotations

import time
import json
from dataclasses import dataclass
from typing import Callable, Protocol


def _library_options(argv):
    params = json.loads(getattr(argv, "custom_action_param", None) or "{}")
    if params is None:params={}
    if not isinstance(params,dict):raise ValueError('联盟设置必须为对象')
    allow = params.get("library_three", False)
    if type(allow) is not bool:
        raise ValueError("整理资料3次设置必须为布尔值")
    return {"library_three": True} if allow else {}

from qjnn_alliance_click import alliance_business_click
from qjnn_alliance_capture import AllianceCaptureManager
from qjnn_alliance_commission import (
    ALLIANCE_OCR_ATTEMPTS,
    ALLIANCE_OCR_RETRY_WAIT_SECONDS,
    OcrItem,
    Rect,
    commission_back_arrow_score,
    detect_commission_map,
    find_alliance_home,
    run_alliance_commission,
    run_alliance_commission_from_home,
    run_alliance_commission_resume,
)
from qjnn_common_popup import (
    GlobalTransientResult,
    guard_runtime_session,
    handle_transient_if_present,
    detect_common_safe_popup,
)
from qjnn_home import detect_home_page
from qjnn_home_navigation import navigate_home_page
from qjnn_library import (
    LIBRARY_OCR_ATTEMPTS,
    LIBRARY_OCR_RETRY_WAIT_SECONDS,
    LIBRARY_PAGE_WAIT_SECONDS,
    LIBRARY_TOP_BACK_CLICK_POINT,
    LIBRARY_TOP_BACK_THRESHOLD,
    map_alliance_reference_point,
    find_library_page,
    run_library,
    top_back_arrow_score,
)

try:
    from maa.custom_action import CustomAction
except ImportError:  # MaaFramework is optional for the offline orchestration test.
    CustomAction = object


@dataclass(frozen=True)
class AllianceDailyExitResult:
    success: bool
    page: str
    reason: str


@dataclass(frozen=True)
class AllianceDailyResult:
    success: bool
    start_state: str
    reason: str


class AllianceDailyExitSession(Protocol):
    def capture_frame(self): ...

    def recognize_text(self, frame) -> list[OcrItem]: ...

    def click(self, x: int, y: int) -> None: ...

    def sleep(self, seconds: float) -> None: ...

    def back_arrow_score(self, frame) -> float: ...


class _BackArrowSessionView:
    def __init__(self, session, scorer: Callable[[object], float]) -> None:
        self._session = session
        self._scorer = scorer

    def back_arrow_score(self, frame) -> float:
        return self._scorer(frame)

    def __getattr__(self, name):
        return getattr(self._session, name)


def detect_alliance_daily_start(items: list[OcrItem], frame) -> str:
    if detect_commission_map(items, frame) is not None:
        return "COMMISSION_PAGE"
    if find_library_page(items, frame) is not None:
        return "LIBRARY_PAGE"
    if find_alliance_home(items, frame) is not None:
        return "ALLIANCE_HOME"
    if str(getattr(detect_home_page(frame), "page", "Unknown")) in {"P1", "P2", "P3"}:
        return "HOME"
    return "UNKNOWN"


def run_alliance_daily(
    session,
    *,
    log: Callable[[str], None] = print,
    capture: Callable[[str, object], bool] | None = None,
    capture_unknown: Callable[[str, object], bool] | None = None,
    start_detector: Callable[[list[OcrItem], object], str] = detect_alliance_daily_start,
    library_three: bool = False,
) -> AllianceDailyResult:
    start_state = "UNKNOWN"
    last_frame = None
    for attempt in range(ALLIANCE_OCR_ATTEMPTS):
        last_frame = session.capture_frame()
        items = session.recognize_text(last_frame)
        start_state = ("UNKNOWN" if detect_common_safe_popup(last_frame, items).matched
                       else start_detector(items, last_frame))
        if start_state != "UNKNOWN":
            break
        transient, fresh_frame, fresh_items = handle_transient_if_present(
            session,
            last_frame,
            items,
            capture=capture_unknown,
            log=log,
        )
        if transient is GlobalTransientResult.ERROR:
            break
        if transient is GlobalTransientResult.HANDLED_POPUP:
            last_frame = fresh_frame
            start_state = start_detector(list(fresh_items), fresh_frame)
            if start_state != "UNKNOWN":
                break
        if attempt + 1 < ALLIANCE_OCR_ATTEMPTS:
            session.sleep(ALLIANCE_OCR_RETRY_WAIT_SECONDS)

    log(f"[AllianceDaily] start_state={start_state}")
    if start_state == "UNKNOWN":
        if capture_unknown is not None and last_frame is not None:
            capture_unknown("QJNN_AllianceDailyUnknownStart", last_frame)
        log("当前页面无法可靠恢复；未返回 HOME、未执行任何猜测点击")
        return AllianceDailyResult(False, start_state, "start_state_unknown")

    commission_session = _BackArrowSessionView(
        session,
        session.commission_back_arrow_score,
    )
    library_session = _BackArrowSessionView(
        session,
        session.top_back_arrow_score,
    )
    if library_three and getattr(session,'account_scoped_records',False):
        from qjnn_account_scope import account_record_path
        from qjnn_library_three import RECORD
        library_session.library_record_path=lambda: account_record_path(session,RECORD)

    if start_state == "HOME":
        def navigation_capture():
            frame = session.capture_frame()
            if str(getattr(detect_home_page(frame), "page", "Unknown")) in {
                "P1", "P2", "P3"
            }:
                return frame
            current_items = session.recognize_text(frame)
            transient, fresh_frame, _ = handle_transient_if_present(
                session,
                frame,
                current_items,
                capture=capture_unknown,
                log=log,
            )
            if transient is GlobalTransientResult.ERROR:
                raise RuntimeError("主页导航期间临时状态处理失败")
            return fresh_frame if transient is GlobalTransientResult.HANDLED_POPUP else frame

        navigation = navigate_home_page(
            "P3",
            navigation_capture,
            session.click,
            sleep=session.sleep,
            log=log,
        )
        if not navigation.success:
            return AllianceDailyResult(False, start_state, "home_navigation_failed")
        commission = run_alliance_commission(
            commission_session,
            log=log,
            capture=capture,
        )
    elif start_state == "ALLIANCE_HOME":
        commission = run_alliance_commission_from_home(
            commission_session,
            log=log,
            capture=capture,
        )
    elif start_state == "COMMISSION_PAGE":
        commission = run_alliance_commission_resume(
            commission_session,
            log=log,
            capture=capture,
        )
    else:
        commission = None

    if commission is not None and not commission.success:
        log(f'[AllianceDaily] commission failed: {commission.reason}')
        return AllianceDailyResult(False, start_state, f"commission:{commission.reason}")

    library_options = {"organize_three": True} if library_three else {}
    library = run_library(library_session, log=log, capture=capture, **library_options)
    if not library.success:
        log(f'[AllianceDaily] library failed: {library.reason}')
        return AllianceDailyResult(False, start_state, f"library:{library.reason}")

    exited = run_alliance_daily_exit(library_session, log=log)
    if not exited.success:
        return AllianceDailyResult(False, start_state, f"exit:{exited.reason}")
    return AllianceDailyResult(True, start_state, "completed")


def run_alliance_daily_exit(
    session: AllianceDailyExitSession,
    *,
    detect_page: Callable[[object], object] = detect_home_page,
    log: Callable[[str], None] = print,
) -> AllianceDailyExitResult:
    frame = None
    for attempt in range(LIBRARY_OCR_ATTEMPTS):
        candidate = session.capture_frame()
        items = session.recognize_text(candidate)
        is_alliance_home = find_alliance_home(items, candidate) is not None
        has_back_arrow = (
            session.back_arrow_score(candidate) >= LIBRARY_TOP_BACK_THRESHOLD
        )
        if is_alliance_home and has_back_arrow:
            frame = candidate
            break
        if attempt + 1 < LIBRARY_OCR_ATTEMPTS:
            session.sleep(LIBRARY_OCR_RETRY_WAIT_SECONDS)
    if frame is None:
        log("联盟总任务退出失败：未同时确认联盟首页和顶部返回箭头")
        return AllianceDailyExitResult(False, "Unknown", "exit_context_not_confirmed")

    frame_height, frame_width = frame.shape[:2]
    click_point = map_alliance_reference_point(
        LIBRARY_TOP_BACK_CLICK_POINT,
        frame_width,
        frame_height,
    )
    log("联盟日常完成，退出搭配师联盟")
    if alliance_business_click(session, *click_point) is False:
        return AllianceDailyExitResult(False, "Unknown", "exit_click_failed")
    session.sleep(LIBRARY_PAGE_WAIT_SECONDS)

    last_page = "Unknown"
    exit_clicks = 1
    lobby_seen = False
    for attempt in range(LIBRARY_OCR_ATTEMPTS):
        home_frame = session.capture_frame()
        last_page = str(getattr(detect_page(home_frame), "page", "Unknown"))
        if last_page == "P3":
            log("已退出搭配师联盟并确认主页 P3")
            return AllianceDailyExitResult(True, "P3", "completed")
        # The library-to-lobby animation can absorb the first Back. Only a
        # positively identified lobby and arrow on two fresh observations may
        # authorize another exit; an unknown page never authorizes input.
        items = session.recognize_text(home_frame)
        lobby = (last_page == "Unknown"
                 and find_alliance_home(items, home_frame) is not None
                 and session.back_arrow_score(home_frame) >= LIBRARY_TOP_BACK_THRESHOLD)
        if lobby and lobby_seen and exit_clicks < 3:
            def confirm_exit():
                fresh = session.capture_frame()
                fresh_items = session.recognize_text(fresh)
                if (str(getattr(detect_page(fresh), "page", "Unknown")) == "Unknown"
                        and find_alliance_home(fresh_items, fresh) is not None
                        and session.back_arrow_score(fresh) >= LIBRARY_TOP_BACK_THRESHOLD):
                    height, width = fresh.shape[:2]
                    x, y = map_alliance_reference_point(LIBRARY_TOP_BACK_CLICK_POINT, width, height)
                    return Rect(x - 4, y - 4, 8, 8)
                return None
            log(f"联盟首页仍在，重新确认返回箭头后退出：{exit_clicks + 1}/3")
            if alliance_business_click(session, *click_point, confirm=confirm_exit) is False:
                return AllianceDailyExitResult(False, last_page, "exit_recheck_failed")
            exit_clicks += 1
            lobby_seen = False
            session.sleep(LIBRARY_PAGE_WAIT_SECONDS)
        else:
            lobby_seen = lobby
        if attempt + 1 < LIBRARY_OCR_ATTEMPTS:
            session.sleep(LIBRARY_OCR_RETRY_WAIT_SECONDS)

    log(f"联盟总任务退出失败：返回后主页状态为 {last_page}")
    return AllianceDailyExitResult(False, last_page, "p3_not_confirmed")


class AllianceDailyAction(CustomAction):
    def __init__(self, alliance_capture: AllianceCaptureManager | None = None) -> None:
        super().__init__()
        self._alliance_capture = alliance_capture

    def run(self, context, argv) -> bool:
        controller = context.tasker.controller

        class RuntimeSession:
            account_scoped_records=True
            @staticmethod
            def capture_frame():
                job = controller.post_screencap()
                job.wait()
                if hasattr(job, "succeeded") and not job.succeeded:
                    raise RuntimeError("联盟总任务退出截图失败")
                cached = getattr(controller, "cached_image", None)
                frame = cached() if callable(cached) else cached
                if frame is None or getattr(frame, "size", 0) == 0:
                    raise RuntimeError("联盟总任务退出截图为空")
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
                    raise RuntimeError(f"联盟总任务退出点击失败：({x}, {y})")

            sleep = staticmethod(time.sleep)
            monotonic = staticmethod(time.monotonic)
            commission_back_arrow_score = staticmethod(commission_back_arrow_score)
            top_back_arrow_score = staticmethod(top_back_arrow_score)

        try:
            diagnostic_capture = self._alliance_capture or AllianceCaptureManager()
            business_capture = (
                self._alliance_capture.capture_once
                if self._alliance_capture is not None
                else None
            )
            return run_alliance_daily(
                guard_runtime_session(context, RuntimeSession()),
                capture=business_capture,
                capture_unknown=diagnostic_capture.capture_once,
                **_library_options(argv),
            ).success
        except Exception as exc:
            print(f"联盟日常状态恢复失败：{exc}")
            return False


class AllianceDailyExitAction(CustomAction):
    """Compatibility/debug exit-only action retained for existing debug chains."""

    def run(self, context, argv) -> bool:
        controller = context.tasker.controller

        class RuntimeSession:
            @staticmethod
            def capture_frame():
                job = controller.post_screencap()
                job.wait()
                if hasattr(job, "succeeded") and not job.succeeded:
                    raise RuntimeError("联盟总任务退出截图失败")
                cached = getattr(controller, "cached_image", None)
                frame = cached() if callable(cached) else cached
                if frame is None or getattr(frame, "size", 0) == 0:
                    raise RuntimeError("联盟总任务退出截图为空")
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
                        OcrItem(str(result.text), rect, float(getattr(result, "score", 0.0)))
                    )
                return converted

            @staticmethod
            def click(x: int, y: int) -> None:
                job = controller.post_click(x, y)
                job.wait()
                if hasattr(job, "succeeded") and not job.succeeded:
                    raise RuntimeError(f"联盟总任务退出点击失败：({x}, {y})")

            sleep = staticmethod(time.sleep)
            back_arrow_score = staticmethod(top_back_arrow_score)

        try:
            return run_alliance_daily_exit(
                guard_runtime_session(context, RuntimeSession())
            ).success
        except Exception as exc:
            print(f"联盟总任务退出失败：{exc}")
            return False
