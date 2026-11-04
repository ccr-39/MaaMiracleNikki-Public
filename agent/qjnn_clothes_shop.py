from __future__ import annotations

from app_paths import DATA_ROOT

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from qjnn_alliance_commission import OcrItem, Rect, find_text_box
from qjnn_common_popup import (
    GlobalTransientResult,
    guard_runtime_session,
    handle_transient_if_present,
)
from qjnn_home import detect_home_page
from qjnn_run_state import RunRecord, run_record
from qjnn_home_random_visit import home_random_visit_loading_classifier

try:
    from maa.custom_action import CustomAction
    from maa.custom_recognition import CustomRecognition
except ImportError:  # MaaFramework is optional for offline tests.
    CustomAction = object
    CustomRecognition = object


REFERENCE_SIZE = (900, 1600)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLOTHES_SHOP_CHECKPOINT = DATA_ROOT / "config" / "clothes_shop_daily_state.json"
OCR_ATTEMPTS = 3
OCR_RETRY_SECONDS = 0.4
PAGE_WAIT_SECONDS = 0.8
PURCHASE_TIMEOUT_SECONDS = 10.0
PURCHASE_POLL_SECONDS = 0.4
MAX_QUANTITY_ADJUSTMENTS = 12
MAX_RETURN_CLICKS = 6

HOME_WARDROBE_ROI = (150, 1450, 190, 150)
WARDROBE_THEME_ROI = (110, 130, 200, 110)
THEME_TITLE_ROI = (250, 350, 400, 120)
THEME_CONFIRM_ROI = (450, 850, 260, 170)
THEME_CANCEL_ROI = (150, 850, 260, 170)
THEME_ITEM_ROI = (120, 210, 430, 230)
DETAIL_ACTION_ROI = (500, 1000, 350, 300)
ACQUISITION_SHOP_ROI = (450, 650, 380, 220)
STORE_TITLE_ROI = (250, 20, 400, 130)
STORE_BUY_ROI = (500, 600, 350, 220)
QUANTITY_TITLE_ROI = (200, 730, 500, 150)
QUANTITY_VALUE_ROI = (350, 840, 200, 120)
QUANTITY_CONFIRM_ROI = (500, 980, 260, 180)
PURCHASE_PRICE_ROI = (415, 950, 200, 70)
PURCHASE_COIN_ROI = (364, 958, 50, 50)

THEME_RADIO_CENTER = (211, 510)
THEME_RADIO_RADIUS = 9
THEME_RADIO_SELECTED_SATURATION = 35.0
QUANTITY_MINUS_ONE_POINT = (305, 897)
QUANTITY_PLUS_ONE_POINT = (593, 897)
TOP_BACK_POINT = (70, 65)


@dataclass(frozen=True)
class ClothesShopResult:
    success: bool
    purchase_submitted: bool
    return_clicks: int
    reason: str


class ClothesShopState(str, Enum):
    HOME = "HOME"
    WARDROBE = "WARDROBE"
    THEME_POPUP = "THEME_POPUP"
    THEME_ITEMS = "THEME_ITEMS"
    ITEM_DETAIL = "ITEM_DETAIL"
    ACQUISITION_POPUP = "ACQUISITION_POPUP"
    STORE = "STORE"
    QUANTITY_POPUP = "QUANTITY_POPUP"


class PurchaseCheckpointStatus(str, Enum):
    VALID = "VALID"
    MISSING = "MISSING"
    STALE = "STALE"
    INVALID = "INVALID"


@dataclass(frozen=True)
class ClothesShopSnapshot:
    state: ClothesShopState
    value: object


@dataclass(frozen=True)
class PurchaseCheckpoint:
    day: str
    status: str


@dataclass(frozen=True)
class PurchaseCheckpointLoad:
    status: PurchaseCheckpointStatus
    checkpoint: PurchaseCheckpoint | None
    reason: str


class ClothesShopSession(Protocol):
    def capture_frame(self): ...

    def recognize_text(self, frame) -> list[OcrItem]: ...

    def click(self, x: int, y: int) -> None: ...

    def sleep(self, seconds: float) -> None: ...

    def monotonic(self) -> float: ...


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text)).replace(":", "：")


def _scaled_rect(rect: tuple[int, int, int, int], width: int, height: int) -> Rect:
    x, y, w, h = rect
    left = round(x * width / REFERENCE_SIZE[0])
    top = round(y * height / REFERENCE_SIZE[1])
    right = round((x + w) * width / REFERENCE_SIZE[0])
    bottom = round((y + h) * height / REFERENCE_SIZE[1])
    return Rect(left, top, max(1, right - left), max(1, bottom - top))


def map_reference_point(point: tuple[int, int], width: int, height: int) -> tuple[int, int]:
    return (
        round(point[0] * width / REFERENCE_SIZE[0]),
        round(point[1] * height / REFERENCE_SIZE[1]),
    )


def _find(items: list[OcrItem], text: str, frame, roi) -> Rect | None:
    height, width = frame.shape[:2]
    return find_text_box(items, text, roi=_scaled_rect(roi, width, height))


def find_wardrobe_entry(items: list[OcrItem], frame) -> Rect | None:
    if str(getattr(detect_home_page(frame), "page", "Unknown")) not in {"P1", "P2", "P3"}:
        return None
    return _find(items, "衣柜", frame, HOME_WARDROBE_ROI)


def find_custom_theme(items: list[OcrItem], frame) -> Rect | None:
    return _find(items, "自定义主题", frame, WARDROBE_THEME_ROI)


def find_theme_popup(items: list[OcrItem], frame) -> tuple[Rect, Rect] | None:
    title = _find(items, "自定义主题", frame, THEME_TITLE_ROI)
    confirm = _find(items, "确定", frame, THEME_CONFIRM_ROI)
    if title is None or confirm is None:
        return None
    # The first theme is positional and user-renamable. Target its radio
    # interior only after recognizing the modal, even if its label has no OCR.
    height, width = frame.shape[:2]
    cx, cy = THEME_RADIO_CENTER
    radius = THEME_RADIO_RADIUS
    radio = _scaled_rect((cx - radius, cy - radius, radius * 2, radius * 2), width, height)
    return radio, confirm


def find_theme_popup_cancel(items: list[OcrItem], frame) -> Rect | None:
    title = _find(items, "自定义主题", frame, THEME_TITLE_ROI)
    confirm = _find(items, "确定", frame, THEME_CONFIRM_ROI)
    cancel = _find(items, "取消", frame, THEME_CANCEL_ROI)
    return cancel if title and confirm and cancel else None


def theme_radio_selected(frame) -> bool:
    """Inspect only the radio centre; selected is filled pink, unselected is white."""
    import cv2

    height, width = frame.shape[:2]
    cx, cy = map_reference_point(THEME_RADIO_CENTER, width, height)
    radius = max(2, round(THEME_RADIO_RADIUS * min(width / 900, height / 1600)))
    crop = frame[max(0, cy - radius) : cy + radius + 1, max(0, cx - radius) : cx + radius + 1]
    if crop.size == 0:
        return False
    saturation = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)[:, :, 1]
    return float(saturation.mean()) >= THEME_RADIO_SELECTED_SATURATION


def find_theme_item(items: list[OcrItem], frame) -> Rect | None:
    # The selected-theme label is above y=210. Restricting this ROI selects the
    # named clothing card even when the theme contains multiple clothes.
    return _find(items, "搭配购物节", frame, THEME_ITEM_ROI)


def find_detail_acquisition(items: list[OcrItem], frame) -> Rect | None:
    title = _find(items, "搭配购物节", frame, (450, 250, 400, 180))
    action = _find(items, "获取途径", frame, DETAIL_ACTION_ROI)
    return action if title and action else None


def find_acquisition_shop(items: list[OcrItem], frame) -> Rect | None:
    title = _find(items, "获取途径", frame, (250, 350, 400, 160))
    shop = _find(items, "服装店", frame, ACQUISITION_SHOP_ROI)
    return shop if title and shop else None


def find_store_buy(items: list[OcrItem], frame) -> Rect | None:
    title = _find(items, "服装店", frame, STORE_TITLE_ROI)
    item = _find(items, "搭配购物节", frame, (450, 130, 400, 180))
    buy = _find(items, "立即购买", frame, STORE_BUY_ROI)
    return buy if title and item and buy else None


def is_store_page(items: list[OcrItem], frame) -> bool:
    return find_store_buy(items, frame) is not None and parse_quantity(items, frame) is None


def parse_quantity(items: list[OcrItem], frame) -> int | None:
    title = _find(items, "请确认购买的数量", frame, QUANTITY_TITLE_ROI)
    if title is None:
        return None
    height, width = frame.shape[:2]
    roi = _scaled_rect(QUANTITY_VALUE_ROI, width, height)
    candidates = []
    for item in items:
        cx, cy = item.box.center
        text = _normalize_text(item.text)
        if roi.x <= cx <= roi.right and roi.y <= cy <= roi.bottom and re.fullmatch(r"\d+", text):
            candidates.append(item)
    if not candidates:
        return None
    best = max(candidates, key=lambda item: (float(item.score), item.box.height))
    return int(_normalize_text(best.text))


def find_quantity_confirm(items: list[OcrItem], frame) -> Rect | None:
    if parse_quantity(items, frame) is None:
        return None
    return _find(items, "确定", frame, QUANTITY_CONFIRM_ROI)


def purchase_coin_quote(items: list[OcrItem], frame) -> int | None:
    """Recognize the exact three-item modal, its total, and the gold coin icon.

    Currency is never inferred from the price number: the yellow coin must be
    visible in its own narrow area; pink diamond / unknown icons are rejected.
    """
    import cv2
    if parse_quantity(items, frame) != 3 or find_quantity_confirm(items, frame) is None:
        return None
    if _find(items, "搭配购物节", frame, (250, 420, 400, 90)) is None:
        return None
    height, width = frame.shape[:2]
    price_roi = _scaled_rect(PURCHASE_PRICE_ROI, width, height)
    amounts = []
    for item in items:
        x, y = item.box.center
        text = _normalize_text(item.text)
        if (item.score >= .8 and price_roi.x <= x <= price_roi.right
                and price_roi.y <= y <= price_roi.bottom and re.fullmatch(r"\d{1,6}", text)):
            amounts.append(int(text))
    if len(amounts) != 1 or amounts[0] <= 0:
        return None
    icon = _scaled_rect(PURCHASE_COIN_ROI, width, height)
    crop = frame[icon.y:icon.bottom, icon.x:icon.right]
    if crop.size == 0:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    gold = (hsv[:, :, 0] >= 16) & (hsv[:, :, 0] <= 42) & (hsv[:, :, 1] >= 65) & (hsv[:, :, 2] >= 110)
    pink = (hsv[:, :, 0] >= 140) & (hsv[:, :, 1] >= 65)
    if float(gold.mean()) < .35 or float(pink.mean()) > .10:
        return None
    return amounts[0]


def detect_clothes_shop_state(items: list[OcrItem], frame) -> ClothesShopSnapshot | None:
    """Classify only the eight resumable business surfaces, most-specific first."""
    quantity = parse_quantity(items, frame)
    if quantity is not None and find_quantity_confirm(items, frame) is not None:
        return ClothesShopSnapshot(ClothesShopState.QUANTITY_POPUP, quantity)
    acquisition_shop = find_acquisition_shop(items, frame)
    if acquisition_shop is not None:
        return ClothesShopSnapshot(ClothesShopState.ACQUISITION_POPUP, acquisition_shop)
    theme_popup = find_theme_popup(items, frame)
    if theme_popup is not None:
        return ClothesShopSnapshot(ClothesShopState.THEME_POPUP, theme_popup)
    store_buy = find_store_buy(items, frame)
    if store_buy is not None:
        return ClothesShopSnapshot(ClothesShopState.STORE, store_buy)
    detail_acquisition = find_detail_acquisition(items, frame)
    if detail_acquisition is not None:
        return ClothesShopSnapshot(ClothesShopState.ITEM_DETAIL, detail_acquisition)
    theme_item = find_theme_item(items, frame)
    if theme_item is not None:
        return ClothesShopSnapshot(ClothesShopState.THEME_ITEMS, theme_item)
    custom_theme = find_custom_theme(items, frame)
    if custom_theme is not None:
        return ClothesShopSnapshot(ClothesShopState.WARDROBE, custom_theme)
    wardrobe = find_wardrobe_entry(items, frame)
    if wardrobe is not None:
        page = str(getattr(detect_home_page(frame), "page", "Unknown"))
        return ClothesShopSnapshot(ClothesShopState.HOME, (page, wardrobe))
    return None


def _today_text(today: date | None = None) -> str:
    return (today or date.today()).isoformat()


def load_purchase_checkpoint(
    path: Path = CLOTHES_SHOP_CHECKPOINT,
    *,
    today: date | None = None,
) -> PurchaseCheckpointLoad:
    if isinstance(path, RunRecord):
        payload = path.read()
        if payload is None:
            return PurchaseCheckpointLoad(PurchaseCheckpointStatus.MISSING, None, "run_record_empty")
    elif not path.is_file():
        return PurchaseCheckpointLoad(PurchaseCheckpointStatus.MISSING, None, "file_missing")
    try:
        if not isinstance(path, RunRecord):
            payload = json.loads(path.read_text(encoding="utf-8"))
        checkpoint = PurchaseCheckpoint(payload["date"], payload["status"])
        if not isinstance(checkpoint.day, str) or checkpoint.status not in {"in_flight", "completed"}:
            raise ValueError("invalid checkpoint schema")
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return PurchaseCheckpointLoad(PurchaseCheckpointStatus.INVALID, None, str(exc))
    if checkpoint.day != _today_text(today):
        return PurchaseCheckpointLoad(PurchaseCheckpointStatus.STALE, checkpoint, "date_mismatch")
    return PurchaseCheckpointLoad(PurchaseCheckpointStatus.VALID, checkpoint, "ok")


def save_purchase_checkpoint(
    status: str,
    path: Path = CLOTHES_SHOP_CHECKPOINT,
    *,
    today: date | None = None,
) -> None:
    if status not in {"in_flight", "completed"}:
        raise ValueError("purchase checkpoint status must be in_flight or completed")
    if isinstance(path, RunRecord):
        path.write({"date": _today_text(today), "status": status})
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps({"date": _today_text(today), "status": status}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _snapshot(session: ClothesShopSession):
    frame = session.capture_frame()
    return frame, session.recognize_text(frame)


def _retry_find(session: ClothesShopSession, selector, *, attempts: int = OCR_ATTEMPTS):
    for attempt in range(attempts):
        frame, items = _snapshot(session)
        value = selector(items, frame)
        if value is not None and value is not False:
            return frame, items, value
        transient, fresh_frame, fresh_items = handle_transient_if_present(
            session,
            frame,
            items,
            loading_classifier=home_random_visit_loading_classifier,
        )
        if transient is GlobalTransientResult.ERROR:
            return None
        if transient in {
            GlobalTransientResult.HANDLED_POPUP,
            GlobalTransientResult.WAITED_LOADING,
        }:
            value = selector(list(fresh_items), fresh_frame)
            if value is not None and value is not False:
                return fresh_frame, list(fresh_items), value
        if attempt + 1 < attempts:
            session.sleep(OCR_RETRY_SECONDS)
    return None


def _click_box(session: ClothesShopSession, box: Rect) -> None:
    session.click(*box.center)
    session.sleep(PAGE_WAIT_SECONDS)


def _click_point(session: ClothesShopSession, frame, point: tuple[int, int]) -> None:
    height, width = frame.shape[:2]
    session.click(*map_reference_point(point, width, height))
    session.sleep(PAGE_WAIT_SECONDS)


def _adjust_quantity_to_three(session: ClothesShopSession, *, log: Callable[[str], None]) -> tuple[object, list[OcrItem], Rect] | None:
    for adjustment in range(MAX_QUANTITY_ADJUSTMENTS + 1):
        found = _retry_find(session, lambda items, frame: parse_quantity(items, frame))
        if found is None:
            log("[ClothesShop] quantity popup/value not recognized; SafeStop")
            return None
        frame, items, quantity = found
        log(f"[ClothesShop] observed purchase quantity={quantity}")
        if quantity == 3:
            confirm = find_quantity_confirm(items, frame)
            if confirm is None:
                log("[ClothesShop] quantity is 3 but confirm button is not recognized")
                return None
            return frame, items, confirm
        if adjustment >= MAX_QUANTITY_ADJUSTMENTS:
            break
        point = QUANTITY_PLUS_ONE_POINT if quantity < 3 else QUANTITY_MINUS_ONE_POINT
        _click_point(session, frame, point)
    log("[ClothesShop] quantity adjustment limit exceeded; SafeStop")
    return None


def _wait_purchase_committed(session: ClothesShopSession) -> bool:
    deadline = session.monotonic() + PURCHASE_TIMEOUT_SECONDS
    while session.monotonic() < deadline:
        frame, items = _snapshot(session)
        if parse_quantity(items, frame) is not None:
            session.sleep(PURCHASE_POLL_SECONDS)
            continue
        if is_store_page(items, frame):
            return True
        transient, fresh_frame, fresh_items = handle_transient_if_present(
            session,
            frame,
            items,
            loading_classifier=home_random_visit_loading_classifier,
        )
        if transient is GlobalTransientResult.ERROR:
            return False
        if transient in {
            GlobalTransientResult.HANDLED_POPUP,
            GlobalTransientResult.WAITED_LOADING,
        }:
            if parse_quantity(list(fresh_items), fresh_frame) is None and is_store_page(
                list(fresh_items), fresh_frame
            ):
                return True
        session.sleep(PURCHASE_POLL_SECONDS)
    return False


def _known_return_page(items: list[OcrItem], frame) -> bool:
    return any(
        selector(items, frame) is not None
        for selector in (find_store_buy, find_detail_acquisition, find_theme_item, find_custom_theme)
    )


def _return_to_home_p2(session: ClothesShopSession, *, log: Callable[[str], None]) -> tuple[bool, int, str]:
    clicks = 0
    while clicks <= MAX_RETURN_CLICKS:
        frame, items = _snapshot(session)
        page = str(getattr(detect_home_page(frame), "page", "Unknown"))
        if page in {"P1", "P2", "P3"}:
            return True, clicks, "completed"
        if clicks >= MAX_RETURN_CLICKS:
            return False, clicks, "return_limit"
        if not _known_return_page(items, frame):
            transient, fresh_frame, fresh_items = handle_transient_if_present(
                session,
                frame,
                items,
                loading_classifier=home_random_visit_loading_classifier,
            )
            if transient is GlobalTransientResult.ERROR:
                return False, clicks, "return_transient_error"
            if transient in {
                GlobalTransientResult.HANDLED_POPUP,
                GlobalTransientResult.WAITED_LOADING,
            }:
                frame, items = fresh_frame, list(fresh_items)
            if not _known_return_page(items, frame):
                return False, clicks, "return_page_unknown"
        _click_point(session, frame, TOP_BACK_POINT)
        clicks += 1
    return False, clicks, "return_limit"


def run_clothes_shop_purchase(
    session: ClothesShopSession,
    *,
    log: Callable[[str], None] = print,
    checkpoint_path: Path | RunRecord | None = None,
    today: date | None = None,
) -> ClothesShopResult:
    """Resume at the detected business state and preserve the one-shot purchase gate."""
    if checkpoint_path is None:
        checkpoint_path = RunRecord("clothes_shop_daily_state.json")
    current = _retry_find(session, detect_clothes_shop_state)
    if current is None:
        return ClothesShopResult(False, False, 0, "business_state_unknown")
    frame, items, snapshot = current
    log(f"[ClothesShop] resume state={snapshot.state.value}")
    loaded = load_purchase_checkpoint(checkpoint_path, today=today)
    if loaded.status is PurchaseCheckpointStatus.INVALID:
        log(f"[ClothesShop] invalid purchase checkpoint: {loaded.reason}; SafeStop")
        return ClothesShopResult(False, False, 0, "checkpoint_invalid")
    if loaded.status is PurchaseCheckpointStatus.VALID:
        assert loaded.checkpoint is not None
        if loaded.checkpoint.status == "in_flight":
            log("[ClothesShop] an earlier purchase is in-flight/ambiguous; zero-click SafeStop")
            return ClothesShopResult(False, False, 0, "purchase_in_flight_ambiguous")
        if snapshot.state is ClothesShopState.HOME:
            page, _ = snapshot.value
            log("[ClothesShop] already purchased in this queue; skipping repeat purchase")
            return ClothesShopResult(page in {"P1", "P2", "P3"}, False, 0, "already_completed")
        if snapshot.state not in {
            ClothesShopState.STORE,
            ClothesShopState.ITEM_DETAIL,
            ClothesShopState.THEME_ITEMS,
            ClothesShopState.WARDROBE,
        }:
            log("[ClothesShop] completed checkpoint conflicts with current page; SafeStop")
            return ClothesShopResult(False, False, 0, "completed_checkpoint_page_conflict")
        returned, return_clicks, reason = _return_to_home_p2(session, log=log)
        return ClothesShopResult(returned, False, return_clicks, reason)

    def advance(expected: ClothesShopState, failure_reason: str):
        def expected_snapshot(current_items, current_frame):
            candidate = detect_clothes_shop_state(current_items, current_frame)
            return candidate if candidate is not None and candidate.state is expected else None

        found = _retry_find(session, expected_snapshot)
        if found is None:
            return None, ClothesShopResult(False, False, 0, failure_reason)
        next_frame, next_items, next_snapshot = found
        return (next_frame, next_items, next_snapshot), None

    while snapshot.state is not ClothesShopState.QUANTITY_POPUP:
        if snapshot.state is ClothesShopState.HOME:
            _, wardrobe = snapshot.value
            _click_box(session, wardrobe)
            expected, reason = ClothesShopState.WARDROBE, "wardrobe_not_entered"
        elif snapshot.state is ClothesShopState.WARDROBE:
            _click_box(session, snapshot.value)
            expected, reason = ClothesShopState.THEME_POPUP, "theme_popup_not_found"
        elif snapshot.state is ClothesShopState.THEME_POPUP:
            theme_row, confirm = snapshot.value
            if not theme_radio_selected(frame):
                log("[ClothesShop] first theme not selected; selecting first-row radio")
                _click_box(session, theme_row)
                selected = _retry_find(
                    session,
                    lambda current_items, current_frame: (
                        detect_clothes_shop_state(current_items, current_frame)
                        if theme_radio_selected(current_frame)
                        else None
                    ),
                )
                if selected is None or selected[2].state is not ClothesShopState.THEME_POPUP:
                    return ClothesShopResult(False, False, 0, "theme_selection_unconfirmed")
                frame, items, snapshot = selected
                _, confirm = snapshot.value
            else:
                log("[ClothesShop] theme already selected; radio click skipped")
            _click_box(session, confirm)
            expected, reason = ClothesShopState.THEME_ITEMS, "theme_item_not_found"
        elif snapshot.state is ClothesShopState.THEME_ITEMS:
            _click_box(session, snapshot.value)
            expected, reason = ClothesShopState.ITEM_DETAIL, "item_detail_not_found"
        elif snapshot.state is ClothesShopState.ITEM_DETAIL:
            _click_box(session, snapshot.value)
            expected, reason = ClothesShopState.ACQUISITION_POPUP, "acquisition_popup_not_found"
        elif snapshot.state is ClothesShopState.ACQUISITION_POPUP:
            _click_box(session, snapshot.value)
            expected, reason = ClothesShopState.STORE, "store_not_found"
        elif snapshot.state is ClothesShopState.STORE:
            _click_box(session, snapshot.value)
            expected, reason = ClothesShopState.QUANTITY_POPUP, "quantity_popup_not_found"
        else:  # Defensive exhaustiveness for future enum members.
            return ClothesShopResult(False, False, 0, "business_state_unhandled")

        advanced, failure = advance(expected, reason)
        if failure is not None:
            return failure
        assert advanced is not None
        frame, items, snapshot = advanced

    adjusted = _adjust_quantity_to_three(session, log=log)
    if adjusted is None:
        return ClothesShopResult(False, False, 0, "quantity_not_three")
    _, _, purchase_confirm = adjusted

    # Re-read the final modal immediately before arming the one-shot fence.
    # Missing / ambiguous prices or currency never authorize a purchase.
    quote_frame, quote_items = _snapshot(session)
    amount = purchase_coin_quote(quote_items, quote_frame)
    if amount is None:
        log("[ClothesShop] purchase blocked: price/currency unconfirmed")
        return ClothesShopResult(False, False, 0, "purchase_quote_unconfirmed")
    purchase_confirm = find_quantity_confirm(quote_items, quote_frame)
    assert purchase_confirm is not None
    log(f"[ClothesShop] purchase verified: {amount} coins; quantity=3")

    # Persist the ambiguity fence before the sole confirm submission. A crash
    # after this write can only SafeStop on restart; it can never buy again.
    try:
        save_purchase_checkpoint("in_flight", checkpoint_path, today=today)
    except OSError as exc:
        log(f"[ClothesShop] cannot persist purchase fence: {exc}; zero-click SafeStop")
        return ClothesShopResult(False, False, 0, "checkpoint_write_failed")
    session.click(*purchase_confirm.center)
    log("[ClothesShop] purchase confirmation submitted exactly once")
    if not _wait_purchase_committed(session):
        log("[ClothesShop] purchase result unknown/timeout; SafeStop without retry")
        return ClothesShopResult(False, True, 0, "purchase_result_unknown")
    try:
        save_purchase_checkpoint("completed", checkpoint_path, today=today)
    except OSError as exc:
        log(f"[ClothesShop] cannot commit purchase checkpoint: {exc}; SafeStop")
        return ClothesShopResult(False, True, 0, "checkpoint_commit_failed")

    returned, return_clicks, reason = _return_to_home_p2(session, log=log)
    return ClothesShopResult(returned, True, return_clicks, reason)


def _ocr_items_from_detail(detail) -> list[OcrItem]:
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
        converted.append(OcrItem(str(result.text), rect, float(getattr(result, "score", 0.0))))
    return converted


class ClothesShopStateRecognition(CustomRecognition):
    """Pipeline gate that lets the shared startup chain handle non-business pages."""

    def analyze(self, context, argv):
        try:
            detail = context.run_recognition("QJNN_AgentOcrBase", argv.image)
            snapshot = detect_clothes_shop_state(_ocr_items_from_detail(detail), argv.image)
            if snapshot is None:
                # Route a verified shared snowflake into the business action;
                # _retry_find then delegates waiting to GlobalTransientGuard.
                if home_random_visit_loading_classifier([], argv.image):
                    return CustomRecognition.AnalyzeResult(
                        box=[0, 0, 1, 1],
                        detail={"state": "GLOBAL_LOADING"},
                    )
                return CustomRecognition.AnalyzeResult(box=None, detail={"state": "UNKNOWN"})
            value = snapshot.value
            if isinstance(value, Rect):
                target = value
            elif snapshot.state is ClothesShopState.HOME:
                target = value[1]
            elif snapshot.state is ClothesShopState.THEME_POPUP:
                target = value[0]
            else:
                target = Rect(0, 0, 1, 1)
            return CustomRecognition.AnalyzeResult(
                box=[target.x, target.y, target.width, target.height],
                detail={"state": snapshot.state.value},
            )
        except Exception as exc:
            return CustomRecognition.AnalyzeResult(
                box=None,
                detail={"state": "UNKNOWN", "error": str(exc)},
            )


class ClothesShopPurchaseAction(CustomAction):
    def __init__(self, run_state=None):
        super().__init__()
        self._run_state = run_state

    def run(self, context, argv) -> bool:
        controller = context.tasker.controller

        class RuntimeSession:
            @staticmethod
            def capture_frame():
                job = controller.post_screencap()
                job.wait()
                if hasattr(job, "succeeded") and not job.succeeded:
                    raise RuntimeError("clothes shop screenshot failed")
                cached = getattr(controller, "cached_image", None)
                frame = cached() if callable(cached) else cached
                if frame is None or getattr(frame, "size", 0) == 0:
                    raise RuntimeError("clothes shop screenshot is empty")
                return frame

            @staticmethod
            def recognize_text(frame) -> list[OcrItem]:
                detail = context.run_recognition("QJNN_AgentOcrBase", frame)
                return _ocr_items_from_detail(detail)

            @staticmethod
            def click(x: int, y: int) -> None:
                job = controller.post_click(x, y)
                job.wait()
                if hasattr(job, "succeeded") and not job.succeeded:
                    raise RuntimeError(f"clothes shop click failed: ({x}, {y})")

            sleep = staticmethod(time.sleep)
            monotonic = staticmethod(time.monotonic)

        try:
            session = guard_runtime_session(context, RuntimeSession())
            checkpoint_path = run_record(self._run_state, "clothes_shop_daily_state.json")
            result = run_clothes_shop_purchase(session, checkpoint_path=checkpoint_path)
            if not result.success:
                print(f"[ClothesShop] SafeStop reason={result.reason}, purchase_submitted={result.purchase_submitted}")
            return result.success
        except Exception as exc:
            print(f"[ClothesShop] unhandled failure; SafeStop: {exc}")
            return False
