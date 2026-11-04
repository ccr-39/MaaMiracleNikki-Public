from __future__ import annotations

from app_paths import DATA_ROOT

import re
import math
import random
import time
from dataclasses import dataclass
from contextlib import ExitStack
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Callable, Protocol

from qjnn_alliance_commission import OcrItem, Rect
from qjnn_common_popup import CommonPopupKind, detect_common_safe_popup, guard_runtime_session, poll_target_with_common_popups
from qjnn_home import detect_home_page
from qjnn_home_navigation import navigate_home_to_p3
from qjnn_home_random_visit import home_random_visit_loading_classifier
from qjnn_state_recovery import detect_entry_state
from qjnn_purchase_record import PurchaseJournal, coin_quote
from qjnn_purchase_safety import PurchaseEvidence, stable_purchase_click
from qjnn_run_state import RunRecord, run_record

try:
    from maa.custom_action import CustomAction
except ImportError:  # MaaFramework is optional for offline tests.
    CustomAction = object


REFERENCE_SIZE = (900, 1600)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DREAMWEAVER_PURCHASE_STATE = DATA_ROOT / "config" / "dreamweaver_purchase_state.json"
TOP_BACK_TEMPLATE = (
    PROJECT_ROOT / "assets" / "resource" / "image" / "alliance" / "top_back_arrow.png"
)

P3_ENTRY_ROI = (300, 800, 260, 210)
DREAMWEAVER_MENU_ROI = (120, 520, 350, 700)
FLOAT_ISLAND_ENTRY_ROI = (120, 1140, 360, 300)
COMMISSION_TITLE_ROI = (260, 300, 380, 150)
COMMISSION_SLOT_ROIS = (
    (40, 540, 810, 220),
    (40, 770, 810, 220),
    (40, 995, 810, 220),
)
OBTAIN_TITLE_ROI = (230, 350, 440, 170)
OBTAIN_SOURCE_ROI = (450, 650, 390, 470)
SHOP_OWNED_ROI = (100, 650, 300, 180)
SHOP_BUY_ROI = (500, 600, 340, 220)
PURCHASE_DIALOG_ROI = (150, 740, 600, 420)
PURCHASE_QUANTITY_ROI = (355, 850, 190, 90)
PURCHASE_QUANTITY_MIN_SCORE = 0.95
STAGE_DETAIL_ROI = (20, 700, 860, 700)
STAGE_ONCE_ROI = (20, 900, 860, 450)
STAGE_TARGET_OWNED_ROI = (40, 700, 380, 480)
STAGE_DROPS_ROI = (40, 700, 810, 480)
OBTAIN_TARGET_OWNED_ROI = (55, 650, 370, 510)
TOP_BACK_ROI = (0, 0, 150, 150)
CHAPTER_BACK_ROI = (0, 180, 150, 180)
FLOAT_ISLAND_TITLE_ROI = (230, 30, 440, 180)
FLOAT_ISLAND_LEFT_ACTION_ROI = (80, 1380, 310, 150)
FLOAT_ISLAND_LEFT_STATUS_ROI = (80, 1500, 310, 100)

# Legacy reference for diagnostics/tests only; never used to submit back input.
TOP_BACK_POINT = (70, 65)
POPUP_OUTSIDE_POINT = (450, 250)
REWARD_OUTSIDE_POINT = (450, 1350)
PURCHASE_PLUS_ONE_POINT = (580, 900)
MAX_POLL_ATTEMPTS = 5
MAX_COMMISSION_ENTRY_CLICKS = 3
MAX_REWARD_LAYERS = 5
MAX_FLOAT_ISLAND_REWARD_LAYERS = 5
FLOAT_ISLAND_REWARD_TIMEOUT_SECONDS = 12.0
FLOAT_ISLAND_REWARD_POLL_SECONDS = 0.5
FLOAT_ISLAND_REWARD_TITLE_ROI = (210, 300, 500, 150)
MAX_PRINCESS_STAGE_CLEARS = 3
MAX_MAIDEN_STAGE_CLEARS = 20
MAX_COMMISSION_STEPS = 16
MISSING_PLUS_THRESHOLD = 0.80
PAGE_WAIT_SECONDS = 0.8
POLL_WAIT_SECONDS = 0.5
PURCHASE_SETTLEMENT_TIMEOUT_SECONDS = 20.0
TOP_BACK_THRESHOLD = 0.80
DREAMWEAVER_PRE_CLICK_DELAY_MIN = 0.6
DREAMWEAVER_PRE_CLICK_DELAY_MAX = 0.9


class SlotState(str, Enum):
    COMPLETED = "completed"
    SUBMITTABLE = "submittable"
    CLOTHES_MISSING = "clothes_missing"
    UNKNOWN = "unknown"


class StageKind(str, Enum):
    PRINCESS = "princess"
    MAIDEN = "maiden"


class FloatIslandState(str, Enum):
    FREE = "free"
    PAID = "paid"
    UNKNOWN = "unknown"


class DreamweaverEntryState(str, Enum):
    HOME = "home"
    DREAMWEAVER_MAIN = "dreamweaver_main"
    COMMISSION_LIST = "commission_list"
    OBTAIN_PATH = "obtain_path"
    SHOP_PRODUCT = "shop_product"
    PURCHASE_DIALOG = "purchase_dialog"
    STAGE_DETAIL = "stage_detail"
    CHAPTER_MAP = "chapter_map"
    FLOAT_ISLAND = "float_island"
    REWARD = "reward"


SLOT_STATE_LABELS = {
    SlotState.COMPLETED: "已完成",
    SlotState.SUBMITTABLE: "可提交",
    SlotState.CLOTHES_MISSING: "衣服缺失",
    SlotState.UNKNOWN: "未知",
}


@dataclass(frozen=True)
class SlotSnapshot:
    index: int
    state: SlotState
    action_box: Rect | None


@dataclass(frozen=True)
class StageSnapshot:
    attempts: int | None
    owned: int | None
    once_box: Rect
    kind: StageKind | None = None
    multi_box: Rect | None = None  # Diagnostics only; never a click target.


@dataclass(frozen=True)
class AcquisitionResult:
    success: bool
    reason: str

    def __bool__(self):
        return self.success


@dataclass(frozen=True)
class FloatIslandSnapshot:
    state: FloatIslandState
    explore_once_box: Rect


@dataclass(frozen=True)
class FloatIslandResult:
    success: bool
    free_claimed: bool
    reason: str


@dataclass(frozen=True)
class DreamweaverCommissionResult:
    success: bool
    completed_slots: int
    reason: str


class CommissionSession(Protocol):
    def capture_frame(self): ...
    def recognize_text(self, frame) -> list[OcrItem]: ...
    def click(self, x: int, y: int) -> None: ...
    def sleep(self, seconds: float) -> None: ...
    def monotonic(self) -> float: ...
    def chapter_back_score(self, frame) -> float: ...


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("：", ":").replace("／", "/").replace("島", "岛")


def _scaled_roi(roi: tuple[int, int, int, int], frame) -> Rect:
    height, width = frame.shape[:2]
    x, y, w, h = roi
    return Rect(
        round(x * width / REFERENCE_SIZE[0]),
        round(y * height / REFERENCE_SIZE[1]),
        max(1, round(w * width / REFERENCE_SIZE[0])),
        max(1, round(h * height / REFERENCE_SIZE[1])),
    )


def _in_roi(box: Rect, roi: Rect) -> bool:
    x, y = box.center
    return roi.x <= x <= roi.right and roi.y <= y <= roi.bottom


def _items_in(items: list[OcrItem], frame, roi) -> list[OcrItem]:
    area = _scaled_roi(roi, frame)
    return [item for item in items if _in_roi(item.box, area)]


def _find(items: list[OcrItem], text: str, frame=None, roi=None) -> Rect | None:
    candidates = _items_in(items, frame, roi) if roi is not None else items
    candidates = [item for item in candidates if item.score >= 0.6]
    target = _normalize(text)
    exact = [item for item in candidates if target == _normalize(item.text)]
    if exact:
        return max(exact, key=lambda item: item.score).box
    matches = [item for item in candidates if target in _normalize(item.text)]
    return max(matches, key=lambda item: item.score).box if matches else None


def _parse_number(items: list[OcrItem], pattern: str) -> int | None:
    items = [item for item in items if item.score >= 0.6]
    regex = re.compile(pattern)
    for item in items:
        match = regex.search(_normalize(item.text))
        if match:
            return int(match.group(1))
    combined = "".join(_normalize(item.text) for item in sorted(items, key=lambda i: (i.box.y, i.box.x)))
    match = regex.search(combined)
    return int(match.group(1)) if match else None


def find_p3_entry(items: list[OcrItem], frame) -> Rect | None:
    area = _items_in(items, frame, P3_ENTRY_ROI)
    whole = _find(area, "织梦人学会")
    if whole:
        return whole
    first, second = _find(area, "织梦人"), _find(area, "学会")
    return first.union(second) if first and second else None


def find_commission_menu_entry(items: list[OcrItem], frame) -> Rect | None:
    return _find(items, "学会委托", frame, DREAMWEAVER_MENU_ROI)


def find_float_island_entry(items: list[OcrItem], frame) -> Rect | None:
    return _find(items, "浮梦岛", frame, FLOAT_ISLAND_ENTRY_ROI)


def is_dreamweaver_main(items: list[OcrItem], frame) -> bool:
    return find_commission_menu_entry(items, frame) is not None


def is_commission_list(items: list[OcrItem], frame) -> bool:
    return _find(items, "学会委托", frame, COMMISSION_TITLE_ROI) is not None


def classify_slots(items: list[OcrItem], frame) -> tuple[SlotSnapshot, ...] | None:
    if is_reward(items) or not is_commission_list(items, frame):
        return None
    slots: list[SlotSnapshot] = []
    for index, roi in enumerate(COMMISSION_SLOT_ROIS):
        row = _items_in(items, frame, roi)
        completed = _find(row, "已完成")
        submittable = next(
            (item.box for item in row if re.search(r"可提交([1-9]\d*)/\d+", _normalize(item.text))),
            None,
        )
        missing = next(
            (item.box for item in row if re.search(r"已拥有0/1", _normalize(item.text))),
            None,
        )
        submit = _find(row, "提交")
        if completed:
            slots.append(SlotSnapshot(index, SlotState.COMPLETED, None))
        elif submittable:
            slots.append(SlotSnapshot(index, SlotState.SUBMITTABLE, submit))
        elif missing:
            slots.append(SlotSnapshot(index, SlotState.CLOTHES_MISSING, missing))
        else:
            slots.append(SlotSnapshot(index, SlotState.UNKNOWN, None))
    return tuple(slots)


def find_obtain_route(items: list[OcrItem], frame) -> tuple[str, Rect] | None:
    if _find(items, "获取途径", frame, OBTAIN_TITLE_ROI) is None:
        return None
    sources = _items_in(items, frame, OBTAIN_SOURCE_ROI)
    shop = _find(sources, "服装店")
    if shop:
        return "shop", shop
    stage = find_obtain_stage_route(items, frame)
    return ("stage", stage) if stage else None


def find_obtain_stage_route(items: list[OcrItem], frame) -> Rect | None:
    """Recognize stage routes independently of other acquisition types/names."""
    if not is_obtain_path(items, frame):
        return None
    sources = _items_in(items, frame, OBTAIN_SOURCE_ROI)
    stage_candidates = [
        item
        for item in sources
        if item.score >= 0.6 and re.search(r"卷.*\d+-\d+|少女级|公主级", _normalize(item.text))
    ]
    if not stage_candidates:
        return None
    # Unknown sources are never inferred from position or from exclusion alone.
    return min(stage_candidates, key=lambda item: item.box.y).box


def is_obtain_path(items: list[OcrItem], frame) -> bool:
    return _find(items, "获取途径", frame, OBTAIN_TITLE_ROI) is not None


def parse_shop_owned(items: list[OcrItem], frame) -> int | None:
    if _find(items, "服装店", frame, (250, 0, 400, 180)) is None:
        return None
    return _parse_number(_items_in(items, frame, SHOP_OWNED_ROI), r"已拥有:?([0-9]+)")


def find_buy_button(items: list[OcrItem], frame) -> Rect | None:
    return _find(items, "立即购买", frame, SHOP_BUY_ROI)


def _purchase_dialog_confirm(items, frame):
    dialog = _items_in(items, frame, PURCHASE_DIALOG_ROI)
    if _find(dialog, "请确认购买的数量") is None or _find(dialog, "取消") is None:
        return None
    return _find(dialog, "确定")


def parse_purchase_dialog(items: list[OcrItem], frame) -> tuple[int, Rect] | None:
    confirm = _purchase_dialog_confirm(items, frame)
    if confirm is None:
        return None
    # Only the central numeric field; +/- controls and prices are not counts.
    numbers = [int(_normalize(item.text)) for item in
               _items_in(items, frame, PURCHASE_QUANTITY_ROI)
               if item.score >= 0.6 and re.fullmatch(r"[0-9]+", _normalize(item.text))]
    quantity = numbers[0] if len(numbers) == 1 else None
    return (quantity, confirm) if quantity is not None and confirm is not None else None


def read_purchase_quantity_field(frame, recognize_text):
    """Read only the quantity input twice, without a 7->1 replacement or guess.

    Full-screen text detection can misread this small '1' as '7'. Independent
    native/3x crop OCR must agree at high confidence before authorizing input.
    Prices and +/- controls lie outside this scaled field.
    """
    import cv2
    area = _scaled_roi(PURCHASE_QUANTITY_ROI, frame)
    crop = frame[area.y:area.bottom, area.x:area.right]
    if not crop.size:
        return None
    counts = []
    for scale in (1, 3):
        image = crop.copy() if scale == 1 else cv2.resize(
            crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        results = list(recognize_text(image))
        if len(results) != 1:
            return None
        result = results[0]
        text = _normalize(result.text)
        box = result.box
        if (result.score < PURCHASE_QUANTITY_MIN_SCORE
                or re.fullmatch(r"[1-9][0-9]*", text) is None
                or box.x < 0 or box.y < 0 or box.right > image.shape[1]
                or box.bottom > image.shape[0] or box.width <= 0 or box.height <= 0):
            return None
        counts.append(int(text))
    return counts[0] if counts[0] == counts[1] else None


def _verified_purchase_dialog(session, items, frame, *, log=print):
    confirm = _purchase_dialog_confirm(items, frame)
    if confirm is None:
        return None
    reader = getattr(session, "recognize_purchase_quantity", None)
    if reader is None:
        # Existing offline/standalone sessions retain their supplied OCR contract.
        return parse_purchase_dialog(items, frame)
    quantity = reader(frame)
    primary = parse_purchase_dialog(items, frame)
    if quantity is not None and (primary is None or primary[0] != quantity):
        log(f"[DreamweaverCommission] quantity field consensus={quantity}; "
            f"full-screen OCR={primary[0] if primary else 'unknown'}")
    return (quantity, confirm) if quantity is not None else None


def read_purchase_product_title(frame, recognize_text):
    """Read only the modal title twice; never borrow its dimmed background text."""
    import cv2
    area = _scaled_roi((350, 420, 200, 90), frame)
    crop = frame[area.y:area.bottom, area.x:area.right]
    if not crop.size:
        return None
    # The translucent modal lets gray product-description text cross the red
    # title's OCR line. Keep the shared title ink so OCR cannot append those
    # background words to an otherwise correct product name.
    import numpy as np
    pixels = crop.astype(np.int16)
    ink = ((pixels[:, :, 2] - pixels[:, :, 1] >= 25)
           & (pixels[:, :, 2] - pixels[:, :, 0] >= 25)
           & (pixels[:, :, 2] < 245))
    crop = np.full_like(crop, 255)
    crop[ink] = 0
    names = []
    for scale in (1, 3):
        image = crop.copy() if scale == 1 else cv2.resize(
            crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        results = list(recognize_text(image))
        if len(results) != 1:
            return None
        item = results[0]
        name, box = _normalize(item.text), item.box
        if (item.score < .9 or not name or box.width <= 0 or box.height <= 0
                or box.x < 0 or box.y < 0 or box.right > image.shape[1]
                or box.bottom > image.shape[0]):
            return None
        names.append(name)
    return names[0] if names[0] == names[1] else None


def _purchase_safe_rect(box: Rect) -> Rect:
    inset = max(1, min(box.width, box.height) // 5)
    return Rect(box.x + inset, box.y + inset,
                max(1, box.width - 2 * inset), max(1, box.height - 2 * inset))


@lru_cache(maxsize=1)
def _purchase_plus_template():
    import cv2
    import numpy as np
    path = PROJECT_ROOT / "assets/resource/image/dreamweaver/purchase_plus_one.png"
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def find_purchase_plus_one(items, frame):
    if parse_purchase_dialog(items, frame) is None:
        return None
    controls = _items_in(items, frame, (540, 840, 100, 110))
    hits = [item.box for item in controls
            if item.score >= 0.9 and _normalize(item.text) == "+1"]
    if len(hits) == 1:
        return _purchase_safe_rect(hits[0])
    # OCR may read the white '+1' as 'X'. Match only its actual button.
    import cv2
    import numpy as np
    area = _scaled_roi((535, 835, 110, 125), frame)
    template = _purchase_plus_template()
    template = cv2.resize(template, (max(1, round(template.shape[1] * frame.shape[1] / 900)),
                                     max(1, round(template.shape[0] * frame.shape[0] / 1600))))
    search = frame[area.y:area.bottom, area.x:area.right]
    if search.shape[0] < template.shape[0] or search.shape[1] < template.shape[1]:
        return None
    _, score, _, location = cv2.minMaxLoc(cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED))
    if not np.isfinite(score) or score < 0.90:
        return None
    return _purchase_safe_rect(Rect(area.x + location[0], area.y + location[1],
                                    template.shape[1], template.shape[0]))


def find_purchase_cancel(items: list[OcrItem], frame) -> Rect | None:
    dialog = _items_in(items, frame, PURCHASE_DIALOG_ROI)
    def exact(label):
        values = [i.box for i in dialog if i.score >= .8 and _normalize(i.text) == label]
        return values[0] if len(values) == 1 else None
    prompt, cancel, confirm = exact("请确认购买的数量"), exact("取消"), exact("确定")
    if prompt is None or cancel is None or confirm is None:
        return None
    h, w = frame.shape[:2]
    if (min(cancel.width, cancel.height, confirm.width, confirm.height) <= 0
            or not (0 <= cancel.x < cancel.right < w / 2 < confirm.x < confirm.right <= w)
            or not (prompt.bottom < cancel.y < cancel.bottom <= h
                    and prompt.bottom < confirm.y < confirm.bottom <= h)
            or abs(cancel.center[1] - confirm.center[1]) > max(cancel.height, confirm.height)):
        return None
    return _purchase_safe_rect(cancel)


def _labeled_number(items, frame, roi, label, number):
    """Bind a count to its label/line, never concatenate unrelated OCR rows."""
    values = [i for i in _items_in(items, frame, roi) if i.score >= 0.6]
    counts = []
    for anchor in values:
        text = _normalize(anchor.text)
        prefix = re.match(label + r":?", text)
        if prefix is None:
            continue
        match = re.fullmatch(number, text[prefix.end():])
        if match:
            counts.append(int(match.group(1)))
            continue
        if text[prefix.end():]:
            continue  # An invalid count cannot borrow a different row's value.
        for item in values:
            if (item is not anchor and item.box.x >= anchor.box.right - 5
                    and abs(item.box.center[1] - anchor.box.center[1]) <= max(item.box.height, anchor.box.height) * 0.7):
                match = re.fullmatch(number, _normalize(item.text))
                if match:
                    counts.append(int(match.group(1)))
    return counts[0] if len(counts) == 1 else None


def obtain_target_owned(items, frame):
    if not is_obtain_path(items, frame):
        return None
    return _labeled_number(items, frame, OBTAIN_TARGET_OWNED_ROI,
                           r"(?:已拥有|数量)", r"([0-9]+)(?:/[0-9]+)?")


def _grade_from_text(values):
    text = "".join(_normalize(i.text) for i in sorted(values, key=lambda i: (i.box.y, i.box.x)) if i.score >= 0.6)
    text = re.sub(r"[()（）]", "", text)
    kinds = [kind for label, kind in (("公主级", StageKind.PRINCESS), ("少女级", StageKind.MAIDEN)) if label in text]
    return kinds[0] if len(kinds) == 1 else None


def obtain_stage_kind(items, frame, source):
    # Only the selected source row, including a split “级)” continuation.
    height = max(source.height * 2, round(90 * frame.shape[0] / 1600))
    return _grade_from_text([i for i in _items_in(items, frame, OBTAIN_SOURCE_ROI)
        if abs(i.box.center[1] - source.center[1]) <= height / 2])


def stage_target_owned(items, frame, target_name):
    """Read only the ownership directly below the requested drop's name."""
    values = _items_in(items, frame, STAGE_DROPS_ROI)
    names = [i for i in values if i.score >= .8 and _normalize(i.text) == _normalize(target_name)]
    if len(names) != 1:
        return None
    name = names[0].box
    counts = [i for i in values if i.score >= .8 and
              0 <= i.box.y - name.bottom <= max(name.height, i.box.height) * 1.5
              and abs(i.box.center[0] - name.center[0]) <= max(name.width, i.box.width) * .5]
    return _labeled_number(counts, frame, STAGE_DROPS_ROI,
                           r"已拥有", r"([0-9]+)(?:/[0-9]+)?")


def parse_stage_detail(items: list[OcrItem], frame, stage_kind=None, target_name=None) -> StageSnapshot | None:
    attempts = _labeled_number(items, frame, STAGE_DETAIL_ROI,
                               r"今日剩余次数", r"([0-3])/3")
    owned = _labeled_number(items, frame, STAGE_TARGET_OWNED_ROI,
                            r"已拥有", r"([0-9]+)(?:/[0-9]+)?")
    if target_name is not None:
        owned = stage_target_owned(items, frame, target_name)
    # Grade cues belong to the fixed difficulty/card area, never the stage name.
    detected_kind = _grade_from_text(_items_in(items, frame, (0, 1200, 900, 200)))
    if stage_kind and detected_kind and stage_kind is not detected_kind:
        return None
    kind = detected_kind or stage_kind
    once_values = [i for i in items if re.fullmatch(r"过关[一1]次", _normalize(i.text))]
    once = _find(once_values, "过关", frame, STAGE_ONCE_ROI)
    multi_values = [i for i in items if re.fullmatch(r"过关[三3二]次", _normalize(i.text))]
    multi = _find(multi_values, "过关", frame, STAGE_ONCE_ROI)
    # “过关三次” is diagnostic only. Its OCR can be “过关二次”, or absent
    # when disabled. It must never authorize, veto, or supply a click target.
    if once is None or (attempts is None and (kind is not StageKind.MAIDEN or owned is None)):
        return None
    if attempts is None and _find(items, "可能获得", frame, (40, 550, 810, 500)) is None:
        return None
    safe = Rect(once.center[0] - once.width // 4, once.center[1] - once.height // 4,
                max(1, 2 * (once.width // 4)), max(1, 2 * (once.height // 4)))
    return StageSnapshot(attempts, owned, safe, kind, multi)


def maiden_stage_visible(items, frame):
    """Recognize an abandoned maiden card for closing, without choosing a drop."""
    return (_find(items, "过关一次", frame, STAGE_ONCE_ROI) is not None
            and _find(items, "过关十次", frame, STAGE_ONCE_ROI) is not None
            and _find(items, "可能获得", frame, (40, 550, 810, 500)) is not None
            and _find(items, "消耗体力", frame, STAGE_DETAIL_ROI) is not None
            and _find(items, "已拥有", frame, STAGE_TARGET_OWNED_ROI) is not None)


def is_chapter_map(items: list[OcrItem], frame) -> bool:
    return (
        _find(items, "切换章节", frame, (0, 1300, 220, 250)) is not None
        and parse_stage_detail(items, frame) is None
        and _find(items, "过关完成") is None
        and _find(items, "过关一次", frame, STAGE_ONCE_ROI) is None
    )


def is_reward(items: list[OcrItem]) -> bool:
    return _find(items, "恭喜获得") is not None


def is_stage_clear_reward(items: list[OcrItem], frame) -> bool:
    values = [i for i in items if _normalize(i.text) == "过关完成"]
    return _find(values, "过关完成", frame, (180, 650, 540, 500)) is not None


def stage_reward_dismiss_target(items, frame, *, attempt=0):
    """Dismiss the settlement outside its card; the banner is not a button."""
    if not is_stage_clear_reward(items, frame):
        return None
    import cv2
    normalized = cv2.resize(frame, (900, 1600))
    hsv = cv2.cvtColor(normalized, cv2.COLOR_BGR2HSV)
    mask = ((hsv[:, :, 1] <= 80) & (hsv[:, :, 2] >= 185)).astype("uint8") * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                          cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11)))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cards = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if (20 <= x and x + w <= 880 and 250 <= y <= 500
                and 650 <= w <= 860 and 450 <= h <= 850
                and cv2.contourArea(contour) / (w * h) >= .75):
            cards.append((x, y, w, h))
    # The standard settlement layout is also used by OCR-only offline fixtures.
    x, y, w, h = max(cards, key=lambda c: c[2] * c[3]) if cards else (50, 348, 795, 685)
    # Use the space just above the card, then below, then a different upper spot.
    # All three are outside the modal and away from the screen-edge controls.
    center_x = x + w // 2 if attempt < 2 else x + w * 2 // 5
    top = y - 45 if attempt % 2 == 0 else y + h + 20
    return _scaled_roi((center_x - 10, top, 20, 20), frame)


def dismiss_stage_clear_reward(session, *, attempt=0) -> bool:
    """Click outside a freshly verified settlement, never its title ribbon."""
    def close_target(items, current):
        return stage_reward_dismiss_target(items, current, attempt=attempt)

    if not _business_click(session, 0, 0, confirm=close_target):
        return False
    session.sleep(PAGE_WAIT_SECONDS)
    return True


def wait_for_stage_detail(session, selector=None, *, attempts=13):
    """Wait for verified counts; retry a lingering reward, never the clear button."""
    selector = selector or parse_stage_detail
    def select(items, frame):
        if is_stage_clear_reward(items, frame):
            return ("reward", None)
        value = selector(items, frame)
        return ("stage", value) if value is not None else None

    for retry in range(3):
        page = _poll(session, select, attempts=attempts)
        if page is None:
            return None
        if page[2][0] == "stage":
            return page[0], page[1], page[2][1]
        if retry == 2 or not dismiss_stage_clear_reward(session, attempt=retry + 1):
            return None
    return None


def detect_float_island(items: list[OcrItem], frame) -> FloatIslandSnapshot | None:
    if _find(items, "浮梦岛", frame, FLOAT_ISLAND_TITLE_ROI) is None:
        return None
    left_action = _find(items, "探索一次", frame, FLOAT_ISLAND_LEFT_ACTION_ROI)
    if left_action is None:
        return None
    status_items = _items_in(items, frame, FLOAT_ISLAND_LEFT_STATUS_ROI)
    normalized = "".join(_normalize(item.text) for item in status_items)
    if re.search(r"免费领取1/1", normalized):
        state = FloatIslandState.FREE
    elif re.search(r"花费|消耗|钻石", normalized):
        state = FloatIslandState.PAID
    else:
        state = FloatIslandState.UNKNOWN
    return FloatIslandSnapshot(state, left_action)


def detect_dreamweaver_entry_state(
    items: list[OcrItem],
    frame,
) -> DreamweaverEntryState | None:
    """Classify restart pages, checking overlays before their underlying page."""
    if is_reward(items) or is_stage_clear_reward(items, frame):
        return DreamweaverEntryState.REWARD
    if parse_purchase_dialog(items, frame) is not None:
        return DreamweaverEntryState.PURCHASE_DIALOG
    if parse_shop_owned(items, frame) is not None:
        return DreamweaverEntryState.SHOP_PRODUCT
    if parse_stage_detail(items, frame) is not None or maiden_stage_visible(items, frame):
        return DreamweaverEntryState.STAGE_DETAIL
    if is_chapter_map(items, frame):
        return DreamweaverEntryState.CHAPTER_MAP
    if is_obtain_path(items, frame):
        return DreamweaverEntryState.OBTAIN_PATH
    if classify_slots(items, frame) is not None:
        return DreamweaverEntryState.COMMISSION_LIST
    if detect_float_island(items, frame) is not None:
        return DreamweaverEntryState.FLOAT_ISLAND
    if is_dreamweaver_main(items, frame):
        return DreamweaverEntryState.DREAMWEAVER_MAIN
    if getattr(detect_home_page(frame), "page", "Unknown") in {"P1", "P2", "P3"}:
        return DreamweaverEntryState.HOME
    return None


def _wait_before_click(
    session: CommissionSession,
    *,
    delay_sampler: Callable[[float, float], float] | None = None,
) -> float:
    """Wait once before a confirmed Dreamweaver business click.

    Offline sessions may inject ``dreamweaver_pre_click_delay_sampler`` or pass
    ``delay_sampler`` directly, so tests never need wall-clock sleeps.
    """
    sampler = delay_sampler or getattr(
        session,
        "dreamweaver_pre_click_delay_sampler",
        random.uniform,
    )
    delay = float(
        sampler(
            DREAMWEAVER_PRE_CLICK_DELAY_MIN,
            DREAMWEAVER_PRE_CLICK_DELAY_MAX,
        )
    )
    if not DREAMWEAVER_PRE_CLICK_DELAY_MIN <= delay <= DREAMWEAVER_PRE_CLICK_DELAY_MAX:
        raise ValueError(f"Dreamweaver pre-click delay out of range: {delay}")
    session.sleep(delay)
    return delay


def _business_click(session: CommissionSession, x: int, y: int, *, confirm=None,
                    position_tolerance=3) -> bool:
    _wait_before_click(session)
    if confirm is not None:
        previous = None
        for index in range(2):
            fresh_frame = session.capture_frame()
            fresh_items = session.recognize_text(fresh_frame)
            if (home_random_visit_loading_classifier(fresh_items, fresh_frame)
                    or detect_common_safe_popup(fresh_frame, fresh_items).kind
                    in {CommonPopupKind.ACHIEVEMENT, CommonPopupKind.SKILL_UNLOCK}):
                return False
            target = confirm(fresh_items, fresh_frame)
            if target is None:
                return False
            if previous is not None:
                tolerance = max(2, round(position_tolerance * fresh_frame.shape[1] / 720))
                if any(abs(a-b) > tolerance for a, b in zip(
                        (previous.x, previous.y, previous.width, previous.height),
                        (target.x, target.y, target.width, target.height))):
                    return False
                if not (previous.x <= target.center[0] < previous.right
                        and previous.y <= target.center[1] < previous.bottom):
                    return False
            previous = target
            if index == 0:
                session.sleep(.4)
        x, y = target.center
    return session.click(x, y) is not False


def _home_page(frame):
    return str(getattr(detect_home_page(frame), "page", "Unknown"))


def _safe_back_click(session, frame, source, *, chapter=False):
    def confirm(items, current):
        if _home_page(current) in {"P1", "P2", "P3"} or not source(items, current):
            return None
        return find_back_button(current, chapter=chapter)

    if _home_page(frame) in {"P1", "P2", "P3"}:
        return False
    target = find_back_button(frame, chapter=chapter)
    if target is None:
        return False
    return _business_click(session, *target.center, confirm=confirm)


def _click_reference(session: CommissionSession, frame, point: tuple[int, int]) -> None:
    height, width = frame.shape[:2]
    _business_click(
        session,
        round(point[0] * width / 900),
        round(point[1] * height / 1600),
        confirm=lambda items, current: _scaled_roi((*point, 1, 1), current) if is_reward(items) else None,
    )


def _poll(session: CommissionSession, selector, attempts: int = MAX_POLL_ATTEMPTS):
    return poll_target_with_common_popups(
        session,
        selector,
        timeout=max(POLL_WAIT_SECONDS, (attempts - 1) * POLL_WAIT_SECONDS),
        interval=POLL_WAIT_SECONDS,
        loading_classifier=home_random_visit_loading_classifier,
        loading_timeout_state="QJNN_DreamweaverLoadingTimeout",
    )


def _poll_read_only(
    session: CommissionSession,
    selector,
    attempts: int = MAX_POLL_ATTEMPTS,
):
    """Finite recognition-only polling; never dismisses popups or clicks UI."""
    for attempt in range(attempts):
        frame = session.capture_frame()
        items = session.recognize_text(frame)
        value = selector(items, frame)
        if value is not None:
            return frame, items, value
        if attempt + 1 < attempts:
            session.sleep(POLL_WAIT_SECONDS)
    return None


def _commission_entry_target(items, frame):
    # Never retry through a reward/modal or mistake the list title for a menu.
    if (is_reward(items) or is_commission_list(items, frame)
            or detect_common_safe_popup(frame, items).matched):
        return None
    return find_commission_menu_entry(items, frame)


def _enter_commission_list(session, menu, *, read_only=False, log=print):
    """A visible menu may ignore its first tap during the character entrance.

    Retry only this navigation control, never a submit/purchase action. Check
    both the page after each failed wait and the target after the random delay.
    """
    poll = _poll_read_only if read_only else _poll
    for attempt in range(MAX_COMMISSION_ENTRY_CLICKS):
        _, _, target = menu
        log(f"[DreamweaverCommission] commission entry attempt={attempt + 1}/{MAX_COMMISSION_ENTRY_CLICKS}")
        clicked = _business_click(session, *target.center, confirm=_commission_entry_target)
        if clicked:
            session.sleep(PAGE_WAIT_SECONDS)
        entered = poll(session, classify_slots)
        if entered is not None:
            return entered
        if not clicked or attempt + 1 == MAX_COMMISSION_ENTRY_CLICKS:
            return None
        frame = session.capture_frame()
        items = session.recognize_text(frame)
        # A delayed transition can complete just after the polling deadline.
        slots = classify_slots(items, frame)
        if slots is not None:
            return frame, items, slots
        target = _commission_entry_target(items, frame)
        if target is None:
            log("[DreamweaverCommission] commission retry cancelled: main menu no longer confirmed")
            return None
        log("[DreamweaverCommission] still on main menu; retrying confirmed commission entry")
        menu = frame, items, target
    return None


def log_read_only_slot_scan(
    items: list[OcrItem],
    frame,
    slots: tuple[SlotSnapshot, ...],
    *,
    log: Callable[[str], None] = print,
) -> None:
    log("[DreamweaverCommissionReadOnly] 三栏OCR扫描开始")
    for slot, roi in zip(slots, COMMISSION_SLOT_ROIS):
        area = _scaled_roi(roi, frame)
        row_items = sorted(
            _items_in(items, frame, roi),
            key=lambda item: (item.box.y, item.box.x),
        )
        log(
            "[DreamweaverCommissionReadOnly] "
            f"栏位={slot.index + 1} 坐标=({area.x},{area.y},{area.width},{area.height}) "
            f"分类={SLOT_STATE_LABELS[slot.state]}"
        )
        if not row_items:
            log("[DreamweaverCommissionReadOnly] OCR原文=<空>")
            continue
        for value in row_items:
            box = value.box
            log(
                "[DreamweaverCommissionReadOnly] "
                f"OCR原文={value.text!r} 坐标=({box.x},{box.y},{box.width},{box.height}) "
                f"置信度={value.score:.4f}"
            )
    log("[DreamweaverCommissionReadOnly] 三栏OCR扫描结束；未执行任何委托操作")


def _leave_shop_product(session: CommissionSession, frame) -> bool:
    def target(items, current):
        owned = parse_shop_owned(items, current)
        product = _product_name(items, current, (450, 130, 400, 150))
        if (owned is None or product is None or _home_page(current) in {"P1", "P2", "P3"}
                or _purchase_dialog_confirm(items, current) is not None):
            return None
        box = find_back_button(current)
        return (_purchase_safe_rect(box), (product, owned)) if box is not None else None

    if _home_page(frame) in {"P1", "P2", "P3"} or not _purchase_click(session, target):
        return False
    session.sleep(PAGE_WAIT_SECONDS)
    return _poll(session, lambda items, frame: classify_slots(items, frame)) is not None


def _return_from_shop(session: CommissionSession, current=None) -> bool:
    if current is None:
        current = _poll(session, lambda items, frame: parse_shop_owned(items, frame))
    if current is None:
        return False
    return _leave_shop_product(session, current[0])


def _return_from_obtain_path(session: CommissionSession, frame) -> bool:
    if not _close_confirmed_popup(session, frame, is_obtain_path):
        return False
    session.sleep(PAGE_WAIT_SECONDS)
    return _restore_commission(session)


def _return_from_chapter_map(session: CommissionSession, frame) -> bool:
    if not _safe_back_click(session, frame, is_chapter_map, chapter=True):
        return False
    session.sleep(PAGE_WAIT_SECONDS)
    return _restore_commission(session)


def _restore_commission(session):
    # The chapter arrow may lead to HOME, not directly to the old modal list.
    # HOME terminates all back input; re-enter through the existing entry flow.
    for _ in range(3):
        page = _poll(session, detect_dreamweaver_entry_state)
        if page is None:
            return False
        current, _, state = page
        if state is DreamweaverEntryState.COMMISSION_LIST:
            return True
        if state is DreamweaverEntryState.HOME:
            navigation = navigate_home_to_p3(session.capture_frame,
                lambda x, y: _business_click(session, x, y), sleep=session.sleep, log=lambda _: None)
            if not navigation.success:
                return False
            entry = _poll(session, find_p3_entry)
            if entry is None:
                return False
            if not _business_click(session, *entry[2].center, confirm=find_p3_entry):
                return False
        elif state is DreamweaverEntryState.DREAMWEAVER_MAIN:
            entry = _poll(session, find_commission_menu_entry)
            if entry is None:
                return False
            if not _business_click(session, *entry[2].center, confirm=_commission_entry_target):
                return False
        else:
            return False
        session.sleep(PAGE_WAIT_SECONDS)
    return False


def _cancel_purchase_and_return(session: CommissionSession, frame, items: list[OcrItem]) -> bool:
    cancel = find_purchase_cancel(items, frame)
    if cancel is None:
        return False
    # A restart on the confirmation dialog must never confirm an old purchase.
    def target(current_items, current):
        box = find_purchase_cancel(current_items, current)
        return (box, "cancel_purchase") if box is not None else None
    if not _purchase_click(session, target):
        return False
    session.sleep(PAGE_WAIT_SECONDS)
    product = _poll(session, lambda current_items, current: parse_shop_owned(current_items, current))
    if product is None:
        return False
    return _leave_shop_product(session, product[0])


def _product_name(items, frame, roi):
    names = [_normalize(item.text) for item in _items_in(items, frame, roi)
             if item.score >= .8 and _normalize(item.text)]
    return names[0] if len(names) == 1 else None


def _wait_for_purchase_settlement(session, product, expected, *, log):
    """Observe a submitted payment without ever submitting another payment.

    Server/loading animations can keep the quantity dialog visible for several
    seconds. The normal two-second navigation poll is too short for a payment.
    Even if OCR misreads the prompt, its cancel/confirm controls still block
    accepting inventory visible behind the modal.
    """
    timeout = PURCHASE_SETTLEMENT_TIMEOUT_SECONDS
    started = session.monotonic()
    log(f"[DreamweaverCommission] waiting up to {timeout:.0f}s for purchase inventory; no repeat payment")

    stable_since = None
    previous_capture = None

    def settled_inventory(items, frame):
        nonlocal stable_since, previous_capture
        modal = (_find(items, "取消") is not None
                 and (_find(items, "确定") is not None or _find(items, "确认") is not None))
        if (not modal and not is_reward(items)
                and not home_random_visit_loading_classifier(items, frame)
                and not detect_common_safe_popup(frame, items).matched
                and parse_shop_owned(items, frame) == expected
                and _product_name(items, frame, (450, 130, 400, 150)) == product):
            if (stable_since is not None and session.capture_number == previous_capture + 1
                    and session.monotonic() - stable_since >= POLL_WAIT_SECONDS):
                return expected
            stable_since = session.monotonic()
            previous_capture = session.capture_number
            return None
        stable_since = None
        return None

    # Preserve the existing, strictly whitelisted achievement/notice handling.
    # The selector and this polling fallback never click a purchase control.
    result = poll_target_with_common_popups(
        session, settled_inventory, timeout=timeout, interval=POLL_WAIT_SECONDS,
        loading_classifier=home_random_visit_loading_classifier,
        loading_timeout_state="QJNN_DreamweaverLoadingTimeout", log=log,
    )
    if result is not None:
        log(f"[DreamweaverCommission] purchase inventory confirmed after {session.monotonic() - started:.1f}s")
    return result


def run_shop_flow(session: CommissionSession, *,
                  checkpoint_path=None,
                  log: Callable[[str], None] = print) -> bool:
    session = PurchaseEvidence(session)
    success = False
    try:
        journal = PurchaseJournal(checkpoint_path if checkpoint_path is not None else RunRecord("dreamweaver_purchase_state.json"))
        with ExitStack() as stack:
            stack.enter_context(journal.transaction())
            fence_path = getattr(session, "purchase_fence_path", None)
            fence = PurchaseJournal(fence_path) if fence_path is not None else None
            if fence is not None:
                stack.enter_context(fence.transaction())
            success = _run_recorded_shop_flow(session, journal, fence=fence, log=log)
            return success
    except (OSError, ValueError, UnicodeError) as exc:
        log("[DreamweaverCommission] shopping stopped: purchase record unavailable or unresolved")
        log(f"[DreamweaverCommission] shopping stopped: {exc}")
        return False
    finally:
        if not success:
            session.note("safe_stop")
            session.save_failure(log)


def _purchase_blocked(items, frame):
    return (home_random_visit_loading_classifier(items, frame) or is_reward(items)
            or detect_common_safe_popup(frame, items).matched)


def _purchase_modal_product(session, items, frame):
    title_reader = getattr(session, "recognize_purchase_product", None)
    return (title_reader(frame) if title_reader is not None
            else _product_name(items, frame, (350, 420, 200, 90)))


def _purchase_click(session, selector, *, before_submit=None):
    if not isinstance(session, PurchaseEvidence):
        session = PurchaseEvidence(session)
    return stable_purchase_click(session, selector, _wait_before_click, _purchase_blocked,
                                 before_submit=before_submit)


def _run_recorded_shop_flow(session, journal, *, fence=None, log):
    snapshot = _poll(session, lambda items, frame: parse_shop_owned(items, frame))
    if snapshot is None:
        log("服装店页面未识别到已拥有数量")
        return False
    frame, items, owned = snapshot
    product = _product_name(items, frame, (450, 130, 400, 150))
    if product is None:
        log("[DreamweaverCommission] shopping product unconfirmed")
        return False
    # Completed purchases may have been consumed by an earlier commission.
    # Use current stock, while the journal transaction still fences uncertain payments.
    if owned >= 2:
        log("[DreamweaverCommission] shopping inventory sufficient; returning to commission")
        return _leave_shop_product(session, frame)
    buy = find_buy_button(items, frame)
    if buy is None:
        log("服装店未识别到立即购买")
        return False
    def confirm_buy(current_items, current):
        if (parse_purchase_dialog(current_items, current) is not None
                or (_find(current_items, "取消") is not None
                    and _find(current_items, "确定") is not None)):
            return None
        if parse_shop_owned(current_items, current) != owned:
            return None
        if _product_name(current_items, current, (450, 130, 400, 150)) != product:
            return None
        box = find_buy_button(current_items, current)
        return (_purchase_safe_rect(box), (product, owned)) if box is not None else None
    if not _purchase_click(session, confirm_buy):
        return False
    session.sleep(PAGE_WAIT_SECONDS)
    def quantity_dialog(current_items, current):
        return _verified_purchase_dialog(session, current_items, current, log=log)
    dialog = _poll(session, quantity_dialog)
    if dialog is None:
        log("购买数量弹窗未识别或数量框两次复核不一致，SafeStop")
        return False
    dialog_frame, _, (quantity, confirm) = dialog
    if quantity != 1:
        log(f"购买数量弹窗默认值不是1（识别为{quantity}），SafeStop")
        return False
    if owned == 0:
        def confirm_plus(current_items, current):
            state = quantity_dialog(current_items, current)
            if _purchase_modal_product(session, current_items, current) != product:
                return None
            box = find_purchase_plus_one(current_items, current) if state and state[0] == 1 else None
            return (box, (product, 1)) if box is not None else None
        if not _purchase_click(session, confirm_plus):
            log("购买数量/+1未通过fresh frame复核，SafeStop")
            return False
        session.sleep(PAGE_WAIT_SECONDS)
        changed = _poll(session, quantity_dialog)
        if changed is None or changed[2][0] != 2:
            log("服装店 N=0 时数量未确认变为2")
            return False
        _, _, (_, confirm) = changed
    wanted = 2 if owned == 0 else 1
    def confirm_purchase(current_items, current):
        state = quantity_dialog(current_items, current)
        if not state or state[0] != wanted:
            log("[DreamweaverCommission] shopping blocked: quantity unconfirmed")
            return None
        # The centered modal title excludes dimmed description text behind the dialog.
        modal_product = _purchase_modal_product(session, current_items, current)
        if modal_product != product:
            log("[DreamweaverCommission] shopping blocked: modal product unconfirmed")
            return None
        amount = coin_quote(current_items, current)
        session.note("payment_check", capture=session.capture_number,
                     quantity=state[0], product=modal_product, coins=amount)
        if amount is None:
            log("[DreamweaverCommission] shopping blocked: price/currency unconfirmed")
            return None
        return _purchase_safe_rect(state[1]), (amount, product, wanted, owned)

    def arm_payment(identity):
        amount, _, _, _ = identity
        # The durable fence survives queue/application exits. Write it first;
        # a subsequent failure remains safely unresolved, never re-submitted.
        if fence is not None:
            fence.arm(amount, product, wanted, owned)
        journal.arm(amount, product, wanted, owned)
        log(f"[DreamweaverCommission] shopping verified: {amount} coins; quantity={wanted}")
    if not _purchase_click(session, confirm_purchase, before_submit=arm_payment):
        log("确认购买前弹窗/数量发生变化，SafeStop")
        return False
    session.sleep(PAGE_WAIT_SECONDS)
    expected = 2 if owned == 0 else owned + 1
    purchased = _wait_for_purchase_settlement(session, product, expected, log=log)
    if purchased is None:
        log("[DreamweaverCommission] shopping result unconfirmed; retry blocked")
        log("购买后未确认已拥有数量增加；不重复购买")
        return False
    journal.complete()
    if fence is not None:
        fence.complete()
    return _return_from_shop(session, purchased)


def _return_from_stage(session: CommissionSession, frame, *, stage_kind=None, target_name=None) -> bool:
    if not _close_confirmed_popup(session, frame, lambda items, current:
            parse_stage_detail(items, current, stage_kind, target_name) is not None
            or maiden_stage_visible(items, current)):
        return False
    session.sleep(PAGE_WAIT_SECONDS)
    chapter = _poll(session, lambda items, current: True if is_chapter_map(items, current) else None)
    if chapter is None:
        return False
    return _return_from_chapter_map(session, chapter[0])


def _close_confirmed_popup(session, frame, source):
    def confirm(items, current):
        if _home_page(current) in {"P1", "P2", "P3"} or not source(items, current):
            return None
        return _scaled_roi((440, 240, 20, 20), current)
    return _business_click(session, *(_scaled_roi((440, 240, 20, 20), frame).center), confirm=confirm)


def run_stage_flow(session: CommissionSession, *, stage_kind=None, target_owned=None, target_name=None,
                   exhaust_attempts=False, return_from_stage=None,
                   log: Callable[[str], None] = print) -> AcquisitionResult:
    """Shared single-clear loop; callers may drain attempts and own the return path."""
    clears = 0
    previous = None
    while True:
        def select_detail(items, current):
            value = parse_stage_detail(items, current, stage_kind, target_name)
            if value is None:
                return None
            if previous is not None:
                if (exhaust_attempts or stage_kind is StageKind.PRINCESS) and value.attempts != previous.attempts - 1:
                    return None  # Wait for the new count; stale frames never authorize another clear.
                if stage_kind is StageKind.MAIDEN and (value.owned is None or value.owned < previous.owned):
                    return None
            return value
        detail = wait_for_stage_detail(session, select_detail, attempts=13)
        if detail is None:
            log("关卡详情/本次过关后的数量未确认；不补点")
            return AcquisitionResult(False, "stage_unknown")
        frame, _, state = detail
        log(f"[DreamweaverCommission] stage controls once={state.once_box} multi(diagnostic)={state.multi_box}")
        stage_kind = stage_kind or state.kind
        if stage_kind is None and not exhaust_attempts:
            return AcquisitionResult(False, "stage_kind_unknown")
        if exhaust_attempts or stage_kind is StageKind.PRINCESS:
            if state.attempts is None:
                return AcquisitionResult(False, "princess_attempts_unknown")
            if exhaust_attempts and not 0 <= state.attempts <= 3:
                return AcquisitionResult(False, "stage_attempts_out_of_range")
            log(f"[DreamweaverCommission] princess remaining={state.attempts}/3")
            finished = state.attempts == 0
            limit = MAX_PRINCESS_STAGE_CLEARS
            reason = "attempts_exhausted"
        else:
            if target_owned is None or state.owned is None or state.owned < target_owned:
                return AcquisitionResult(False, "target_owned_unconfirmed")
            log(f"[DreamweaverCommission] maiden target={target_name!r} owned={state.owned}/2")
            finished = state.owned >= 2
            limit = MAX_MAIDEN_STAGE_CLEARS
            reason = "acquired"
        if finished:
            returned = (return_from_stage(session, frame) if return_from_stage is not None
                        else _return_from_stage(session, frame, stage_kind=stage_kind, target_name=target_name))
            return AcquisitionResult(returned, reason if returned else "stage_return_failed")
        if clears >= limit:
            log("关卡循环达到安全上限，SafeStop")
            return AcquisitionResult(False, "stage_clear_limit")
        def confirm_once(items, current):
            fresh = parse_stage_detail(items, current, stage_kind, target_name)
            if fresh is None or fresh.attempts != state.attempts or fresh.owned != state.owned:
                return None
            allowed = fresh.attempts > 0 if exhaust_attempts or stage_kind is StageKind.PRINCESS else fresh.owned < 2
            return fresh.once_box if allowed else None
        if not _business_click(session, *state.once_box.center, confirm=confirm_once):
            return AcquisitionResult(False, "stage_changed_before_clear")
        clears += 1
        session.sleep(PAGE_WAIT_SECONDS)
        reward = _poll(session, lambda items, current: True if is_stage_clear_reward(items, current) else None, attempts=21)
        if reward is None:
            log("点击过关一次后未识别到过关完成；不重复点击")
            return AcquisitionResult(False, "stage_reward_unknown")
        if not dismiss_stage_clear_reward(session):
            return AcquisitionResult(False, "stage_reward_changed")
        session.sleep(PAGE_WAIT_SECONDS)
        previous = state


def _submit_confirmation(items: list[OcrItem], frame) -> Rect | None:
    combined = "".join(
        _normalize(item.text) for item in sorted(items, key=lambda item: (item.box.y, item.box.x))
    )
    if "确定要提交" not in combined:
        return None
    return _find(items, "确定", frame, (300, 650, 500, 500))


def submit_slot(session: CommissionSession, slot: SlotSnapshot, *, log: Callable[[str], None] = print) -> bool:
    if slot.action_box is None or slot.state is not SlotState.SUBMITTABLE:
        return False
    # Submission may be irreversible when the daily confirmation is disabled.
    # Re-read the list and the exact row immediately before clicking it.
    fresh = _poll(session, lambda items, frame: classify_slots(items, frame))
    if fresh is None:
        log("提交前无法重新确认学会委托页面")
        return False
    _, _, fresh_slots = fresh
    fresh_slot = fresh_slots[slot.index]
    if fresh_slot.state is not SlotState.SUBMITTABLE or fresh_slot.action_box is None:
        log("提交前原栏已变化或提交按钮不可用，SafeStop")
        return False
    def confirm_submit(items, frame):
        slots = classify_slots(items, frame)
        current = slots[slot.index] if slots is not None else None
        return current.action_box if current and current.state is SlotState.SUBMITTABLE else None
    if not _business_click(session, *fresh_slot.action_box.center, confirm=confirm_submit):
        return False
    session.sleep(PAGE_WAIT_SECONDS)
    first = _poll(
        session,
        lambda items, frame: _submit_confirmation(items, frame)
        or ("reward" if is_reward(items) else None)
        or ("list" if classify_slots(items, frame) is not None else None),
    )
    if first is None:
        log("提交后未识别确认框、奖励或委托列表；不重复提交")
        return False
    frame, items, state = first
    if isinstance(state, Rect):
        if not _business_click(session, *state.center, confirm=_submit_confirmation):
            return False
        session.sleep(PAGE_WAIT_SECONDS)

    for dismiss_count in range(MAX_REWARD_LAYERS + 1):
        def submitted_or_reward(now_items, now_frame):
            if is_reward(now_items):
                return "reward"
            slots = classify_slots(now_items, now_frame)
            # The server can leave the old SUBMITTABLE row visible for several
            # seconds before showing the reward. Wait; never submit it twice.
            if slots is not None and slots[slot.index].state is SlotState.COMPLETED:
                return "list", slots
            return None

        current = _poll(
            session,
            submitted_or_reward,
            attempts=41,
        )
        if current is None:
            return False
        current_frame, _, current_state = current
        if isinstance(current_state, tuple) and current_state[0] == "list":
            slots = current_state[1]
            return slots[slot.index].state is SlotState.COMPLETED
        if dismiss_count >= MAX_REWARD_LAYERS:
            log("奖励关闭达到5次硬上限，SafeStop")
            return False
        _click_reference(session, current_frame, REWARD_OUTSIDE_POINT)
        session.sleep(PAGE_WAIT_SECONDS)
    return False


def acquire_missing_clothes(session: CommissionSession, slot: SlotSnapshot, *,
                            checkpoint_path=None,
                            log: Callable[[str], None] = print) -> AcquisitionResult:
    if slot.action_box is None:
        return AcquisitionResult(False, "missing_target")
    fresh = _poll(session, classify_slots)
    if fresh is None:
        return AcquisitionResult(False, "commission_unknown")
    slot = fresh[2][slot.index]
    if slot.state is not SlotState.CLOTHES_MISSING or slot.action_box is None:
        return AcquisitionResult(False, "slot_changed")
    requested = [match.group(1) for item in _items_in(fresh[1], fresh[0], COMMISSION_SLOT_ROIS[slot.index])
                 if item.score >= .8 and (match := re.fullmatch(r"请帮我找到[0-9]+个(.+)", _normalize(item.text)))]
    target_name = requested[0] if len(requested) == 1 else None
    # OCR width is not the plus position: the old right+height/2 calculation
    # lands left of the button in the supplied screenshot. Match the stable +.
    def confirm_missing(items, current):
        slots = classify_slots(items, current)
        candidate = slots[slot.index] if slots is not None else None
        if candidate is None or candidate.state is not SlotState.CLOTHES_MISSING:
            return None
        return find_missing_plus(current, candidate, log=log)
    target = find_missing_plus(fresh[0], slot, log=log)
    if target is None or not _business_click(session, *target.center, confirm=confirm_missing):
        return AcquisitionResult(False, "missing_plus_unconfirmed")
    session.sleep(PAGE_WAIT_SECONDS)
    route = _poll(session, find_obtain_route)
    if route is None:
        log("未识别获取途径或安全来源")
        return AcquisitionResult(False, "obtain_route_unknown")
    route_frame, route_items, (kind, source) = route
    stage_kind = obtain_stage_kind(route_items, route_frame, source) if kind == "stage" else None
    target_owned = obtain_target_owned(route_items, route_frame) if kind == "stage" else None
    if stage_kind is StageKind.MAIDEN:
        if target_owned is None:
            return AcquisitionResult(False, "obtain_target_owned_unknown")
        log(f"[DreamweaverCommission] obtain target owned={target_owned}")
        if target_owned >= 2:
            returned = _return_from_obtain_path(session, route_frame)
            return AcquisitionResult(returned, "acquired" if returned else "obtain_return_failed")
    if not _business_click(session, *source.center, confirm=lambda items, current:
            value[1] if (value := find_obtain_route(items, current)) and value[0] == kind else None):
        return AcquisitionResult(False, "obtain_route_changed")
    session.sleep(PAGE_WAIT_SECONDS)
    if kind == "shop":
        success = run_shop_flow(session, checkpoint_path=checkpoint_path, log=log)
        return AcquisitionResult(success, "acquired" if success else "shop_failed")
    return run_stage_flow(session, stage_kind=stage_kind, target_owned=target_owned,
                          target_name=target_name, log=log)


def _float_island_reward_or_page(items: list[OcrItem], frame):
    if is_float_island_reward(items, frame):
        return "reward"
    if _find(items, "恭喜获得", frame, FLOAT_ISLAND_REWARD_TITLE_ROI) is not None:
        return None  # A fading/partial card must not expose the underlying page.
    page = detect_float_island(items, frame)
    return ("page", page) if page is not None and page.state is not FloatIslandState.UNKNOWN else None


def is_float_island_reward(items, frame):
    """Stable heading plus blank card strips; no reward name/icon matching."""
    import numpy as np

    titles = [i for i in items if _normalize(i.text) == "恭喜获得"]
    if _find(titles, "恭喜获得", frame, FLOAT_ISLAND_REWARD_TITLE_ROI) is None:
        return False
    for roi in ((160, 460, 110, 440), (650, 460, 80, 450)):
        area = _scaled_roi(roi, frame)
        pixels = frame[area.y:area.bottom, area.x:area.right]
        if not pixels.size or np.mean(np.min(pixels, axis=2) >= 185) < 0.6:
            return False
    return True


def _wait_float_island_reward_transition(session, *, deadline, require_reward, log):
    # Observation only: do not enter the shared 20s Loading wait or submit any
    # input. Loading, animation, unknown and pre-reward island frames all consume
    # this one bounded budget. Each observation requests a new screenshot.
    attempts = math.ceil(FLOAT_ISLAND_REWARD_TIMEOUT_SECONDS / FLOAT_ISLAND_REWARD_POLL_SECONDS) + 1
    for _ in range(attempts):
        remaining = deadline - session.monotonic()
        if remaining <= 0:
            break
        session.sleep(min(FLOAT_ISLAND_REWARD_POLL_SECONDS, remaining))
        frame = session.capture_frame()
        items = session.recognize_text(frame)
        state = _float_island_reward_or_page(items, frame)
        returned_paid = (isinstance(state, tuple) and state[0] == "page"
                         and state[1].state is FloatIslandState.PAID)
        if state == "reward" or (not require_reward and returned_paid):
            return frame, items, state
        phase = "Loading" if home_random_visit_loading_classifier(items, frame) else "transition"
        log(f"[DreamweaverCommission] awaiting float island reward: {phase}")
    return None


def _leave_float_island(
    session: CommissionSession,
    frame,
    *,
    free_claimed: bool,
    log: Callable[[str], None],
) -> FloatIslandResult:
    # Confirm both the FloatIsland page and the actual arrow in a fresh frame.
    if not _safe_back_click(session, frame, lambda items, current: detect_float_island(items, current) is not None):
        return FloatIslandResult(False, free_claimed, "float_island_back_unconfirmed")
    session.sleep(PAGE_WAIT_SECONDS)
    returned = _poll(
        session,
        lambda items, current: True if is_dreamweaver_main(items, current) else None,
    )
    if returned is None:
        return FloatIslandResult(False, free_claimed, "float_island_main_not_restored")
    log("[DreamweaverCommission] returned from float island")
    return FloatIslandResult(True, free_claimed, "completed")


def run_float_island(
    session: CommissionSession,
    *,
    log: Callable[[str], None] = print,
) -> FloatIslandResult:
    entry = _poll(
        session,
        lambda items, frame: (
            find_float_island_entry(items, frame)
            if is_dreamweaver_main(items, frame)
            else None
        ),
    )
    if entry is None:
        return FloatIslandResult(False, False, "float_island_entry_not_found")
    _, _, entry_box = entry
    log("[DreamweaverCommission] entering float island")
    if not _business_click(session, *entry_box.center, confirm=lambda items, frame:
                           find_float_island_entry(items, frame) if is_dreamweaver_main(items, frame) else None):
        return FloatIslandResult(False, False, "float_island_entry_unstable")
    session.sleep(PAGE_WAIT_SECONDS)

    current = _poll(session, detect_float_island)
    if current is None:
        return FloatIslandResult(False, False, "float_island_not_entered")
    log("[DreamweaverCommission] float island confirmed")
    return continue_float_island(session, current=current, log=log)


def continue_float_island(
    session: CommissionSession,
    *,
    current=None,
    reward_already_closed: bool = False,
    log: Callable[[str], None] = print,
) -> FloatIslandResult:
    if current is None:
        current = _poll(session, detect_float_island)
        if current is None:
            return FloatIslandResult(False, False, "float_island_not_confirmed")
        log("[DreamweaverCommission] float island confirmed")
    free_claimed = reward_already_closed

    while True:
        frame, _, state = current
        if state.state is FloatIslandState.PAID:
            log("[DreamweaverCommission] float island free exploration already used")
            return _leave_float_island(
                session,
                frame,
                free_claimed=free_claimed,
                log=log,
            )
        if state.state is FloatIslandState.UNKNOWN:
            log("[DreamweaverCommission] float island left status unknown; SafeStop")
            return FloatIslandResult(False, free_claimed, "float_island_status_unknown")
        if free_claimed:
            log("[DreamweaverCommission] free state remained after claim; refusing a second exploration")
            return FloatIslandResult(False, True, "float_island_free_state_stale")

        # This fresh snapshot confirms the page, FREE 1/1 status and the left
        # 探索一次 button in one frame. The right-side 探索十次 is never selected.
        log("[DreamweaverCommission] claiming one free float island exploration")
        def confirm_free(items, current_frame):
            if is_reward(items):
                return None
            current_state = detect_float_island(items, current_frame)
            return (current_state.explore_once_box if current_state is not None
                    and current_state.state is FloatIslandState.FREE else None)
        if not _business_click(session, *state.explore_once_box.center, confirm=confirm_free):
            return FloatIslandResult(False, False, "float_island_free_button_unstable")
        free_claimed = True
        reward_deadline = session.monotonic() + FLOAT_ISLAND_REWARD_TIMEOUT_SECONDS
        session.sleep(PAGE_WAIT_SECONDS)

        reward_seen = False
        next_page = None
        for dismiss_count in range(MAX_FLOAT_ISLAND_REWARD_LAYERS + 1):
            observed = _wait_float_island_reward_transition(session,
                deadline=reward_deadline, require_reward=not reward_seen, log=log)
            if observed is None:
                reason = "float_island_reward_state_unknown" if reward_seen else "float_island_reward_not_found"
                return FloatIslandResult(False, True, reason)
            observed_frame, observed_items, observed_state = observed
            if isinstance(observed_state, tuple) and observed_state[0] == "page":
                if not reward_seen:
                    return FloatIslandResult(False, True, "float_island_reward_not_found")
                next_page = (observed_frame, observed_items, observed_state[1])
                break
            reward_seen = True
            if dismiss_count >= MAX_FLOAT_ISLAND_REWARD_LAYERS:
                log("[DreamweaverCommission] float island reward close limit reached; SafeStop")
                return FloatIslandResult(False, True, "float_island_reward_limit")
            # Keep the existing click cadence, then re-confirm the heading and
            # card on a fresh frame before closing; never click OCR residue.
            _business_click(session, *(_scaled_roi((440, 1340, 20, 20), observed_frame).center),
                confirm=lambda items, current: _scaled_roi((440, 1340, 20, 20), current)
                if is_float_island_reward(items, current) else None)
            reward_deadline = session.monotonic() + FLOAT_ISLAND_REWARD_TIMEOUT_SECONDS
            session.sleep(PAGE_WAIT_SECONDS)
        if next_page is None:
            return FloatIslandResult(False, True, "float_island_reward_limit")
        current = next_page


def recover_reward_page(
    session: CommissionSession,
    frame,
    items: list[OcrItem],
    *,
    log: Callable[[str], None] = print,
):
    """Close only confirmed reward layers, then return a freshly classified page."""
    current = (frame, items, DreamweaverEntryState.REWARD)
    for dismiss_count in range(MAX_REWARD_LAYERS + 1):
        current_frame, _, state = current
        if state is not DreamweaverEntryState.REWARD:
            return current
        if dismiss_count >= MAX_REWARD_LAYERS:
            log("[DreamweaverCommission] recovery reward close limit reached; SafeStop")
            return None
        _click_reference(session, current_frame, REWARD_OUTSIDE_POINT)
        session.sleep(PAGE_WAIT_SECONDS)
        current = _poll(session, detect_dreamweaver_entry_state)
        if current is None:
            return None
    return None


def run_commission_list(session: CommissionSession, *,
                        checkpoint_path=None,
                        log: Callable[[str], None] = print) -> DreamweaverCommissionResult:
    completed = 0
    if checkpoint_path is None:
        checkpoint_path = RunRecord("dreamweaver_purchase_state.json")
    unavailable = set()
    for _ in range(MAX_COMMISSION_STEPS):
        current = _poll(
            session,
            lambda items, frame: (
                DreamweaverEntryState.REWARD if is_reward(items)
                else classify_slots(items, frame)
            ),
        )
        if current is None:
            return DreamweaverCommissionResult(False, completed, "commission_list_unknown")
        frame, items, slots = current
        if slots is DreamweaverEntryState.REWARD:
            # The list may update before its delayed reward animation opens.
            # Drain that overlay before reading or submitting another row.
            log("[DreamweaverCommission] delayed commission reward; closing before reading slots")
            recovered = recover_reward_page(session, frame, items, log=log)
            if recovered is None:
                return DreamweaverCommissionResult(False, completed, "reward_recovery_failed")
            frame, items, _ = recovered
            slots = classify_slots(items, frame)
            if slots is None:
                return DreamweaverCommissionResult(False, completed, "commission_list_unknown")
        for slot in slots:
            log(
                f"[DreamweaverCommission] slot {slot.index + 1} = "
                f"{SLOT_STATE_LABELS[slot.state]}"
            )
        completed = sum(slot.state is SlotState.COMPLETED for slot in slots)
        actionable = [s for s in slots if s.state is not SlotState.COMPLETED
                      and not (s.index in unavailable and s.state is SlotState.CLOTHES_MISSING)]
        if not actionable:
            if completed == 3:
                log("[DreamweaverCommission] all three completed")
            else:
                log("[DreamweaverCommission] remaining clothes unavailable today; normal finish")
            if not _close_confirmed_popup(session, frame, is_commission_list):
                return DreamweaverCommissionResult(False, completed, "commission_close_unconfirmed")
            session.sleep(PAGE_WAIT_SECONDS)
            if _poll(session, lambda items, page: True if is_dreamweaver_main(items, page) else None) is None:
                return DreamweaverCommissionResult(False, completed, "main_not_restored")
            log("[DreamweaverCommission] returned to dreamweaver main")
            return DreamweaverCommissionResult(True, completed, "commission_completed" if completed == 3 else "daily_clothes_unavailable")
        slot = actionable[0]
        if slot is None or slot.state is SlotState.UNKNOWN:
            return DreamweaverCommissionResult(False, completed, "slot_state_unknown")
        if slot.state is SlotState.CLOTHES_MISSING:
            acquired = acquire_missing_clothes(session, slot,
                                                checkpoint_path=checkpoint_path, log=log)
            if not acquired:
                log(f"[DreamweaverCommission] slot {slot.index + 1} acquisition unconfirmed: {acquired.reason}")
                return DreamweaverCommissionResult(False, completed, "acquire_failed")
            if acquired.reason in {"attempts_exhausted"}:
                unavailable.add(slot.index)
                log(f"[DreamweaverCommission] slot {slot.index + 1} = 今日无法继续")
        elif not submit_slot(session, slot, log=log):
            return DreamweaverCommissionResult(False, completed, "submit_failed")
    return DreamweaverCommissionResult(False, completed, "loop_limit")


def run_dreamweaver_commission(
    session: CommissionSession,
    *,
    read_only: bool = False,
    checkpoint_path=None,
    log: Callable[[str], None] = print,
) -> DreamweaverCommissionResult:
    if checkpoint_path is None and not read_only:
        checkpoint_path = RunRecord("dreamweaver_purchase_state.json")
    if read_only:
        navigation = navigate_home_to_p3(
            session.capture_frame,
            lambda x, y: _business_click(session, x, y),
            sleep=session.sleep,
            log=log,
        )
        if not navigation.success:
            return DreamweaverCommissionResult(False, 0, "home_p3_not_reached")
        p3 = _poll_read_only(session, find_p3_entry)
        if p3 is None:
            return DreamweaverCommissionResult(False, 0, "dreamweaver_entry_not_found")
        frame, _, entry = p3
        log("[DreamweaverCommission] entering dreamweaver")
        # The animated two-line HOME label changes OCR width by 5px at 720p.
        # Keep this tolerance local to navigation, away from paid controls.
        if not _business_click(session, *entry.center, confirm=find_p3_entry, position_tolerance=8):
            return DreamweaverCommissionResult(False, 0, "dreamweaver_entry_unstable")
        session.sleep(PAGE_WAIT_SECONDS)
        menu = _poll_read_only(session, find_commission_menu_entry)
        if menu is None:
            return DreamweaverCommissionResult(False, 0, "dreamweaver_main_not_found")
        log("[DreamweaverCommission] dreamweaver main confirmed")
        log("[DreamweaverCommission] entering commission")
        entered = _enter_commission_list(session, menu, read_only=True, log=log)
        if entered is None:
            return DreamweaverCommissionResult(False, 0, "commission_not_entered")
        log("[DreamweaverCommission] commission list confirmed")
        frame, items, slots = entered
        log_read_only_slot_scan(items, frame, slots, log=log)
        completed = sum(slot.state is SlotState.COMPLETED for slot in slots)
        return DreamweaverCommissionResult(True, completed, "read_only_scan_completed")

    detected = detect_entry_state(
        session,
        detect_dreamweaver_entry_state,
        task_name="DreamweaverCommission",
        loading_classifier=home_random_visit_loading_classifier,
        unknown_capture_state="QJNN_DreamweaverUnknownStart",
        loading_timeout_state="QJNN_DreamweaverLoadingTimeout",
        log=log,
    )
    if detected is None:
        return DreamweaverCommissionResult(False, 0, "startup_state_unknown")
    state = detected.state
    state_frame = detected.frame
    state_items = detected.items
    reward_already_closed = False
    log(f"[DreamweaverCommission] recovery entry = {state.value}")

    if state is DreamweaverEntryState.REWARD:
        recovered = recover_reward_page(session, state_frame, state_items, log=log)
        if recovered is None:
            return DreamweaverCommissionResult(False, 0, "reward_recovery_failed")
        state_frame, state_items, state = recovered
        reward_already_closed = state is DreamweaverEntryState.FLOAT_ISLAND
        log(f"[DreamweaverCommission] recovery after reward = {state.value}")

    if state is DreamweaverEntryState.HOME:
        navigation = navigate_home_to_p3(
            session.capture_frame,
            lambda x, y: _business_click(session, x, y),
            sleep=session.sleep,
            log=log,
        )
        if not navigation.success:
            return DreamweaverCommissionResult(False, 0, "home_p3_not_reached")
        p3 = _poll(session, find_p3_entry)
        if p3 is None:
            return DreamweaverCommissionResult(False, 0, "dreamweaver_entry_not_found")
        frame, _, entry = p3
        log("[DreamweaverCommission] entering dreamweaver")
        if not _business_click(session, *entry.center, confirm=find_p3_entry, position_tolerance=8):
            return DreamweaverCommissionResult(False, 0, "dreamweaver_entry_unstable")
        session.sleep(PAGE_WAIT_SECONDS)
        main = _poll(session, lambda items, current: True if is_dreamweaver_main(items, current) else None)
        if main is None:
            return DreamweaverCommissionResult(False, 0, "dreamweaver_main_not_found")
        state = DreamweaverEntryState.DREAMWEAVER_MAIN

    if state is DreamweaverEntryState.PURCHASE_DIALOG:
        if not _cancel_purchase_and_return(session, state_frame, state_items):
            return DreamweaverCommissionResult(False, 0, "purchase_dialog_recovery_failed")
        state = DreamweaverEntryState.COMMISSION_LIST
    elif state is DreamweaverEntryState.SHOP_PRODUCT:
        if not _leave_shop_product(session, state_frame):
            return DreamweaverCommissionResult(False, 0, "shop_recovery_failed")
        state = DreamweaverEntryState.COMMISSION_LIST
    elif state is DreamweaverEntryState.OBTAIN_PATH:
        if not _return_from_obtain_path(session, state_frame):
            return DreamweaverCommissionResult(False, 0, "obtain_path_recovery_failed")
        state = DreamweaverEntryState.COMMISSION_LIST
    elif state is DreamweaverEntryState.STAGE_DETAIL:
        if not _return_from_stage(session, state_frame):
            return DreamweaverCommissionResult(False, 0, "stage_detail_recovery_failed")
        state = DreamweaverEntryState.COMMISSION_LIST
    elif state is DreamweaverEntryState.CHAPTER_MAP:
        if not _return_from_chapter_map(session, state_frame):
            return DreamweaverCommissionResult(False, 0, "chapter_map_recovery_failed")
        state = DreamweaverEntryState.COMMISSION_LIST

    completed_slots = 3 if state is DreamweaverEntryState.FLOAT_ISLAND else 0
    if state is DreamweaverEntryState.DREAMWEAVER_MAIN:
        log("[DreamweaverCommission] dreamweaver main confirmed")
        menu = _poll(session, find_commission_menu_entry)
        if menu is None:
            return DreamweaverCommissionResult(False, 0, "dreamweaver_main_not_found")
        log("[DreamweaverCommission] entering commission")
        if _enter_commission_list(session, menu, log=log) is None:
            return DreamweaverCommissionResult(False, 0, "commission_not_entered")
        state = DreamweaverEntryState.COMMISSION_LIST

    if state is DreamweaverEntryState.COMMISSION_LIST:
        log("[DreamweaverCommission] commission list confirmed")
        commission_result = run_commission_list(session,
                                                checkpoint_path=checkpoint_path, log=log)
        if not commission_result.success:
            return commission_result
        completed_slots = commission_result.completed_slots
        state = DreamweaverEntryState.DREAMWEAVER_MAIN

    if state is DreamweaverEntryState.DREAMWEAVER_MAIN:
        float_result = run_float_island(session, log=log)
    elif state is DreamweaverEntryState.FLOAT_ISLAND:
        float_snapshot = detect_float_island(state_items, state_frame)
        if float_snapshot is None:
            return DreamweaverCommissionResult(False, completed_slots, "float_island_recovery_unconfirmed")
        log("[DreamweaverCommission] float island confirmed")
        float_result = continue_float_island(
            session,
            current=(state_frame, state_items, float_snapshot),
            reward_already_closed=reward_already_closed,
            log=log,
        )
    else:
        return DreamweaverCommissionResult(False, completed_slots, "unsupported_recovery_state")
    if not float_result.success:
        return DreamweaverCommissionResult(
            False,
            completed_slots,
            float_result.reason,
        )

    main = _poll(
        session,
        lambda items, current: True if is_dreamweaver_main(items, current) else None,
    )
    if main is None:
        return DreamweaverCommissionResult(False, completed_slots, "dreamweaver_main_before_exit_not_found")
    main_frame, _, _ = main
    # Page recognition plus the arrow template gate the exit; HOME is terminal.
    log("[DreamweaverCommission] leaving dreamweaver")
    if not _safe_back_click(session, main_frame, is_dreamweaver_main):
        return DreamweaverCommissionResult(False, completed_slots, "dreamweaver_back_unconfirmed")
    session.sleep(PAGE_WAIT_SECONDS)
    home = _poll(
        session,
        lambda items, current: (
            _home_page(current) if _home_page(current) in {"P1", "P2", "P3"} else None
        ),
    )
    if home is None:
        return DreamweaverCommissionResult(False, completed_slots, "home_p3_not_restored")
    log(f"[DreamweaverCommission] HOME {home[2]} confirmed")
    log("[DreamweaverCommission] success")
    return DreamweaverCommissionResult(True, completed_slots, "completed" if completed_slots == 3 else "completed_with_unavailable_clothes")


@lru_cache(maxsize=1)
def _back_template():
    import cv2
    import numpy as np

    return cv2.imdecode(np.fromfile(TOP_BACK_TEMPLATE, dtype=np.uint8), cv2.IMREAD_COLOR)


@lru_cache(maxsize=1)
def _missing_plus_template():
    import cv2
    import numpy as np

    # Keep the original 42x43 crop and downstream cyan-stroke mask unchanged.
    path = PROJECT_ROOT / "assets/resource/image/dreamweaver/missing_clothes_plus.png"
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def find_missing_plus(frame, slot, *, log=None):
    import cv2
    import numpy as np

    if slot.state is not SlotState.CLOTHES_MISSING or not 0 <= slot.index < len(COMMISSION_SLOT_ROIS):
        return None
    normalized = cv2.resize(frame, REFERENCE_SIZE)
    # Search only the owned-status/entry strip of this row, not a predicted
    # offset from OCR or a shared x coordinate. Match cyan '+' strokes alone;
    # the circle/card/background and dynamic garment pixels are excluded.
    left, row_top, _, _ = COMMISSION_SLOT_ROIS[slot.index]
    top = row_top + 100
    search = normalized[top:top+120, left:left+330]

    def strokes(image):
        return cv2.inRange(cv2.cvtColor(image, cv2.COLOR_BGR2HSV),
                           (75, 60, 70), (105, 255, 255))

    template = strokes(_missing_plus_template())[6:37, 6:37]
    scores = cv2.matchTemplate(strokes(search), template, cv2.TM_CCOEFF_NORMED)
    _, score, _, location = cv2.minMaxLoc(scores)
    if log is not None:
        log(f"[DreamweaverCommission] slot {slot.index + 1} local entry glyph: "
            f"ROI={(left, top, 330, 120)} score={score:.3f}")
    if not np.isfinite(score) or score < MISSING_PLUS_THRESHOLD:
        return None
    # Two separate matching icons in one strip are ambiguous: do not guess.
    lx, ly = location
    remaining = scores.copy()
    remaining[max(0, ly-25):ly+26, max(0, lx-25):lx+26] = -1
    if cv2.minMaxLoc(remaining)[1] >= MISSING_PLUS_THRESHOLD:
        if log is not None:
            log("[DreamweaverCommission] ambiguous local entry icons; no click")
        return None
    x = left + location[0] + template.shape[1] // 2
    y = top + location[1] + template.shape[0] // 2
    safe = _scaled_roi((x - 8, y - 8, 16, 16), frame)
    if log is not None:
        log(f"[DreamweaverCommission] local entry confirmed: SafeRect={safe} point={safe.center}")
    return safe


def _match_back(frame, *, chapter=False):
    import cv2

    template = _back_template()
    if template is None:
        return 0.0, None
    area = _scaled_roi(CHAPTER_BACK_ROI if chapter else TOP_BACK_ROI, frame)
    search = frame[area.y:area.bottom, area.x:area.right]
    scale = min(frame.shape[:2]) / 900
    template = cv2.resize(template, (round(template.shape[1] * scale), round(template.shape[0] * scale)))
    if template.shape[0] > search.shape[0] or template.shape[1] > search.shape[1]:
        return 0.0, None
    _, score, _, location = cv2.minMaxLoc(cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED))
    if chapter:
        # Chapter maps can use a larger white glyph than the wardrobe arrow.
        variant = _chapter_back_template()
        if variant is not None:
            variant = cv2.resize(variant, (round(variant.shape[1] * scale), round(variant.shape[0] * scale)))
            if variant.shape[0] <= search.shape[0] and variant.shape[1] <= search.shape[1]:
                _, other_score, _, other_location = cv2.minMaxLoc(
                    cv2.matchTemplate(search, variant, cv2.TM_CCOEFF_NORMED))
                if other_score > score:
                    score, location, template = other_score, other_location, variant
    # Inset to the arrow's interior; never substitute the old top-left point.
    x, y = area.x + location[0], area.y + location[1]
    h, w = template.shape[:2]
    return float(score), Rect(x + w // 2 - w // 4, y + h // 2 - h // 4,
                              2 * (w // 4), 2 * (h // 4))


@lru_cache(maxsize=1)
def _chapter_back_template():
    import cv2
    import numpy as np
    path = PROJECT_ROOT / "assets/resource/image/common/chapter_back_arrow.png"
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR) if path.is_file() else None


def find_back_button(frame, *, chapter=False):
    if _home_page(frame) in {"P1", "P2", "P3"}:
        return None
    score, safe = _match_back(frame, chapter=chapter)
    return safe if score >= TOP_BACK_THRESHOLD else None


def chapter_back_score(frame) -> float:
    return _match_back(frame, chapter=True)[0]


class DreamweaverCommissionAction(CustomAction):
    def __init__(self, run_state=None) -> None:
        super().__init__()
        self._read_only = False
        self._run_state = run_state

    def run(self, context, argv) -> bool:
        # This action has no configurable parameters after removing shopping
        # budgets. MaaFramework may serialize omitted parameters as JSON null.
        controller = context.tasker.controller

        class RuntimeSession:
            purchase_fence_path = DATA_ROOT / "state" / "dreamweaver_purchase_fence.json"
            purchase_evidence_root = DATA_ROOT / "images" / "debug" / "pipeline" / "dreamweaver_purchase"

            @staticmethod
            def capture_frame():
                job = controller.post_screencap()
                job.wait()
                if not getattr(job, "succeeded", False):
                    raise RuntimeError("织梦人学会委托截图失败")
                cached = getattr(controller, "cached_image", None)
                frame = cached() if callable(cached) else cached
                if frame is None or getattr(frame, "size", 0) == 0:
                    raise RuntimeError("织梦人学会委托截图为空")
                return frame

            @staticmethod
            def recognize_text(frame) -> list[OcrItem]:
                detail = context.run_recognition("QJNN_AgentOcrBase", frame)
                results = list(getattr(detail, "all_results", []) or []) if detail else []
                converted = []
                for result in results:
                    box = result.box
                    rect = Rect(int(box.x), int(box.y), int(box.w), int(box.h)) if hasattr(box, "x") else Rect(*(int(v) for v in list(box)[:4]))
                    converted.append(OcrItem(str(result.text), rect, float(getattr(result, "score", 0.0))))
                return converted

            @staticmethod
            def recognize_purchase_quantity(frame):
                return read_purchase_quantity_field(frame, RuntimeSession.recognize_text)

            @staticmethod
            def recognize_purchase_product(frame):
                return read_purchase_product_title(frame, RuntimeSession.recognize_text)

            @staticmethod
            def click(x: int, y: int) -> None:
                print(f"[DreamweaverCommission] click submitted: ({x}, {y})")
                job = controller.post_click(x, y)
                job.wait()
                if hasattr(job, "succeeded") and not job.succeeded:
                    raise RuntimeError(f"织梦人学会委托点击失败：({x}, {y})")
                print(f"[DreamweaverCommission] click completed: ({x}, {y})")

            sleep = staticmethod(time.sleep)
            monotonic = staticmethod(time.monotonic)
            chapter_back_score = staticmethod(chapter_back_score)

        try:
            session = RuntimeSession()
            checkpoint_path = None
            if not self._read_only:
                session = guard_runtime_session(context, session)
                checkpoint_path = run_record(self._run_state, "dreamweaver_purchase_state.json")
            result = run_dreamweaver_commission(
                session,
                read_only=self._read_only,
                **({"checkpoint_path": checkpoint_path} if checkpoint_path is not None else {}),
            )
            if not result.success:
                mode = "ReadOnly" if self._read_only else "Formal"
                print(
                    f"[DreamweaverCommission] {mode} SafeStop "
                    f"reason={result.reason} completed_slots={result.completed_slots}"
                )
            return result.success
        except Exception as exc:
            print(f"织梦人学会委托 SafeStop：{exc}")
            return False


class DreamweaverCommissionReadOnlyAction(DreamweaverCommissionAction):
    def __init__(self) -> None:
        super().__init__()
        self._read_only = True
