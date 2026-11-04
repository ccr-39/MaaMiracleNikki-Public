from __future__ import annotations

from app_paths import DATA_ROOT

import re
import time
from dataclasses import dataclass, replace
from enum import Enum
import json
from pathlib import Path
from typing import Callable, Protocol, Sequence

from qjnn_alliance_commission import OcrItem, Rect, find_text_box
from qjnn_arena import arena_back_button_score
from qjnn_common_popup import (
    GlobalTransientResult,
    guard_runtime_session,
    handle_transient_if_present,
)
from qjnn_home import detect_home_page

try:
    from maa.custom_action import CustomAction
    from maa.custom_recognition import CustomRecognition
except ImportError:  # MaaFramework is optional for static checks and offline tests.
    CustomAction = object
    CustomRecognition = object


FRIEND_PAGE_TITLE = "好友一览"
FRIEND_SEND_HEART_TEXT = "一键送心"
FRIEND_SEND_HEART_OCR_TEXTS = (FRIEND_SEND_HEART_TEXT, "键送心")
FRIEND_SUCCESS_PREFIX = "体力已成功发送给"
FRIEND_SUCCESS_SUFFIX = "个好友"
FRIEND_NO_TARGET_TEXT = "目前没有可以送心的好友"
FRIEND_SUCCESS_PATTERN = re.compile(
    rf"^{re.escape(FRIEND_SUCCESS_PREFIX)}[0-9０-９]+{re.escape(FRIEND_SUCCESS_SUFFIX)}$"
)
FRIEND_FIXED_TEXTS = {
    FRIEND_PAGE_TITLE,
    "待承认好友",
    "寻找好友",
    *FRIEND_SEND_HEART_OCR_TEXTS,
}
FRIEND_REFERENCE_SIZE = (900, 1600)

# These are intentionally broad bands, not screenshot-derived narrow boxes.
FRIEND_TITLE_BAND = (0, 0, 900, 640)
FRIEND_ACTION_BAND = (0, 880, 900, 720)
FRIEND_BACK_SAFE_RECT = (30, 25, 80, 80)
FRIEND_BACK_TEMPLATE_THRESHOLD = 0.80
FRIEND_PRECLICK_SNAPSHOTS = 2
FRIEND_PRECLICK_INTERVAL_SECONDS = 0.2
FRIEND_MAX_BUTTON_DRIFT_REFERENCE_PIXELS = 40
FRIEND_RESULT_TIMEOUT_SECONDS = 6.0
FRIEND_RESULT_POLL_SECONDS = 0.4
FRIEND_RESULT_MIN_SCORE = 0.65
FRIEND_RESULT_MAX_TEXT_LENGTH = 30
FRIEND_HOME_PAGES = {"P1", "P2", "P3"}
_friend_attempt: "FriendSendHeartAttempt | None" = None
_friend_home_entry_authorized = False
_friend_home_entry_controller: object | None = None
PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRIEND_CAPTURE_ROOT = DATA_ROOT / "images" / "screenshots" / "friend"
FRIEND_BEFORE_CAPTURE = FRIEND_CAPTURE_ROOT / "friend_page_before_send_real_001.png"
FRIEND_NO_TARGET_CAPTURE = (
    FRIEND_CAPTURE_ROOT / "friend_send_heart_no_target_real_001.png"
)
FRIEND_SUCCESS_CAPTURE = (
    FRIEND_CAPTURE_ROOT / "friend_send_heart_success_real_001.png"
)
FRIEND_UNKNOWN_CAPTURE = (
    DATA_ROOT / "images" / "debug" / "pipeline" / "friend_send_heart_unknown_001.png"
)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value))


def _scaled_rect(
    rect: tuple[int, int, int, int],
    frame_width: int,
    frame_height: int,
) -> Rect:
    reference_width, reference_height = FRIEND_REFERENCE_SIZE
    x, y, width, height = rect
    left = round(x * frame_width / reference_width)
    top = round(y * frame_height / reference_height)
    right = round((x + width) * frame_width / reference_width)
    bottom = round((y + height) * frame_height / reference_height)
    return Rect(left, top, max(1, right - left), max(1, bottom - top))


def find_friend_page(
    items: list[OcrItem],
    frame,
) -> tuple[Rect, Rect] | None:
    """Require both stable labels; never inspect dynamic friend-list content."""
    height, width = frame.shape[:2]
    title = find_text_box(
        items,
        FRIEND_PAGE_TITLE,
        roi=_scaled_rect(FRIEND_TITLE_BAND, width, height),
    )
    action_roi = _scaled_rect(FRIEND_ACTION_BAND, width, height)
    send_heart = next(
        (
            box
            for target in FRIEND_SEND_HEART_OCR_TEXTS
            if (box := find_text_box(items, target, roi=action_roi)) is not None
        ),
        None,
    )
    if title is None or send_heart is None:
        return None
    if not (
        send_heart.width > 0 and send_heart.height > 0
        and action_roi.x <= send_heart.x and action_roi.y <= send_heart.y
        and send_heart.right <= action_roi.right and send_heart.bottom <= action_roi.bottom
    ):
        return None
    return title, send_heart


def ocr_text_set(items: Sequence[OcrItem]) -> set[str]:
    return {_normalize_text(item.text) for item in items if _normalize_text(item.text)}


def find_new_result_text(
    items: Sequence[OcrItem],
    baseline: set[str],
) -> OcrItem | None:
    """Find a newly appeared Chinese line without guessing its wording or ROI."""
    candidates: list[OcrItem] = []
    for item in items:
        text = _normalize_text(item.text)
        if not text or text in baseline or text in FRIEND_FIXED_TEXTS:
            continue
        if not 2 <= len(text) <= FRIEND_RESULT_MAX_TEXT_LENGTH:
            continue
        if float(item.score) < FRIEND_RESULT_MIN_SCORE:
            continue
        if re.search(r"[\u3400-\u9fff]", text) is None:
            continue
        candidates.append(item)
    if not candidates:
        return None
    return max(candidates, key=lambda item: float(item.score))


class FriendSendHeartResultKind(str, Enum):
    SUCCESS = "SUCCESS"
    NO_TARGET = "NO_TARGET"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FriendSendHeartResult:
    kind: FriendSendHeartResultKind
    item: OcrItem | None = None


def classify_send_heart_result(
    items: Sequence[OcrItem],
) -> FriendSendHeartResult:
    """Classify only the two result texts backed by real-device evidence."""
    candidates = sorted(items, key=lambda item: float(item.score), reverse=True)
    for item in candidates:
        if float(item.score) < FRIEND_RESULT_MIN_SCORE:
            continue
        text = _normalize_text(item.text)
        if FRIEND_SUCCESS_PATTERN.fullmatch(text):
            return FriendSendHeartResult(FriendSendHeartResultKind.SUCCESS, item)
        if text == FRIEND_NO_TARGET_TEXT:
            return FriendSendHeartResult(FriendSendHeartResultKind.NO_TARGET, item)
    return FriendSendHeartResult(FriendSendHeartResultKind.UNKNOWN)


@dataclass(frozen=True)
class FriendSendHeartAttempt:
    baseline: frozenset[str]
    clicked_box: Rect
    clicked_at: float
    state: str = "in_flight"
    controller_key: object | None = None


def _controller_key(session):
    controller = getattr(session, "controller", None)
    if controller is None:
        return None  # Offline sessions need no real Controller.
    # Maa's Tasker.controller returns a new Python wrapper per access. Use the
    # bound native handle to keep authorization stable across CustomActions.
    handle = getattr(controller, "_handle", None)
    value = getattr(handle, "value", handle)
    return ("maa", value) if value else ("object", id(controller))


def begin_friend_run() -> None:
    """A new task must obtain its own HOME-entry authorization."""
    global _friend_home_entry_authorized, _friend_home_entry_controller
    _friend_home_entry_authorized = False
    _friend_home_entry_controller = None


def enter_friend_from_home(session: FriendSession, *, log: Callable[[str], None] = print) -> bool:
    global _friend_home_entry_authorized, _friend_home_entry_controller
    begin_friend_run()
    if _friend_attempt is not None:
        log("[FriendEntry] unresolved attempt; SafeStop without another send")
        return False
    frame = session.capture_frame()
    if detect_home_page(frame).page not in FRIEND_HOME_PAGES:
        log("[FriendEntry] HOME not confirmed; zero clicks")
        return False
    entry = find_text_box(session.recognize_text(frame), "好友")
    if entry is None:
        log("[FriendEntry] friend entry not confirmed; zero clicks")
        return False
    session.click(*entry.center)
    # Grant only after this task's verified HOME entry click succeeds. The
    # existing Pipeline still confirms FriendScreen before the two-frame check.
    _friend_home_entry_authorized = True
    _friend_home_entry_controller = _controller_key(session)
    return True


def save_full_frame(path: Path, frame) -> None:
    import cv2

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded, data = cv2.imencode(".png", frame)
    if not encoded:
        raise RuntimeError(f"好友送心截图编码失败：{path}")
    data.tofile(str(path))
    print(f"[FriendSendHeartCapture] saved full frame: {path}")


def log_ocr_items(prefix: str, items: Sequence[OcrItem]) -> None:
    print(f"{prefix} OCR count={len(items)}")
    for item in items:
        print(
            f"{prefix} text={item.text!r}, score={float(item.score):.6f}, "
            f"box=({item.box.x},{item.box.y},{item.box.width},{item.box.height})"
        )


class FriendSession(Protocol):
    controller: object

    def capture_frame(self): ...

    def recognize_text(self, frame) -> list[OcrItem]: ...

    def click(self, x: int, y: int) -> None: ...

    def sleep(self, seconds: float) -> None: ...

    def monotonic(self) -> float: ...


def click_send_heart_once(
    session: FriendSession,
    *,
    log: Callable[[str], None] = print,
) -> bool:
    global _friend_attempt, _friend_home_entry_authorized, _friend_home_entry_controller
    if _friend_attempt is not None:
        log("[FriendSendHeartClick] 已存在点击记录，拒绝重复点击")
        return False

    authorized = (
        _friend_home_entry_authorized
        and _friend_home_entry_controller == _controller_key(session)
    )
    begin_friend_run()  # Consume the grant even if subsequent validation fails.
    if not authorized:
        log("[FriendSendHeartClick] no verified HOME entry; zero-send SafeStop")
        return False

    baseline: set[str] = set()
    page: tuple[Rect, Rect] | None = None
    first_button: Rect | None = None
    first_shape = None
    for index in range(FRIEND_PRECLICK_SNAPSHOTS):
        frame = session.capture_frame()
        items = session.recognize_text(frame)
        baseline.update(ocr_text_set(items))
        candidate_page = find_friend_page(items, frame)
        if candidate_page is None:
            transient, fresh_frame, fresh_items = handle_transient_if_present(
                session,
                frame,
                items,
                log=log,
            )
            if transient is GlobalTransientResult.ERROR:
                return False
            if transient is GlobalTransientResult.HANDLED_POPUP:
                frame, items = fresh_frame, list(fresh_items)
                baseline.update(ocr_text_set(items))
                candidate_page = find_friend_page(items, frame)
        if candidate_page is None:
            log(f"[FriendSendHeartClick] frame {index + 1} invalid; zero clicks")
            return False
        # Never retain an earlier frame's target after a later frame misses.
        page = candidate_page
        if first_button is None:
            first_button = page[1]
            first_shape = frame.shape[:2]
        else:
            drift = FRIEND_MAX_BUTTON_DRIFT_REFERENCE_PIXELS * frame.shape[1] / FRIEND_REFERENCE_SIZE[0]
            if first_shape != frame.shape[:2] or any(
                abs(current - previous) > drift
                for current, previous in zip(page[1].center, first_button.center)
            ):
                log("[FriendSendHeartClick] button drift/page geometry changed; zero clicks")
                return False
        if index + 1 < FRIEND_PRECLICK_SNAPSHOTS:
            session.sleep(FRIEND_PRECLICK_INTERVAL_SECONDS)

    if page is None:
        log("[FriendSendHeartClick] 未同时识别“好友一览”和“一键送心”，零点击停止")
        return False

    title_box, send_heart_box = page
    title_item = next(
        item for item in items if find_text_box([item], FRIEND_PAGE_TITLE) is not None
    )
    send_item = next(
        item
        for target in FRIEND_SEND_HEART_OCR_TEXTS
        for item in items
        if find_text_box([item], target) is not None
    )
    print(
        "[FriendScreen] "
        f"text={title_item.text!r}, score={float(title_item.score):.6f}, box={title_box}"
    )
    print("[FriendSendHeart] button matched:")
    print(
        f"[FriendSendHeart] text={send_item.text!r}, "
        f"confidence={float(send_item.score):.6f}, box={send_heart_box}"
    )
    save_full_frame(FRIEND_BEFORE_CAPTURE, frame)
    _friend_attempt = FriendSendHeartAttempt(
        frozenset(baseline), send_heart_box, session.monotonic(),
        controller_key=_controller_key(session),
    )
    try:
        session.click(*send_heart_box.center)
    except Exception as exc:
        # A controller error does not prove that the device received no click.
        # Keep the fence, but never authorize automatic continuation/retry.
        _friend_attempt = replace(_friend_attempt, state="click_unknown")
        log(f"[FriendSendHeartClick] click outcome unknown; attempt retained; SafeStop: {exc}")
        return False
    _friend_attempt = replace(_friend_attempt, state="sent", clicked_at=session.monotonic())
    log(
        "[FriendSendHeartClick] 已单击一次 OCR box："
        f"box={send_heart_box}, point={send_heart_box.center}"
    )
    return True


def send_or_resume_friend_attempt(session: FriendSession, *, log: Callable[[str], None] = print) -> bool:
    if _friend_attempt is not None:
        trusted = (
            _friend_attempt.state == "sent"
            and _friend_attempt.controller_key == _controller_key(session)
        )
        log("[FriendSendHeartClick] resume existing result wait" if trusted else "[FriendSendHeartClick] untrusted/ambiguous attempt; SafeStop")
        return trusted
    return click_send_heart_once(session, log=log)


def wait_for_send_heart_result(
    session: FriendSession,
    *,
    timeout: float = FRIEND_RESULT_TIMEOUT_SECONDS,
    interval: float = FRIEND_RESULT_POLL_SECONDS,
    log: Callable[[str], None] = print,
) -> bool:
    global _friend_attempt
    attempt = _friend_attempt
    if (
        not isinstance(attempt, FriendSendHeartAttempt)
        or attempt.state != "sent"
        or attempt.controller_key != _controller_key(session)
    ):
        log("[FriendSendHeartWaitingResult] 缺少已执行点击记录，拒绝确认成功")
        return False
    if timeout <= 0 or interval <= 0:
        raise ValueError("送心结果等待 timeout 和 interval 必须大于 0")

    deadline = session.monotonic() + timeout
    observed_new_texts: set[str] = set()
    last_frame = None
    while session.monotonic() < deadline:
        # Always wait before the fresh frame so the immediate post-click stale frame
        # can never become the only result check.
        remaining = deadline - session.monotonic()
        session.sleep(min(interval, remaining))
        frame = session.capture_frame()
        last_frame = frame
        items = session.recognize_text(frame)
        elapsed = session.monotonic() - attempt.clicked_at
        log_ocr_items(
            f"[FriendSendHeartWaitingResult][elapsed={elapsed:.3f}s]",
            items,
        )
        if find_friend_page(items, frame) is None:
            transient, fresh_frame, fresh_items = handle_transient_if_present(
                session,
                frame,
                items,
                log=log,
            )
            if transient is GlobalTransientResult.ERROR:
                return False
            if transient is GlobalTransientResult.HANDLED_POPUP:
                frame, items = fresh_frame, list(fresh_items)
                last_frame = frame
                if find_friend_page(items, frame) is not None:
                    classified = classify_send_heart_result(items)
                    if classified.kind is not FriendSendHeartResultKind.UNKNOWN:
                        result = classified.item
                        assert result is not None
                        text = _normalize_text(result.text)
                        log(
                            "[FriendSendHeartWaitingResult] transient cleared; "
                            f"result={classified.kind.value}, text={text!r}, "
                            f"confidence={float(result.score):.6f}, box={result.box}"
                        )
                        capture = (
                            FRIEND_SUCCESS_CAPTURE
                            if classified.kind is FriendSendHeartResultKind.SUCCESS
                            else FRIEND_NO_TARGET_CAPTURE
                        )
                        save_full_frame(capture, frame)
                        _friend_attempt = None
                        return True
            log("[FriendSendHeartWaitingResult] 当前帧未稳定确认好友页，继续有限轮询")
            continue
        classified = classify_send_heart_result(items)
        if classified.kind is not FriendSendHeartResultKind.UNKNOWN:
            result = classified.item
            assert result is not None
            text = _normalize_text(result.text)
            log(
                "[FriendSendHeartWaitingResult] result classified: "
                f"kind={classified.kind.value}, text={text!r}, "
                f"confidence={float(result.score):.6f}, box={result.box}"
            )
            capture = (
                FRIEND_SUCCESS_CAPTURE
                if classified.kind is FriendSendHeartResultKind.SUCCESS
                else FRIEND_NO_TARGET_CAPTURE
            )
            save_full_frame(capture, frame)
            _friend_attempt = None
            return True

        # Dynamic OCR delta remains diagnostic-only. It can never authorize
        # FriendBack or completion.
        diagnostic = find_new_result_text(items, set(attempt.baseline))
        if diagnostic is not None:
            text = _normalize_text(diagnostic.text)
            if text not in observed_new_texts:
                observed_new_texts.add(text)
                log(
                    "[FriendSendHeartWaitingResult] diagnostic-only new OCR text: "
                    f"text={text!r}, confidence={float(diagnostic.score):.6f}, "
                    f"box={diagnostic.box}; result=UNKNOWN"
                )

    if last_frame is not None:
        save_full_frame(FRIEND_UNKNOWN_CAPTURE, last_frame)
    log(
        "[FriendSendHeartWaitingResult] 有限轮询超时；result=UNKNOWN；"
        f"不重复点击。observed={sorted(observed_new_texts)!r}"
    )
    return False


def click_friend_back_once(
    session: FriendSession,
    *,
    log: Callable[[str], None] = print,
) -> bool:
    """Click the in-page pink arrow only after FriendScreen and template gates."""
    global _friend_attempt
    frame = session.capture_frame()
    items = session.recognize_text(frame)
    if find_friend_page(items, frame) is None:
        transient, fresh_frame, fresh_items = handle_transient_if_present(
            session,
            frame,
            items,
            log=log,
        )
        if transient is GlobalTransientResult.HANDLED_POPUP:
            frame, items = fresh_frame, list(fresh_items)
        elif transient is GlobalTransientResult.ERROR:
            return False
    if find_friend_page(items, frame) is None:
        log("[FriendBack] FriendScreen 未确认，零点击停止")
        return False
    template_score = arena_back_button_score(frame)
    if template_score < FRIEND_BACK_TEMPLATE_THRESHOLD:
        log(
            "[FriendBack] 左上粉色圆形返回模板未确认，零点击停止："
            f"score={template_score:.6f}, "
            f"threshold={FRIEND_BACK_TEMPLATE_THRESHOLD:.2f}"
        )
        return False
    height, width = frame.shape[:2]
    safe_rect = _scaled_rect(FRIEND_BACK_SAFE_RECT, width, height)
    log(
        "[FriendBack] FriendScreen confirmed; "
        f"template_score={template_score:.6f}, SafeRect={safe_rect}, "
        f"point={safe_rect.center}"
    )
    session.click(*safe_rect.center)
    if (
        classify_send_heart_result(items).kind is not FriendSendHeartResultKind.UNKNOWN
        and _friend_attempt is not None
        and _friend_attempt.controller_key == _controller_key(session)
    ):
        _friend_attempt = None
    begin_friend_run()
    return True


def _make_runtime_session(context):
    tasker = getattr(context, "tasker", None)
    controller = getattr(tasker, "controller", None)
    if controller is None:
        raise RuntimeError("Friend runtime controller is unavailable; SafeStop")

    class RuntimeSession:
        @staticmethod
        def capture_frame():
            job = controller.post_screencap()
            job.wait()
            if hasattr(job, "succeeded") and not job.succeeded:
                raise RuntimeError("好友送心截图失败")
            cached = getattr(controller, "cached_image", None)
            frame = cached() if callable(cached) else cached
            if frame is None or getattr(frame, "size", 0) == 0:
                raise RuntimeError("好友送心截图为空")
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
                raise RuntimeError(f"好友送心点击失败：({x}, {y})")

        sleep = staticmethod(time.sleep)
        monotonic = staticmethod(time.monotonic)

    runtime = RuntimeSession()
    runtime.controller = controller
    return guard_runtime_session(context, runtime)


def _ocr_items_from_detail(detail) -> list[OcrItem]:
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


class FriendSendHeartResultRecognition(CustomRecognition):
    """Recognize a known result only in the strict FriendPage context."""

    def analyze(self, context, argv):
        try:
            param = json.loads(argv.custom_recognition_param or "{}")
            expected_kind = FriendSendHeartResultKind(str(param.get("kind", "")))
            detail = context.run_recognition("QJNN_AgentOcrBase", argv.image)
            items = _ocr_items_from_detail(detail)
            classified = classify_send_heart_result(items)
            page_matched = find_friend_page(items, argv.image) is not None
            matched = page_matched and classified.kind is expected_kind
            result = classified.item
            return CustomRecognition.AnalyzeResult(
                box=(
                    [result.box.x, result.box.y, result.box.width, result.box.height]
                    if matched and result is not None
                    else None
                ),
                detail={
                    "kind": classified.kind.value,
                    "expected_kind": expected_kind.value,
                    "friend_page": page_matched,
                    "text": _normalize_text(result.text) if result is not None else None,
                    "confidence": float(result.score) if result is not None else 0.0,
                },
            )
        except Exception as exc:
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={"kind": FriendSendHeartResultKind.UNKNOWN.value, "error": str(exc)},
            )


class HomeAnyPageRecognition(CustomRecognition):
    """Match a real P1/P2/P3 page that also exposes the shared Friend entry."""

    def analyze(self, context, argv):
        try:
            page_detection = detect_home_page(argv.image)
            page = page_detection.page
            # Keep the global HomeScreen result diagnostic-only. Its “任务” OCR
            # is known to miss the stylized P3 footer and must not gate this task.
            params = json.loads(getattr(argv, 'custom_recognition_param', '{}') or "{}")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise ValueError('HOME recognition parameters must be an object')
            strict = params.get("strict", False)
            if type(strict) is not bool:
                raise ValueError('HOME strict parameter must be boolean')
            home = None if strict else context.run_recognition("QJNN_HomeScreen", argv.image)
            friend = context.run_recognition(
                "QJNN_AgentOcrBase",
                argv.image,
                pipeline_override={
                    "QJNN_AgentOcrBase": {
                        "expected": ["好友"],
                        "threshold": 0.3,
                    }
                },
            )
            page_matched = page in FRIEND_HOME_PAGES
            friend_matched = friend is not None and friend.hit
            home_matched = home is not None and home.hit
            matched = page_matched and friend_matched
            if strict:
                from qjnn_home_room_entry import home_toolbar_anchor_count
                from qjnn_common_popup import detect_common_safe_popup, CommonPopupKind
                detail = context.run_recognition("QJNN_AgentOcrBase", argv.image)
                items = _ocr_items_from_detail(detail)
                texts = {"".join(i.text.split()) for i in items if i.score >= .6}
                overlays = {"暖暖月签到", "恭喜获得", "获得奖励", "确定", "确认", "取消"}
                h,w=argv.image.shape[:2]
                mail_header=any(''.join(i.text.split())=='邮箱' and i.score>=.6
                                and .2*w<=i.box.center[0]<=.8*w and i.box.center[1]<.25*h for i in items)
                matched = (matched and home_toolbar_anchor_count(items, argv.image) >= 2
                           and not texts.intersection(overlays) and not mail_header
                           and detect_common_safe_popup(argv.image, items).kind is CommonPopupKind.NONE)
            print(
                "[HomeAnyPage] "
                f"page={page}, left_score={page_detection.left_score:.6f}, "
                f"right_score={page_detection.right_score:.6f}, "
                f"HomeScreen={home_matched}, friend_entry={friend_matched}, "
                f"matched={matched}"
            )
            if friend_matched and getattr(friend, "best_result", None) is not None:
                result = friend.best_result
                box = result.box
                values = (
                    (int(box.x), int(box.y), int(box.w), int(box.h))
                    if all(hasattr(box, name) for name in ("x", "y", "w", "h"))
                    else tuple(int(value) for value in list(box)[:4])
                )
                print(
                    f"[HomeAnyPage] friend text={result.text!r}, "
                    f"confidence={float(getattr(result, 'score', 0.0)):.6f}, "
                    f"box={values}"
                )
            return CustomRecognition.AnalyzeResult(
                box=[0, 0, 1, 1] if matched else None,
                detail={
                    "page": page,
                    "page_matched": page_matched,
                    "home_screen": home_matched,
                    "friend_entry": friend_matched,
                    "matched": matched,
                },
            )
        except Exception as exc:
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={"page": "Unknown", "error": str(exc)},
            )


class FriendSendHeartClickAction(CustomAction):
    def run(self, context, argv) -> bool:
        try:
            param = json.loads(getattr(argv, "custom_action_param", "") or "{}")
            if param is None:
                # Maa serializes an omitted custom_action_param as JSON null.
                # The formal send node intentionally has no mode parameter.
                mode = "send"
            elif isinstance(param, dict):
                mode = param.get("mode", "send")
            else:
                print("[FriendSendHeartClick] invalid action parameter type; SafeStop")
                return False
            if mode not in ("begin", "entry", "send"):
                print("[FriendSendHeartClick] invalid action mode; SafeStop")
                return False
            if mode == "begin":
                begin_friend_run()
                return True
            session = _make_runtime_session(context)
            if session is None:
                print("[FriendSendHeartClick] runtime session is unavailable; SafeStop")
                return False
            if mode == "entry":
                return enter_friend_from_home(session)
            if mode == "send":
                return send_or_resume_friend_attempt(session)
            return False
        except Exception as exc:
            print(f"[FriendSendHeartClick] 失败：{exc}")
            return False


class FriendSendHeartWaitingResultAction(CustomAction):
    def run(self, context, argv) -> bool:
        try:
            return wait_for_send_heart_result(_make_runtime_session(context))
        except Exception as exc:
            print(f"[FriendSendHeartWaitingResult] 失败：{exc}")
            return False


class FriendBackAction(CustomAction):
    def run(self, context, argv) -> bool:
        try:
            return click_friend_back_once(_make_runtime_session(context))
        except Exception as exc:
            print(f"[FriendBack] 失败：{exc}")
            return False
