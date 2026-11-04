from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path


class HomePageTemplateError(RuntimeError):
    """A home-page arrow template could not be decoded."""


@dataclass(frozen=True)
class HomePageDetection:
    """Result of detecting the current-theme home carousel page."""

    left: bool
    right: bool
    page: str
    left_score: float
    right_score: float
    threshold: float
    left_roi: tuple[int, int, int, int]
    right_roi: tuple[int, int, int, int]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOME_PAGE_TEMPLATE_ROOT = (
    PROJECT_ROOT / "assets" / "resource" / "image" / "home_pages" / "current_theme"
)
HOME_PAGE_REFERENCE_SIZE = (900, 1600)
HOME_PAGE_REFERENCE_SHORT_SIDE = 900
HOME_PAGE_ARROW_THRESHOLD = 0.75

# Reference coordinates are x, y, width, height on a 900 x 1600 screenshot.
# They cover only the fixed edge controls around y=1095. The red exclamation
# badges sit further inward and are deliberately outside these ROIs/templates.
HOME_PAGE_LEFT_ROI = (0, 1040, 70, 120)
HOME_PAGE_RIGHT_ROI = (830, 1040, 70, 120)
HOME_PAGE_ARROW_TEMPLATES = {
    "left": "left_arrow.png",
    "right": "right_arrow.png",
}


def _load_template(path: Path):
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise HomePageTemplateError(
            "缺少 OpenCV。请运行：python -m pip install -r agent/requirements.txt"
        ) from exc

    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError as exc:
        raise HomePageTemplateError(f"无法读取主页箭头模板 {path}：{exc}") from exc
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise HomePageTemplateError(f"主页箭头模板读取失败或格式无效：{path}")
    return image


def _scaled_roi(
    roi: tuple[int, int, int, int],
    frame_width: int,
    frame_height: int,
) -> tuple[int, int, int, int]:
    """Map a reference ROI to the current frame without assuming one resolution."""
    reference_width, reference_height = HOME_PAGE_REFERENCE_SIZE
    x, y, width, height = roi
    left = round(x * frame_width / reference_width)
    top = round(y * frame_height / reference_height)
    right = round((x + width) * frame_width / reference_width)
    bottom = round((y + height) * frame_height / reference_height)
    return left, top, max(1, right - left), max(1, bottom - top)


def _white_ui_mask(image):
    """Keep the stable white arrow strokes and discard colored backgrounds/badges."""
    import cv2
    import numpy as np

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return ((hsv[:, :, 1] <= 60) & (hsv[:, :, 2] >= 200)).astype(np.uint8) * 255


def _home_arrow_score(frame, template, roi: tuple[int, int, int, int]) -> float:
    import cv2

    frame_height, frame_width = frame.shape[:2]
    x, y, width, height = roi
    search = _white_ui_mask(frame[y : y + height, x : x + width])

    scale = min(frame_height, frame_width) / HOME_PAGE_REFERENCE_SHORT_SIDE
    if len(template.shape) == 3:
        template = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    template_height, template_width = template.shape[:2]
    scaled_width = max(1, round(template_width * scale))
    scaled_height = max(1, round(template_height * scale))
    candidate = cv2.resize(
        template,
        (scaled_width, scaled_height),
        interpolation=cv2.INTER_NEAREST,
    )
    if scaled_width > width or scaled_height > height:
        return 0.0

    scores = cv2.matchTemplate(search, candidate, cv2.TM_CCOEFF_NORMED)
    return float(cv2.minMaxLoc(scores)[1])


def detect_home_page(
    frame,
    *,
    template_root: Path = HOME_PAGE_TEMPLATE_ROOT,
    threshold: float = HOME_PAGE_ARROW_THRESHOLD,
) -> HomePageDetection:
    """Detect P1/P2/P3 from the two edge arrows; this function never clicks."""
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold 必须在 0 到 1 之间")
    if frame is None or getattr(frame, "size", 0) == 0:
        raise ValueError("frame 不能为空")
    if len(frame.shape) != 3 or frame.shape[2] != 3:
        raise ValueError("frame 必须是 BGR 三通道图像")

    frame_height, frame_width = frame.shape[:2]
    left_roi = _scaled_roi(HOME_PAGE_LEFT_ROI, frame_width, frame_height)
    right_roi = _scaled_roi(HOME_PAGE_RIGHT_ROI, frame_width, frame_height)
    templates = {
        side: _load_template(template_root / filename)
        for side, filename in HOME_PAGE_ARROW_TEMPLATES.items()
    }
    left_score = _home_arrow_score(frame, templates["left"], left_roi)
    right_score = _home_arrow_score(frame, templates["right"], right_roi)
    left = left_score >= threshold
    right = right_score >= threshold
    page = {
        (False, True): "P1",
        (True, True): "P2",
        (True, False): "P3",
        (False, False): "Unknown",
    }[(left, right)]
    return HomePageDetection(
        left=left,
        right=right,
        page=page,
        left_score=left_score,
        right_score=right_score,
        threshold=threshold,
        left_roi=left_roi,
        right_roi=right_roi,
    )
