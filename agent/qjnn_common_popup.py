from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol, Sequence

from qjnn_reward import rect_contains, rects_intersect

try:
    from maa.custom_action import CustomAction
except ImportError:  # MaaFramework is optional for offline tests.
    CustomAction = object


COMMON_POPUP_REFERENCE_SIZE = (720, 1280)
COMMON_ACHIEVEMENT_TITLE_ROI = (90, 220, 540, 260)
COMMON_ACHIEVEMENT_POPUP_RECT = (80, 230, 560, 760)
COMMON_ACHIEVEMENT_SAFE_RECT = (120, 570, 200, 45)
COMMON_ACHIEVEMENT_SHARE_FORBIDDEN_RECT = (500, 630, 170, 100)
COMMON_ACHIEVEMENT_STRUCTURE_THRESHOLD = 0.22
# Measured on the 720x1280 skill-unlock capture. Keep the dismissal region
# below the entire foreground card and away from the side toolbars.
COMMON_SKILL_UNLOCK_TITLE_ROI = (145, 300, 430, 120)
COMMON_SKILL_UNLOCK_POPUP_RECT = (30, 265, 660, 575)
COMMON_SKILL_UNLOCK_SAFE_RECT = (240, 870, 240, 120)
COMMON_POPUP_OCR_THRESHOLD = 0.70
MAX_COMMON_POPUP_DISMISS = 5
MAX_ACHIEVEMENT_POPUP_DISMISS = 32
COMMON_POPUP_DISAPPEAR_TIMEOUT_SECONDS = 5.0
COMMON_POPUP_POLL_INTERVAL_SECONDS = 0.4
COMMON_POPUP_RECLICK_CONFIRM_POLLS = 2
COMMON_POPUP_ANIMATION_SETTLE_SECONDS = 1.6
COMMON_POPUP_STABLE_PIXEL_DELTA = 2.0


class CommonPopupKind(str, Enum):
    NONE = "none"
    ACHIEVEMENT = "achievement"
    SKILL_UNLOCK = "skill_unlock"
    NOTICE_CONFIRM = "notice_confirm"
    NOTICE_OUTSIDE = "notice_outside"
    UNKNOWN = "unknown"


class GlobalTransientResult(str, Enum):
    HANDLED_POPUP = "handled_popup"
    WAITED_LOADING = "waited_loading"
    NOT_TRANSIENT = "not_transient"
    ERROR = "error"


class CommonPopupHandlingError(RuntimeError):
    pass


class CommonPopupUnknownError(CommonPopupHandlingError):
    pass


class CommonPopupLimitError(CommonPopupHandlingError):
    pass


class CommonPopupDisappearTimeout(CommonPopupHandlingError):
    pass


class _PopupAlreadyDisappeared(CommonPopupHandlingError):
    pass


class OcrLike(Protocol):
    text: str
    box: object
    score: float


@dataclass(frozen=True)
class CommonPopupDetection:
    kind: CommonPopupKind
    matched: bool
    title_found: bool
    structure_score: float
    popup_rect: tuple[int, int, int, int]
    safe_rect: tuple[int, int, int, int]
    share_forbidden_rect: tuple[int, int, int, int]
    title_text: str | None = None
    title_confidence: float | None = None
    title_box: tuple[int, int, int, int] | None = None
    share_box: tuple[int, int, int, int] | None = None
    achievement_identity: tuple[str, ...] = ()


def _normalize_text(value: str) -> str:
    return "".join(str(value).split()).replace(":", "").replace("：", "")


def _scaled_rect(rect: tuple[int, int, int, int], width: int, height: int):
    ref_width, ref_height = COMMON_POPUP_REFERENCE_SIZE
    x, y, w, h = rect
    return (
        round(x * width / ref_width),
        round(y * height / ref_height),
        round(w * width / ref_width),
        round(h * height / ref_height),
    )


def _box_tuple(box) -> tuple[int, int, int, int]:
    return int(box.x), int(box.y), int(box.width), int(box.height)


def _context_ocr_items(context, node_name: str, frame) -> list[OcrLike]:
    from qjnn_alliance_commission import OcrItem, Rect

    detail = context.run_recognition(node_name, frame)
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
            OcrItem(
                str(result.text),
                rect,
                float(getattr(result, "score", 0.0)),
            )
        )
    return converted


def _box_intersects_rect(box, rect: tuple[int, int, int, int]) -> bool:
    return rects_intersect(_box_tuple(box), rect)


def _find_ocr(
    items: Sequence[OcrLike],
    text: str,
    roi: tuple[int, int, int, int],
) -> OcrLike | None:
    target = _normalize_text(text)
    for item in items:
        if float(item.score) < COMMON_POPUP_OCR_THRESHOLD:
            continue
        if not _box_intersects_rect(item.box, roi):
            continue
        if target in _normalize_text(item.text):
            return item
    return None


def _light_card_score(frame, popup_rect: tuple[int, int, int, int]) -> float:
    import cv2
    import numpy as np

    x, y, width, height = popup_rect
    probe = frame[y : y + height, x : x + width]
    if probe.size == 0:
        return 0.0
    hsv = cv2.cvtColor(probe, cv2.COLOR_BGR2HSV)
    light_low_saturation = (hsv[:, :, 1] <= 80) & (hsv[:, :, 2] >= 185)
    return float(np.mean(light_low_saturation))


def _information_notice(frame, items):
    """Informational cards only: no purchase, deletion or choice confirmation."""
    import cv2
    height, width = frame.shape[:2]
    center = _scaled_rect((80, 280, 560, 760), width, height)
    foreground = [i for i in items if i.score >= COMMON_POPUP_OCR_THRESHOLD
                  and rect_contains(center, _box_tuple(i.box))]
    body = "".join(_normalize_text(i.text) for i in foreground)
    if re.search(r"购买|支付|花费|消耗|钻石|删除|分解|重置|清空|补签|充值|兑换|解散|退出|绑定|取消", body):
        return None
    tutorial = any("搭配竞技场新功能上线" in _normalize_text(i.text) for i in foreground)
    heading = next((i for i in foreground if re.sub(r"[!！.。]+$", "", _normalize_text(i.text)) in
                    {"提示", "温馨提示", "说明", "功能说明", "通知", "新功能开启", "功能解锁"}), None)
    confirms = [i for i in foreground if _normalize_text(i.text) in {"确定", "确认", "知道了", "我知道了"}]
    if not tutorial and heading is None and not confirms:
        return None
    if len(confirms) > 1:
        return None
    # Locate a central light card, independent of its changing notice text.
    normalized = cv2.resize(frame, COMMON_POPUP_REFERENCE_SIZE)
    hsv = cv2.cvtColor(normalized, cv2.COLOR_BGR2HSV)
    mask = ((hsv[:, :, 1] <= 80) & (hsv[:, :, 2] >= 185)).astype("uint8") * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9)))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cards = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if (50 <= x and x + w <= 670 and 230 <= y and y + h <= 1080
                and 320 <= w <= 620 and 170 <= h <= 800
                and cv2.contourArea(contour) / (w * h) >= .75):
            cards.append(_scaled_rect((x, y, w, h), width, height))
    # The first-run Arena explanation is also proved by its complete stable
    # introduction and sole acknowledgement; never depend on the topic/name.
    if tutorial and confirms:
        cards.append(_scaled_rect((110, 430, 500, 430), width, height))
    required = [i for i in foreground if tutorial and "搭配竞技场新功能上线" in _normalize_text(i.text)]
    if heading is not None:
        required.append(heading)
    required += confirms
    cards = [card for card in cards if all(rect_contains(card, _box_tuple(i.box)) for i in required)]
    if not cards:
        return None
    popup = min(cards, key=lambda card: card[2] * card[3])
    if confirms:
        box = _box_tuple(confirms[0].box)
        inset = max(1, min(box[2:]) // 5)
        safe = (box[0] + inset, box[1] + inset, box[2] - inset * 2, box[3] - inset * 2)
        kind = CommonPopupKind.NOTICE_CONFIRM
    else:
        # Only a structurally confirmed information card without action/choice
        # controls can be dismissed below its actual bounds.
        if re.search(r"前往|领取|开始|分享|同意|拒绝|使用|选择", body):
            return None
        y = popup[1] + popup[3] + round(height * .02)
        if y + round(height * .04) >= height * .94:
            return None
        safe = (round(width * .40), y, round(width * .20), round(height * .04))
        kind = CommonPopupKind.NOTICE_OUTSIDE
    title = heading or (confirms[0] if confirms else next(
        i for i in foreground if "搭配竞技场新功能上线" in _normalize_text(i.text)))
    return CommonPopupDetection(kind, True, True, _light_card_score(frame, popup), popup,
                                safe, (0, 0, 0, 0), str(title.text), float(title.score), _box_tuple(title.box))


def detect_common_safe_popup(
    frame,
    items: Sequence[OcrLike],
) -> CommonPopupDetection:
    if frame is None or getattr(frame, "size", 0) == 0:
        return CommonPopupDetection(
            CommonPopupKind.NONE,
            False,
            False,
            0.0,
            COMMON_ACHIEVEMENT_POPUP_RECT,
            COMMON_ACHIEVEMENT_SAFE_RECT,
            COMMON_ACHIEVEMENT_SHARE_FORBIDDEN_RECT,
        )
    height, width = frame.shape[:2]
    popup_rect = _scaled_rect(COMMON_ACHIEVEMENT_POPUP_RECT, width, height)
    safe_rect = _scaled_rect(COMMON_ACHIEVEMENT_SAFE_RECT, width, height)
    forbidden_rect = _scaled_rect(
        COMMON_ACHIEVEMENT_SHARE_FORBIDDEN_RECT, width, height
    )
    title_roi = _scaled_rect(COMMON_ACHIEVEMENT_TITLE_ROI, width, height)
    title = _find_ocr(items, "新成就达成", title_roi)
    skill_title = _find_ocr(
        items, "学会新技能",
        _scaled_rect(COMMON_SKILL_UNLOCK_TITLE_ROI, width, height),
    )
    if skill_title is not None:
        popup_rect = _scaled_rect(COMMON_SKILL_UNLOCK_POPUP_RECT, width, height)
        return CommonPopupDetection(
            CommonPopupKind.SKILL_UNLOCK, True, True,
            _light_card_score(frame, popup_rect), popup_rect,
            _scaled_rect(COMMON_SKILL_UNLOCK_SAFE_RECT, width, height),
            popup_rect, str(skill_title.text), float(skill_title.score),
            _box_tuple(skill_title.box),
        )
    share = _find_ocr(items, "分享", forbidden_rect)
    reward = _find_ocr(items, "奖励", popup_rect)
    structure_score = _light_card_score(frame, popup_rect)
    structure_present = structure_score >= COMMON_ACHIEVEMENT_STRUCTURE_THRESHOLD

    # The stable title is the whitelist. Achievement names, stages and rewards
    # are dynamic and must never participate in popup classification.
    if title is not None:
        kind = CommonPopupKind.ACHIEVEMENT
    elif _find_ocr(
        items, "过关完成", _scaled_rect((150, 650, 420, 170), width, height),
    ) is not None:
        # Closing a skill card reveals this business-owned settlement card.
        # Its “获得奖励” text must not turn it into an unknown common popup.
        kind = CommonPopupKind.NONE
    elif structure_present and (share is not None or reward is not None):
        # Popup-like structure, but not a whitelisted type. Block it without input.
        kind = CommonPopupKind.UNKNOWN
    else:
        kind = CommonPopupKind.NONE
    if title is None:
        notice = _information_notice(frame, items)
        if notice is not None:
            return notice
    return CommonPopupDetection(
        kind,
        kind is CommonPopupKind.ACHIEVEMENT,
        title is not None,
        structure_score,
        popup_rect,
        safe_rect,
        forbidden_rect,
        str(title.text) if title is not None else None,
        float(title.score) if title is not None else None,
        _box_tuple(title.box) if title is not None else None,
        _box_tuple(share.box) if share is not None else None,
        tuple(sorted(_normalize_text(i.text) for i in items
                     if title is not None and i.score >= COMMON_POPUP_OCR_THRESHOLD
                     and rect_contains(_scaled_rect((100, 450, 530, 190), width, height), _box_tuple(i.box)))),
    )


def validate_common_popup_geometry(detection: CommonPopupDetection | None = None) -> bool:
    if detection is not None and detection.kind in {CommonPopupKind.NOTICE_CONFIRM, CommonPopupKind.NOTICE_OUTSIDE}:
        return (min(detection.safe_rect[2:]) > 0 and
                (rect_contains(detection.popup_rect, detection.safe_rect)
                 if detection.kind is CommonPopupKind.NOTICE_CONFIRM else
                 not rects_intersect(detection.popup_rect, detection.safe_rect)))
    if detection is not None and detection.kind is CommonPopupKind.SKILL_UNLOCK:
        return (
            all(value > 0 for value in detection.safe_rect[2:])
            and not rects_intersect(detection.popup_rect, detection.safe_rect)
            and not rects_intersect(detection.safe_rect, detection.share_forbidden_rect)
        )
    if not rect_contains(
        COMMON_ACHIEVEMENT_POPUP_RECT, COMMON_ACHIEVEMENT_SAFE_RECT
    ):
        return False
    if rects_intersect(
        COMMON_ACHIEVEMENT_SAFE_RECT,
        COMMON_ACHIEVEMENT_SHARE_FORBIDDEN_RECT,
    ):
        return False
    return True


_common_popup_capture: Callable[[str, object], bool] | None = None


def configure_common_popup_capture(
    capture: Callable[[str, object], bool] | None,
) -> None:
    global _common_popup_capture
    _common_popup_capture = capture


class CommonPopupGuard:
    def __init__(
        self,
        *,
        capture: Callable[[str, object], bool] | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.dismiss_count = 0
        self.capture = capture if capture is not None else _common_popup_capture
        self.log = log
        self._achievement_identity = None
        self._achievement_repeats = 0

    def dismiss_limit(self, detection):
        return (MAX_ACHIEVEMENT_POPUP_DISMISS
                if detection.kind is CommonPopupKind.ACHIEVEMENT else MAX_COMMON_POPUP_DISMISS)

    def inspect_and_handle(
        self,
        frame,
        items: Sequence[OcrLike],
        click: Callable[[int, int], bool | None],
    ) -> bool:
        detection = detect_common_safe_popup(frame, items)
        if detection.kind is CommonPopupKind.NONE:
            return False
        if detection.kind is CommonPopupKind.UNKNOWN:
            self.log("[CommonPopupGuard] unknown popup; no click")
            if self.capture is not None:
                self.capture("QJNN_CommonPopupUnknown", frame)
            raise CommonPopupUnknownError("unknown common popup; zero-click SafeStop")
        if self.dismiss_count >= self.dismiss_limit(detection):
            raise CommonPopupLimitError("common popup dismiss limit reached")
        if detection.kind is CommonPopupKind.ACHIEVEMENT:
            if self._achievement_identity != detection.achievement_identity:
                self._achievement_identity = detection.achievement_identity
                self._achievement_repeats = 0
            if self._achievement_repeats >= MAX_COMMON_POPUP_DISMISS:
                raise CommonPopupLimitError("same achievement remained after the finite click limit")
        if not validate_common_popup_geometry(detection):
            raise CommonPopupHandlingError("invalid common popup safe geometry")
        safe = detection.safe_rect
        forbidden = detection.share_forbidden_rect
        if rects_intersect(safe, forbidden):
            raise CommonPopupHandlingError("safe rect intersects ShareForbiddenRect")
        click_point = (
            (random.randrange(safe[0], safe[0] + safe[2]),
             random.randrange(safe[1], safe[1] + safe[3]))
            if detection.kind is CommonPopupKind.SKILL_UNLOCK
            else (safe[0] + safe[2] // 2, safe[1] + safe[3] // 2)
        )
        capture_state = (
            "QJNN_CommonSkillUnlockPopup"
            if detection.kind is CommonPopupKind.SKILL_UNLOCK
            else "QJNN_CommonAchievementPopup"
        )
        if detection.kind in {CommonPopupKind.NOTICE_CONFIRM, CommonPopupKind.NOTICE_OUTSIDE}:
            capture_state = "QJNN_CommonInformationPopup"
        if detection.share_box is not None:
            if rects_intersect(safe, detection.share_box):
                raise CommonPopupHandlingError("SafeRect intersects OCR share box")
        self.log(
            "[CommonPopupGuard] detected: "
            f"kind={detection.kind.name}, text={detection.title_text!r}, "
            f"confidence={detection.title_confidence!r}, box={detection.title_box!r}"
        )
        if self.capture is not None:
            self.capture(capture_state, frame)
            click_map_saved = self.capture(
                capture_state + "ClickMap",
                render_common_popup_click_map(frame, detection, click_point=click_point),
            )
            if not click_map_saved:
                self.log(
                    f"[CommonPopupCapture] ERROR: {detection.kind.value} click_map "
                    "was not saved before click"
                )
        self.log(
            "[CommonPopupGuard] click requested: "
            f"SafeRect={safe}, point={click_point}"
        )
        submitted = click(*click_point)
        if submitted is False:
            raise CommonPopupHandlingError(
                f"common popup click was not submitted: {click_point}"
            )
        self.dismiss_count += 1
        if detection.kind is CommonPopupKind.ACHIEVEMENT:
            self._achievement_repeats += 1
        self.log(
            f"[CommonPopup] {detection.kind.value} dismiss click: "
            f"{self.dismiss_count}/{self.dismiss_limit(detection)}"
        )
        return True

    def dismiss_and_wait(
        self,
        session,
        frame,
        items: Sequence[OcrLike],
    ) -> tuple[bool, int]:
        handled = self.inspect_and_handle(frame, items, session.click)
        if not handled:
            return False, 0
        kind = detect_common_safe_popup(frame, items).kind.value
        dismissed_detection = detect_common_safe_popup(frame, items)
        total_polls = 0
        while True:
            disappeared, polls, last_frame, last_detection = (
                wait_for_popup_disappear(session, log=self.log, dismissed_detection=dismissed_detection)
            )
            total_polls += polls
            if last_detection.kind is CommonPopupKind.UNKNOWN:
                if self.capture is not None:
                    self.capture("QJNN_CommonPopupUnknown", last_frame)
                raise CommonPopupUnknownError(
                    "unknown common popup appeared while waiting; zero additional clicks"
                )
            if disappeared:
                self.log(
                    f"[CommonPopup] {kind} dismissed successfully after "
                    f"{self.dismiss_count} safe click(s)"
                )
                return True, total_polls

            if not _retry_presence_confirmed(last_detection):
                raise CommonPopupDisappearTimeout(
                    "popup presence uncertain after disappearance wait; no additional clicks"
                )

            # Never click the frame returned by the wait. The popup can close
            # between the last observation and retry eligibility.
            session.sleep(COMMON_POPUP_POLL_INTERVAL_SECONDS)
            retry_frame = session.capture_frame()
            retry_items = session.recognize_text(retry_frame)
            retry_detection = detect_common_safe_popup(retry_frame, retry_items)
            if retry_detection.kind is CommonPopupKind.NONE:
                return True, total_polls + 1
            if (retry_frame is last_frame
                    or not _retry_presence_confirmed(retry_detection)
                    or not _same_stable_popup(last_frame, retry_frame, last_detection, retry_detection)):
                raise CommonPopupDisappearTimeout(
                    "popup changed or capture not fresh before retry; no additional clicks"
                )

            # A full disappearance window and a new pre-click capture are
            # mandatory; the geometry only protects input while the card exists.
            if self.dismiss_count >= self.dismiss_limit(last_detection):
                if self.capture is not None:
                    self.capture(
                        ("QJNN_CommonSkillUnlockPopupDismissFailure"
                         if last_detection.kind is CommonPopupKind.SKILL_UNLOCK
                         else "QJNN_CommonAchievementPopupDismissFailure"), last_frame
                    )
                raise CommonPopupLimitError(
                    "common popup remained after the finite safe-click limit"
                )
            def confirmed_retry_click(x, y):
                # Diagnostics/click-map saving above can take time. Recheck after
                # that work as well, immediately before submitting input.
                final_frame = session.capture_frame()
                final_items = session.recognize_text(final_frame)
                final_detection = detect_common_safe_popup(final_frame, final_items)
                if final_detection.kind is CommonPopupKind.NONE:
                    raise _PopupAlreadyDisappeared()
                if (final_frame is retry_frame
                        or not _retry_presence_confirmed(final_detection)
                        or not _same_stable_popup(retry_frame, final_frame, retry_detection, final_detection)
                        or final_detection.share_box is not None
                        and rects_intersect(final_detection.safe_rect, final_detection.share_box)):
                    raise CommonPopupDisappearTimeout(
                        "popup not stable immediately before retry; no additional clicks"
                    )
                return session.click(x, y)

            try:
                self.inspect_and_handle(retry_frame, retry_items, confirmed_retry_click)
                dismissed_detection = retry_detection
            except _PopupAlreadyDisappeared:
                return True, total_polls + 2


def get_common_popup_guard(
    session,
    *,
    log: Callable[[str], None] = print,
) -> CommonPopupGuard:
    guard = getattr(session, "_qjnn_common_popup_guard", None)
    if guard is None:
        guard = CommonPopupGuard(log=log)
        setattr(session, "_qjnn_common_popup_guard", guard)
    return guard


def _retry_presence_confirmed(detection):
    # OCR alone can survive a fading card. Re-clicks require the full card too.
    return (detection.matched
            and detection.structure_score >= COMMON_ACHIEVEMENT_STRUCTURE_THRESHOLD)


def _same_stable_popup(previous, current, before, after):
    import numpy as np

    if (previous.shape != current.shape or before.kind is not after.kind
            or before.title_text != after.title_text):
        return False
    if after.kind is CommonPopupKind.ACHIEVEMENT:
        # Confetti, glowing rewards and successive achievement names change
        # pixels continuously. The fresh title and card geometry identify the
        # same safe dismissal operation, independent of those animations.
        tolerance = max(2, round(8 * current.shape[1] / COMMON_POPUP_REFERENCE_SIZE[0]))
        return (before.title_box is not None and after.title_box is not None
                and all(abs(a-b) <= tolerance for a, b in zip(before.title_box, after.title_box))
                and _retry_presence_confirmed(before) and _retry_presence_confirmed(after)
                and before.safe_rect == after.safe_rect
                and (after.share_box is None or not rects_intersect(after.safe_rect, after.share_box)))
    if before.title_box != after.title_box:
        return False
    x, y, w, h = after.popup_rect
    old = previous[y:y+h, x:x+w].astype("float32")
    new = current[y:y+h, x:x+w].astype("float32")
    return bool(old.size and np.mean(np.abs(old - new)) <= COMMON_POPUP_STABLE_PIXEL_DELTA)


def wait_for_popup_disappear(
    session,
    *,
    timeout: float = COMMON_POPUP_DISAPPEAR_TIMEOUT_SECONDS,
    interval: float = COMMON_POPUP_POLL_INTERVAL_SECONDS,
    log: Callable[[str], None] = print,
    dismissed_detection: CommonPopupDetection | None = None,
) -> tuple[bool, int, object, CommonPopupDetection]:
    if timeout <= 0 or interval <= 0:
        raise ValueError("popup disappear timeout and interval must be positive")
    started = session.monotonic()
    deadline = started + timeout
    polls = 0
    confirmed_present_polls = 0
    last_frame = None
    last_detection = detect_common_safe_popup(None, [])
    previous_frame = None
    previous_detection = None
    while session.monotonic() < deadline:
        remaining = deadline - session.monotonic()
        session.sleep(min(interval, remaining))
        # Every poll posts a fresh screencap through the active session/controller.
        last_frame = session.capture_frame()
        items = session.recognize_text(last_frame)
        polls += 1
        detection = detect_common_safe_popup(last_frame, items)
        last_detection = detection
        if detection.kind is CommonPopupKind.NONE:
            log(f"[CommonPopupDebug] popup disappeared after {polls} polls")
            return True, polls, last_frame, detection
        if detection.kind is CommonPopupKind.UNKNOWN:
            return False, polls, last_frame, detection
        stable = (session.monotonic() - started >= COMMON_POPUP_ANIMATION_SETTLE_SECONDS
                  and _retry_presence_confirmed(detection)
                  and previous_frame is not None and last_frame is not previous_frame
                  and _same_stable_popup(previous_frame, last_frame, previous_detection, detection))
        confirmed_present_polls = confirmed_present_polls + 1 if stable else 0
        previous_frame, previous_detection = last_frame, detection
        if (confirmed_present_polls >= COMMON_POPUP_RECLICK_CONFIRM_POLLS
                and dismissed_detection is not None
                and dismissed_detection.kind is CommonPopupKind.ACHIEVEMENT
                and detection.kind is CommonPopupKind.ACHIEVEMENT
                and detection.achievement_identity
                and detection.achievement_identity != dismissed_detection.achievement_identity):
            log("[CommonPopupDebug] next achievement card confirmed on settled fresh frames")
            return False, polls, last_frame, detection
        # Always use the entire finite disappearance window. Two early frames
        # can both belong to the closing animation, not a stuck popup.
    if confirmed_present_polls < COMMON_POPUP_RECLICK_CONFIRM_POLLS:
        raise CommonPopupDisappearTimeout(
            "popup did not disappear and stable presence is unconfirmed; no additional clicks"
        )
    log(f"[CommonPopupDebug] popup remained stable throughout wait; confirmations={confirmed_present_polls}")
    return False, polls, last_frame, last_detection


def poll_target_with_common_popups(
    session,
    selector: Callable[[Sequence[OcrLike], object], object | None],
    *,
    timeout: float,
    interval: float,
    loading_classifier: Callable[[Sequence[OcrLike], object], bool] | None = None,
    loading_timeout: float = 20.0,
    loading_interval: float = 0.5,
    loading_timeout_state: str = "QJNN_GlobalLoadingTimeout",
    loading_capture: Callable[[str, object], bool] | None = None,
    on_timeout: Callable[[object, Sequence[OcrLike]], None] | None = None,
    log: Callable[[str], None] = print,
):
    if timeout <= 0 or interval <= 0:
        raise ValueError("poll timeout and interval must be positive")
    attempts = max(1, math.ceil(timeout / interval) + 1)
    deadline = session.monotonic() + timeout
    last_frame = None
    last_items: Sequence[OcrLike] = []
    pending_snapshot = None
    for _ in range(attempts):
        if pending_snapshot is None:
            last_frame = session.capture_frame()
            last_items = session.recognize_text(last_frame)
        else:
            last_frame, last_items = pending_snapshot
            pending_snapshot = None
        # These overlays expose clickable business controls underneath them.
        # Dismiss the modal before trusting any of those background controls.
        blocking_overlay = (
            detect_common_safe_popup(last_frame, last_items).kind
            in {CommonPopupKind.ACHIEVEMENT, CommonPopupKind.SKILL_UNLOCK,
                CommonPopupKind.NOTICE_CONFIRM, CommonPopupKind.NOTICE_OUTSIDE}
        )
        value = None
        if not blocking_overlay:
            value = selector(last_items, last_frame)
        if value is not None:
            return last_frame, last_items, value
        transient, fresh_frame, fresh_items = handle_transient_if_present(
            session,
            last_frame,
            last_items,
            loading_classifier=loading_classifier,
            loading_timeout=loading_timeout,
            loading_interval=loading_interval,
            loading_timeout_state=loading_timeout_state,
            loading_capture=loading_capture,
            log=log,
        )
        if transient in {
            GlobalTransientResult.HANDLED_POPUP,
            GlobalTransientResult.WAITED_LOADING,
        }:
            last_frame, last_items = fresh_frame, fresh_items
            value = selector(last_items, last_frame)
            if value is not None:
                return last_frame, last_items, value
            pending_snapshot = (fresh_frame, fresh_items)
            continue
        if transient is GlobalTransientResult.ERROR:
            last_frame, last_items = fresh_frame, fresh_items
            break
        remaining = deadline - session.monotonic()
        if remaining <= 0:
            break
        session.sleep(min(interval, remaining))
    if on_timeout is not None and last_frame is not None:
        on_timeout(last_frame, last_items)
    return None


def handle_transient_if_present(
    session,
    frame,
    items: Sequence[OcrLike],
    *,
    loading_classifier: Callable[[Sequence[OcrLike], object], bool] | None = None,
    loading_timeout: float = 20.0,
    loading_interval: float = 0.5,
    loading_timeout_state: str = "QJNN_GlobalLoadingTimeout",
    capture: Callable[[str, object], bool] | None = None,
    loading_capture: Callable[[str, object], bool] | None = None,
    log: Callable[[str], None] = print,
) -> tuple[GlobalTransientResult, object | None, Sequence[OcrLike]]:
    """Handle only a strict transient after the caller's business miss.

    Callers must run their own legal-page selector first.  All captures here use
    the raw supplied session, so the guard cannot recurse through a wrapper.
    """

    detection = detect_common_safe_popup(frame, items)
    if detection.matched:
        guard = get_common_popup_guard(session, log=log)
        if capture is not None and guard.capture is None:
            guard.capture = capture
        try:
            polls = 0
            while True:
                handled, current_polls = guard.dismiss_and_wait(session, frame, items)
                polls += current_polls
                if not handled:
                    return GlobalTransientResult.ERROR, frame, items
                fresh_frame = session.capture_frame()
                fresh_items = session.recognize_text(fresh_frame)
                next_popup = detect_common_safe_popup(fresh_frame, fresh_items)
                if next_popup.kind is CommonPopupKind.NONE:
                    break
                if next_popup.kind is not CommonPopupKind.ACHIEVEMENT:
                    log("[GlobalTransientGuard] unconfirmed popup in handoff capture; no additional clicks")
                    return GlobalTransientResult.ERROR, fresh_frame, fresh_items
                # A second achievement can arrive between clearing one card
                # and handing control back. Require a separate settled capture
                # before treating it as another dismissible card.
                session.sleep(COMMON_POPUP_ANIMATION_SETTLE_SECONDS)
                frame = session.capture_frame()
                items = session.recognize_text(frame)
                settled = detect_common_safe_popup(frame, items)
                if settled.kind is CommonPopupKind.NONE:
                    fresh_frame, fresh_items = frame, items
                    break
                if (frame is fresh_frame or not _retry_presence_confirmed(settled)
                        or not _same_stable_popup(fresh_frame, frame, next_popup, settled)):
                    raise CommonPopupDisappearTimeout("next achievement is not confirmed on a fresh frame")
                log("[GlobalTransientGuard] next achievement confirmed; continuing dismissal")
        except CommonPopupHandlingError as exc:
            log(f"[GlobalTransientGuard] {detection.kind.value} error: {exc}")
            return GlobalTransientResult.ERROR, frame, items
        if not handled:
            return GlobalTransientResult.ERROR, frame, items
        log(
            f"[GlobalTransientGuard] {detection.kind.value} cleared; "
            f"fresh-frame polls={polls}; retrying business recognition"
        )
        return GlobalTransientResult.HANDLED_POPUP, fresh_frame, fresh_items

    if loading_classifier is not None and loading_classifier(items, frame):
        if loading_timeout <= 0 or loading_interval <= 0:
            raise ValueError("loading timeout and interval must be positive")
        deadline = session.monotonic() + loading_timeout
        log(
            "[GlobalTransientGuard] confirmed loading; zero clicks while waiting "
            f"up to {loading_timeout:.1f}s"
        )
        last_frame, last_items = frame, items
        while session.monotonic() < deadline:
            remaining = deadline - session.monotonic()
            session.sleep(min(loading_interval, remaining))
            last_frame = session.capture_frame()
            last_items = session.recognize_text(last_frame)
            if loading_classifier(last_items, last_frame):
                continue
            fresh_frame = session.capture_frame()
            fresh_items = session.recognize_text(fresh_frame)
            if loading_classifier(fresh_items, fresh_frame):
                last_frame, last_items = fresh_frame, fresh_items
                continue
            log(
                "[GlobalTransientGuard] loading disappeared; "
                "retrying business recognition with a fresh frame"
            )
            return GlobalTransientResult.WAITED_LOADING, fresh_frame, fresh_items
        timeout_capture = loading_capture if loading_capture is not None else capture
        if timeout_capture is not None:
            timeout_capture(loading_timeout_state, last_frame)
        log("[GlobalTransientGuard] loading exceeded the finite timeout")
        return GlobalTransientResult.ERROR, last_frame, last_items

    # Popup-like appearance without a whitelisted title is not a
    # global transient.  Business reward pages must remain owned by business.
    return GlobalTransientResult.NOT_TRANSIENT, frame, items


def log_common_popup_diagnostics(
    detection: CommonPopupDetection,
    frame,
    *,
    log: Callable[[str], None] = print,
) -> None:
    height, width = frame.shape[:2]
    log(f"[CommonPopupDebug] frame={width}x{height}")
    log(
        "[CommonPopupDebug] popup title: "
        f"text={detection.title_text!r}, "
        f"confidence={detection.title_confidence!r}, "
        f"box={detection.title_box!r}"
    )
    log(f"[CommonPopupDebug] PopupRect={detection.popup_rect}")
    log(f"[CommonPopupDebug] SafeRect={detection.safe_rect}")
    log(
        "[CommonPopupDebug] ShareForbiddenRect="
        f"{detection.share_forbidden_rect}, share_box={detection.share_box!r}"
    )
    log(
        "[CommonPopupDebug] safe/share overlap="
        f"{bool(detection.share_box and rects_intersect(detection.safe_rect, detection.share_box))}"
    )


def render_common_popup_click_map(frame, detection: CommonPopupDetection, *, click_point=None):
    """Draw debug-only screenshot-space geometry without changing click logic."""
    import cv2

    annotated = frame.copy()
    click_point = click_point or (
        detection.safe_rect[0] + detection.safe_rect[2] // 2,
        detection.safe_rect[1] + detection.safe_rect[3] // 2,
    )

    def draw_rect(rect, color, label):
        x, y, width, height = rect
        cv2.rectangle(
            annotated,
            (x, y),
            (x + width, y + height),
            color,
            3,
        )
        cv2.putText(
            annotated,
            label,
            (x + 4, max(24, y - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            color,
            2,
            cv2.LINE_AA,
        )

    draw_rect(detection.popup_rect, (255, 255, 0), "PopupRect")
    draw_rect(detection.safe_rect, (0, 255, 0), "SafeRect")
    draw_rect(
        detection.share_forbidden_rect,
        (0, 0, 255),
        "ShareForbiddenRect",
    )
    cv2.drawMarker(
        annotated,
        click_point,
        (255, 0, 255),
        cv2.MARKER_CROSS,
        30,
        4,
    )
    height, width = frame.shape[:2]
    cv2.putText(
        annotated,
        f"frame={width}x{height} controller.post_click={click_point}",
        (12, 32),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.62,
        (255, 0, 255),
        2,
        cv2.LINE_AA,
    )
    return annotated


def run_common_popup_debug(
    session,
    *,
    capture: Callable[[str, object], bool],
    log: Callable[[str], None] = print,
) -> bool:
    frame = session.capture_frame()
    items = session.recognize_text(frame)
    detection = detect_common_safe_popup(frame, items)
    log_common_popup_diagnostics(detection, frame, log=log)
    if detection.kind is CommonPopupKind.NONE:
        log("[CommonPopupDebug] current screen is not a whitelisted common popup")
        return False
    if detection.matched:
        display_info = getattr(session, "device_display_info", None)
        if callable(display_info):
            try:
                log(f"[CommonPopupDebug] {display_info()}")
            except Exception as exc:
                log(f"[CommonPopupDebug] device display info unavailable: {exc}")

    guard = CommonPopupGuard(capture=capture, log=log)
    try:
        handled, polls = guard.dismiss_and_wait(session, frame, items)
    except CommonPopupHandlingError as exc:
        log(f"[CommonPopupDebug] SafeStop: {exc}")
        return False
    if not handled:
        return False
    log(
        "[CommonPopupDebug] whitelisted popup disappeared after "
        f"{guard.dismiss_count} safe click(s) and {polls} fresh-frame polls"
    )
    return True


def run_common_popup_preflight(
    session,
    *,
    capture: Callable[[str, object], bool],
    log: Callable[[str], None] = print,
) -> bool:
    """Clear one whitelisted popup before the requested business task starts."""
    frame = session.capture_frame()
    items = session.recognize_text(frame)
    detection = detect_common_safe_popup(frame, items)
    if not detection.matched:
        log(
            "[CommonPopupPreflight] no common popup whitelist title present; "
            "submitting business task with zero clicks"
        )
        return True

    log_common_popup_diagnostics(detection, frame, log=log)
    guard = CommonPopupGuard(capture=capture, log=log)
    try:
        handled, polls = guard.dismiss_and_wait(session, frame, items)
    except CommonPopupHandlingError as exc:
        log(f"[CommonPopupPreflight] SafeStop: {exc}")
        return False
    if not handled:
        return False
    log(
        "[CommonPopupPreflight] whitelisted popup cleared before business task; "
        f"fresh-frame polls={polls}"
    )
    return True


def guard_runtime_session(context, session, *, log: Callable[[str], None] = print):
    """Compatibility hook: captures stay raw; business code owns fallback timing."""
    session._qjnn_transient_fallback_enabled = True
    return session
def _make_runtime_session(context):
    import time

    from qjnn_alliance_commission import OcrItem, Rect

    controller = context.tasker.controller
    ocr_frame = None
    ocr_items = None

    class RuntimeSession:
        @staticmethod
        def capture_frame():
            nonlocal ocr_frame, ocr_items
            # Reuse OCR only within one completed capture. Some controllers
            # reuse the image object, so every capture explicitly invalidates it.
            ocr_frame, ocr_items = None, None
            job = controller.post_screencap()
            if job is None:
                raise RuntimeError("common popup screenshot returned no job")
            job.wait()
            if not getattr(job, "succeeded", False):
                raise RuntimeError("common popup screenshot failed")
            cached = getattr(controller, "cached_image", None)
            frame = cached() if callable(cached) else cached
            if frame is None or getattr(frame, "size", 0) == 0:
                raise RuntimeError("common popup screenshot is empty")
            return frame

        @staticmethod
        def recognize_text(frame):
            nonlocal ocr_frame, ocr_items
            if frame is not ocr_frame or ocr_items is None:
                ocr_items = _context_ocr_items(context, "QJNN_AgentOcrBase", frame)
                ocr_frame = frame
            return list(ocr_items)

        @staticmethod
        def click(x: int, y: int) -> bool:
            try:
                job = controller.post_click(x, y)
                if job is None:
                    raise RuntimeError("controller.post_click returned no job")
                print(
                    "[CommonPopupController] click actually submitted: "
                    f"point=({x}, {y})"
                )
                job.wait()
                if hasattr(job, "succeeded") and not job.succeeded:
                    raise RuntimeError("controller click job did not succeed")
                print(
                    "[CommonPopupController] click completed: "
                    f"point=({x}, {y}), succeeded=True"
                )
                return True
            except Exception as exc:
                print(
                    "[CommonPopupController] click failure: "
                    f"point=({x}, {y}), error={exc}"
                )
                raise

        @staticmethod
        def device_display_info() -> str:
            from adb_controller import run_process

            info = controller.info
            if callable(info):
                info = info()
            adb_path = info.get("adb_path", "") if isinstance(info, dict) else ""
            serial = info.get("adb_serial", "") if isinstance(info, dict) else ""
            resolution = getattr(controller, "resolution", (0, 0))
            if callable(resolution):
                resolution = resolution()
            raw_width, raw_height = resolution
            if not adb_path or not serial:
                raise RuntimeError("Maa AdbController info has no adb_path/adb_serial")
            result = run_process(
                [adb_path, "-s", serial, "shell", "wm", "size"],
                timeout=8,
            )
            if result.returncode != 0:
                message = str(result.stderr or result.stdout or "unknown error").strip()
                raise RuntimeError(f"wm size failed: {message}")
            wm_size = " | ".join(
                line.strip()
                for line in str(result.stdout or "").splitlines()
                if line.strip()
            )
            return (
                f"Maa physical/raw size={raw_width}x{raw_height}; "
                f"wm size={wm_size or 'empty'}"
            )

        @staticmethod
        def monotonic() -> float:
            return time.monotonic()

        @staticmethod
        def sleep(seconds: float) -> None:
            time.sleep(seconds)

    runtime = RuntimeSession()
    runtime.controller = controller
    return guard_runtime_session(context, runtime)


class CommonPopupDebugAction(CustomAction):
    def __init__(self, capture_manager=None) -> None:
        super().__init__()
        self._capture_manager = capture_manager

    def run(self, context, argv) -> bool:
        try:
            if self._capture_manager is None:
                from qjnn_common_popup_capture import CommonPopupCaptureManager

                self._capture_manager = CommonPopupCaptureManager()
            return run_common_popup_debug(
                _make_runtime_session(context),
                capture=self._capture_manager.capture_once,
            )
        except Exception as exc:
            print(f"[CommonPopupDebug] failed: {exc}")
            return False


class CommonPopupPreflightAction(CustomAction):
    def __init__(self, capture_manager=None) -> None:
        super().__init__()
        self._capture_manager = capture_manager

    def run(self, context, argv) -> bool:
        try:
            if self._capture_manager is None:
                from qjnn_common_popup_capture import CommonPopupCaptureManager

                self._capture_manager = CommonPopupCaptureManager()
            return run_common_popup_preflight(
                _make_runtime_session(context),
                capture=self._capture_manager.capture_once,
            )
        except Exception as exc:
            print(f"[CommonPopupPreflight] failed: {exc}")
            return False


class CommonPopupRuntimeAction(CustomAction):
    """Dismiss a popup matched by the generated Pipeline jump-back guard."""

    def __init__(self) -> None:
        super().__init__()
        self._dismiss_count = 0

    def run(self, context, argv) -> bool:
        try:
            from qjnn_common_popup_capture import CommonPopupCaptureManager

            session = _make_runtime_session(context)
            frame = session.capture_frame()
            items = session.recognize_text(frame)
            manager = CommonPopupCaptureManager()
            guard = CommonPopupGuard(capture=manager.capture_once)
            guard.dismiss_count = self._dismiss_count
            handled, polls = guard.dismiss_and_wait(session, frame, items)
            self._dismiss_count = guard.dismiss_count
            if handled:
                print(
                    "[CommonPopupGuard] popup disappeared after "
                    f"{polls} fresh-frame polls; continuing original node"
                )
            return handled
        except Exception as exc:
            print(f"[CommonPopupGuard] runtime SafeStop: {exc}")
            return False
