from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Sequence

try:
    from maa.custom_recognition import CustomRecognition
except ImportError:  # MaaFramework is optional for pure offline tests.
    CustomRecognition = object


SIGNIN_OCR_THRESHOLD = 0.3
SIGNIN_PAGE_TEXT = "暖暖月签到"
SIGNIN_CLAIM_TEXT = "点击领取"
SIGNIN_REWARD_TITLES = ("恭喜获得", "获得奖励")


class SigninState(str, Enum):
    CLAIMABLE = "CLAIMABLE"
    CLAIMED = "CLAIMED"
    NOT_SIGNIN = "NOT_SIGNIN"


class OcrLike(Protocol):
    text: str
    box: object
    score: float


@dataclass(frozen=True)
class SigninStateDetection:
    state: SigninState
    page_item: OcrLike | None
    claim_item: OcrLike | None
    reward_item: OcrLike | None


def _normalize_text(value: str) -> str:
    return "".join(str(value).split())


def _find_text(
    items: Sequence[OcrLike],
    expected: Sequence[str],
) -> OcrLike | None:
    targets = tuple(_normalize_text(text) for text in expected)
    for item in items:
        if float(getattr(item, "score", 0.0)) < SIGNIN_OCR_THRESHOLD:
            continue
        text = _normalize_text(getattr(item, "text", ""))
        if any(target in text for target in targets):
            return item
    return None


def detect_signin_state(items: Sequence[OcrLike]) -> SigninStateDetection:
    """Classify mutually exclusive signin states from one fresh OCR frame."""
    page_item = _find_text(items, (SIGNIN_PAGE_TEXT,))
    claim_item = _find_text(items, (SIGNIN_CLAIM_TEXT,))
    reward_item = _find_text(items, SIGNIN_REWARD_TITLES)

    if page_item is None or reward_item is not None:
        state = SigninState.NOT_SIGNIN
    elif claim_item is not None:
        state = SigninState.CLAIMABLE
    else:
        state = SigninState.CLAIMED
    return SigninStateDetection(state, page_item, claim_item, reward_item)


def signin_close_target(items, frame):
    """Known calendar only; never dismiss an unknown purchase/reward dialog."""
    from qjnn_alliance_commission import Rect
    state = detect_signin_state(items)
    texts = [_normalize_text(i.text) for i in items if i.score >= .9]
    h, w = frame.shape[:2]
    # A newly rolled-over calendar has no claimed cells yet. Its reward grid
    # still identifies the owned calendar without authorizing a makeup click.
    rewards = [i for i in items if i.score >= .9
               and re.fullmatch(r'[0-9]+(?:金币|钻石|体力|星光币)', _normalize_text(i.text))
               and i.box.center[0] >= .25*w
               and .35*h <= i.box.center[1] <= .8*h]
    calendar_evidence = (sum(t in {'已领取', '点击领取', '立即补签'} for t in texts) >= 2
                         or ('立即补签' in texts and len(rewards) >= 3))
    if (state.page_item is None or state.page_item.score < .9 or state.reward_item is not None
            or any(t in {'确定', '确认', '取消', '补签确认', '确认补签'} for t in texts)
            or not calendar_evidence):
        return None
    return Rect(round(240*w/720),round(220*h/1280),round(240*w/720),round(48*h/1280))


def classify_signin_flags(
    *, page_present: bool, claim_present: bool, reward_present: bool
) -> SigninState:
    if not page_present or reward_present:
        return SigninState.NOT_SIGNIN
    return SigninState.CLAIMABLE if claim_present else SigninState.CLAIMED


def _box_values(item: OcrLike | None) -> list[int] | None:
    if item is None:
        return None
    box = item.box
    if all(hasattr(box, name) for name in ("x", "y", "w", "h")):
        return [int(box.x), int(box.y), int(box.w), int(box.h)]
    if all(hasattr(box, name) for name in ("x", "y", "width", "height")):
        return [int(box.x), int(box.y), int(box.width), int(box.height)]
    return [int(value) for value in list(box)[:4]]


class SigninStateRecognition(CustomRecognition):
    def analyze(self, context, argv):
        try:
            param = json.loads(argv.custom_recognition_param or "{}")
            expected_state = SigninState(str(param.get("state", "")))
            detail = context.run_recognition("QJNN_AgentOcrBase", argv.image)
            results = [] if detail is None else list(
                getattr(detail, "all_results", []) or []
            )
            if (
                not results
                and detail is not None
                and getattr(detail, "best_result", None) is not None
            ):
                results = [detail.best_result]

            detection = detect_signin_state(results)
            from qjnn_reward import detect_reward_clothes_card, detect_reward_suit_showcase
            if (detect_reward_clothes_card(argv.image).matched
                    or detect_reward_suit_showcase(argv.image).matched):
                return CustomRecognition.AnalyzeResult(
                    box=None, detail={"state": SigninState.NOT_SIGNIN.value,
                                      "reward_present": True})
            page_detail = None
            claim_detail = None
            if detection.page_item is None:
                page_detail = context.run_recognition(
                    "QJNN_SigninPageContext", argv.image
                )
            page_present = detection.page_item is not None or bool(
                page_detail is not None and page_detail.hit
            )
            # A claim fallback cannot turn a non-calendar frame into a signin
            # state. Keep both OCR/template fallbacks on confirmed calendars.
            if page_present and detection.claim_item is None:
                # Recognition-only call: QJNN_SigninClaim's Click action is not run.
                claim_detail = context.run_recognition("QJNN_SigninClaim", argv.image)
            claim_present = detection.claim_item is not None or bool(
                claim_detail is not None and claim_detail.hit
            )
            state = classify_signin_flags(
                page_present=page_present,
                claim_present=claim_present,
                reward_present=detection.reward_item is not None,
            )
            matched = state is expected_state
            target = (
                detection.claim_item
                if expected_state is SigninState.CLAIMABLE
                else detection.page_item
            )
            target_box = _box_values(target) if matched else None
            if matched and target_box is None:
                fallback = claim_detail if expected_state is SigninState.CLAIMABLE else page_detail
                if fallback is not None and getattr(fallback, "best_result", None) is not None:
                    target_box = _box_values(fallback.best_result)
            if matched:
                print(
                    "[SigninState] "
                    f"state={state.value}, "
                    f"page_box={_box_values(detection.page_item)}, "
                    f"claim_box={_box_values(detection.claim_item)}, "
                    "reward_present=False"
                )
            return CustomRecognition.AnalyzeResult(
                box=target_box,
                detail={
                    "state": state.value,
                    "expected_state": expected_state.value,
                    "matched": matched,
                    "page_box": _box_values(detection.page_item),
                    "claim_box": _box_values(detection.claim_item),
                    "reward_box": _box_values(detection.reward_item),
                },
            )
        except Exception as exc:
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={"state": SigninState.NOT_SIGNIN.value, "error": str(exc)},
            )
