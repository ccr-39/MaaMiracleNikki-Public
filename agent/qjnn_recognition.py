from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

from qjnn_home import (
    HOME_PAGE_ARROW_THRESHOLD,
    HOME_PAGE_LEFT_ROI,
    HOME_PAGE_REFERENCE_SHORT_SIDE,
    HOME_PAGE_REFERENCE_SIZE,
    HOME_PAGE_RIGHT_ROI,
    HOME_PAGE_TEMPLATE_ROOT,
    HomePageDetection,
    detect_home_page,
)
from qjnn_foreground import PackageForegroundRecognition
from qjnn_reward import RewardClothesRecognition, RewardSuitShowcaseRecognition
from qjnn_signin import SigninStateRecognition
from qjnn_limited_signin import LimitedSigninPageRecognition

try:
    from maa.agent.agent_server import AgentServer
    from maa.context import Context
    from maa.custom_recognition import CustomRecognition
except ImportError:  # MaaFramework is optional for the standalone template test.
    AgentServer = None


class TemplateMatchError(RuntimeError):
    """A screenshot or template could not be decoded."""


class ScreenState(str, Enum):
    START_SCREEN = "START_SCREEN"
    LOGIN_SCREEN = "LOGIN_SCREEN"
    HOME_SCREEN = "HOME_SCREEN"
    DAILY_SCREEN = "DAILY_SCREEN"
    REWARD_POPUP = "REWARD_POPUP"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class TemplateMatchResult:
    matched: bool
    confidence: float
    x: int
    y: int
    width: int
    height: int
    center_x: int
    center_y: int

    def to_dict(self) -> dict[str, bool | float | int]:
        return asdict(self)


@dataclass(frozen=True)
class ScreenDetection:
    state: ScreenState
    confidence: float
    template: str | None
    match: TemplateMatchResult | None

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "confidence": self.confidence,
            "template": self.template,
            "match": self.match.to_dict() if self.match is not None else None,
        }


# Only states backed by real, currently supplied templates participate in detection.
# Future page states are declared above but remain UNKNOWN until their templates exist.
SCREEN_TEMPLATES: tuple[tuple[ScreenState, str], ...] = (
    (ScreenState.START_SCREEN, "start/start_button.png"),
)

def _read_image(path: Path):
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise TemplateMatchError(
            "缺少 OpenCV。请运行：python -m pip install -r agent/requirements.txt"
        ) from exc

    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError as exc:
        raise TemplateMatchError(f"无法读取图片 {path}：{exc}") from exc
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise TemplateMatchError(f"图片读取失败或格式无效：{path}")
    return image


def match_template(
    screenshot_path: Path,
    template_path: Path,
    threshold: float = 0.85,
) -> TemplateMatchResult:
    """Find the best OpenCV template match and return a stable result shape."""
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold 必须在 0 到 1 之间")

    try:
        import cv2
    except ImportError as exc:
        raise TemplateMatchError(
            "缺少 OpenCV。请运行：python -m pip install -r agent/requirements.txt"
        ) from exc

    screenshot = _read_image(screenshot_path)
    template = _read_image(template_path)
    screen_h, screen_w = screenshot.shape[:2]
    height, width = template.shape[:2]
    if width > screen_w or height > screen_h:
        return TemplateMatchResult(False, 0.0, 0, 0, width, height, 0, 0)

    scores = cv2.matchTemplate(screenshot, template, cv2.TM_CCOEFF_NORMED)
    _, confidence, _, location = cv2.minMaxLoc(scores)
    x, y = int(location[0]), int(location[1])
    return TemplateMatchResult(
        matched=bool(confidence >= threshold),
        confidence=float(confidence),
        x=x,
        y=y,
        width=width,
        height=height,
        center_x=x + width // 2,
        center_y=y + height // 2,
    )


def detect_screen(
    image_path: Path,
    template_root: Path,
    threshold: float = 0.85,
) -> ScreenDetection:
    """Recognize a page through the single shared screen-state entry point."""
    best_state = ScreenState.UNKNOWN
    best_template: str | None = None
    best_match: TemplateMatchResult | None = None

    for state, template_name in SCREEN_TEMPLATES:
        template_path = template_root / template_name
        if not template_path.is_file():
            continue
        result = match_template(image_path, template_path, threshold)
        if best_match is None or result.confidence > best_match.confidence:
            best_state = state
            best_template = template_name
            best_match = result

    if best_match is None:
        return ScreenDetection(
            state=ScreenState.UNKNOWN,
            confidence=0.0,
            template=None,
            match=None,
        )
    return ScreenDetection(
        state=best_state if best_match.matched else ScreenState.UNKNOWN,
        confidence=best_match.confidence,
        template=best_template,
        match=best_match,
    )


if AgentServer is not None:

    from qjnn_privacy import PrivateLoginForegroundRecognition
    from qjnn_login import LoginScreenRecognition
    from qjnn_friend_send_heart import (
        FriendSendHeartResultRecognition,
        HomeAnyPageRecognition,
    )
    from qjnn_clothes_shop import ClothesShopStateRecognition

    AgentServer.custom_recognition("qjnn_home_any_page")(HomeAnyPageRecognition)

    AgentServer.custom_recognition("qjnn_friend_send_heart_result")(
        FriendSendHeartResultRecognition
    )

    AgentServer.custom_recognition("qjnn_clothes_shop_state")(
        ClothesShopStateRecognition
    )

    AgentServer.custom_recognition("qjnn_private_login_foreground")(
        PrivateLoginForegroundRecognition
    )
    AgentServer.custom_recognition("qjnn_login_screen")(LoginScreenRecognition)

    AgentServer.custom_recognition("qjnn_package_foreground")(
        PackageForegroundRecognition
    )

    AgentServer.custom_recognition("qjnn_reward_clothes_card")(
        RewardClothesRecognition
    )

    AgentServer.custom_recognition("qjnn_reward_suit_showcase")(
        RewardSuitShowcaseRecognition
    )

    AgentServer.custom_recognition("qjnn_signin_state")(SigninStateRecognition)
    AgentServer.custom_recognition("qjnn_limited_signin_page")(LimitedSigninPageRecognition)

    @AgentServer.custom_recognition("qjnn_first_text_in_rois")
    class FirstTextInRois(CustomRecognition):
        """依次在候选区域运行已有 OCR 节点，返回首次命中的区域。"""

        def analyze(
            self,
            context: Context,
            argv: CustomRecognition.AnalyzeArg,
        ) -> CustomRecognition.AnalyzeResult:
            param = json.loads(argv.custom_recognition_param or "{}")
            rois = param.get("candidate_rois", [])
            expected = param.get("expected", [])

            if not isinstance(rois, list) or not isinstance(expected, list):
                return CustomRecognition.AnalyzeResult(
                    box=None,
                    detail={"error": "candidate_rois 和 expected 必须为数组"},
                )

            for roi in rois:
                detail = context.run_recognition(
                    "QJNN_AgentOcrBase",
                    argv.image,
                    pipeline_override={
                        "QJNN_AgentOcrBase": {
                            "roi": roi,
                            "expected": expected,
                        }
                    },
                )
                if detail is not None and detail.hit:
                    return CustomRecognition.AnalyzeResult(
                        box=detail.best_result.box,
                        detail={"text": detail.best_result.text, "source_roi": roi},
                    )

            return CustomRecognition.AnalyzeResult(box=None, detail={"matched": False})
