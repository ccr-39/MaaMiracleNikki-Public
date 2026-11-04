from __future__ import annotations

from app_paths import DATA_ROOT

import re
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from qjnn_alliance_commission import Rect
from qjnn_clothes_shop import find_wardrobe_entry
from qjnn_common_popup import (
    _make_runtime_session, detect_common_safe_popup, poll_target_with_common_popups,
)
from qjnn_diagnostic import DiagnosticFrameStore
from qjnn_dreamweaver_commission import (
    CustomAction, _business_click, _find, _items_in, _normalize, _scaled_roi,
    _safe_back_click, _close_confirmed_popup, _wait_before_click,
    find_obtain_stage_route, is_obtain_path, is_chapter_map,
    parse_stage_detail, is_stage_clear_reward, run_stage_flow, dismiss_stage_clear_reward,
    wait_for_stage_detail,
)
from qjnn_home_random_visit import home_random_visit_loading_classifier

DEFAULT_MAX_ITEMS = 4
MAX_ITEM_LIMIT = 999
MAX_LIST_SWIPES = 40
MAX_SWIPES = 4
MAX_STEPS = 40
MAX_RETURN_ATTEMPTS = 3
ROOT = Path(__file__).resolve().parent.parent


class State(str, Enum):
    HOME = "home"
    WARDROBE = "wardrobe"
    THEME = "theme"
    HAIR = "hair"
    DRESS = "dress"
    COAT = "coat"
    TOP = "top"
    BOTTOM = "bottom"
    SOCKS = "socks"
    SHOES = "shoes"
    ACCESSORY = "accessory"
    MAKEUP = "makeup"
    DETAIL = "detail"
    ACQUISITION = "acquisition"
    STAGE = "stage"
    SETTLEMENT = "settlement"
    CHAPTER = "chapter"


CATEGORIES = (State.HAIR, State.DRESS, State.COAT, State.TOP, State.BOTTOM,
              State.SOCKS, State.SHOES, State.ACCESSORY, State.MAKEUP)
CATEGORY_LABELS = dict(zip(CATEGORIES, ("发型", "连衣裙", "外套", "上衣", "下装",
                                       "袜子", "鞋子", "饰品", "妆容")))


@dataclass(frozen=True)
class Card:
    number: str
    name: str  # Observed identity only; never a configured garment name.
    box: Rect


@dataclass(frozen=True)
class Result:
    success: bool
    processed_items: int
    reason: str


def theme_dialog(items, frame, *, log=None):
    if not _find(items, "自定义主题", frame, (250, 350, 400, 120)):
        return None
    confirm = _find(items, "确定", frame, (450, 850, 260, 170))
    if confirm is None:
        return None
    # Count the radio controls, never OCR lines: a wrapped first theme can
    # produce several text rows, none of which constitutes another option.
    import cv2
    normalized = cv2.resize(frame, (900, 1600))
    gray = cv2.cvtColor(normalized[450:820, 175:245], cv2.COLOR_BGR2GRAY)
    circles = cv2.HoughCircles(gray, cv2.HOUGH_GRADIENT, 1, 45,
                              param1=80, param2=15, minRadius=10, maxRadius=24)
    if circles is None or len(circles[0]) < 2:
        return None
    rows = sorted(circles[0], key=lambda c: float(c[1]))
    if (max(float(c[0]) for c in rows) - min(float(c[0]) for c in rows) > 12
            or any(float(b[1] - a[1]) < 45 for a, b in zip(rows, rows[1:]))):
        return None
    if log is not None:
        for index, (_, row_y, _) in enumerate(rows, 1):
            log(f"[StaminaDaily] theme row {index} center_y={round((float(row_y) + 450) * frame.shape[0] / 1600)}")
    cx, cy, _ = rows[1]
    cx, cy = round(cx + 175), round(cy + 450)
    saturation = cv2.cvtColor(normalized[cy-8:cy+9, cx-8:cx+9], cv2.COLOR_BGR2HSV)[:, :, 1]
    radio = _scaled_roi((cx-8, cy-8, 16, 16), frame)
    if log is not None:
        log(f"[StaminaDaily] selected row=2, point={radio.center}")
    return radio, confirm, float(saturation.mean()) >= 35


def list_category(items, frame):
    values = _items_in(items, frame, (130, 75, 220, 65))
    for category, label in CATEGORY_LABELS.items():
        if _find(values, label):
            return category
    return None


def is_list(items, frame):
    return (list_category(items, frame) is not None
            and _find(items, "总收集", frame, (350, 70, 300, 80)) is not None)


def is_filtered_list(items, frame):
    return (is_list(items, frame) and _find(items, "查看全部", frame, (280, 150, 200, 80)) is not None
            and _find(items, "自定义主题", frame, (110, 130, 200, 110)) is None)


def visible_cards(items, frame):
    """Card number + title line are structural anchors; order is row then column."""
    values = _items_in(items, frame, (150, 220, 730, 1340))
    cards = []
    for item in values:
        text = _normalize(item.text)
        combined = re.fullmatch(r"(\d{3,5})([^\d%].+)", text)
        if item.score >= .6 and combined:
            cards.append(Card(combined[1], combined[2], item.box))
            continue
        if item.score < .6 or not re.fullmatch(r"\d{3,5}", text):
            continue
        titles = [i for i in values if i.score >= .6 and i.box.x > item.box.right
                  and i.box.x - item.box.right < frame.shape[1] * .18
                  and abs(i.box.center[1] - item.box.center[1]) < max(i.box.height, item.box.height) * .6
                  and not re.fullmatch(r"[\d%]+", _normalize(i.text))]
        if len(titles) != 1:
            return None  # Cannot safely count or select partially recognized cards.
        cards.append(Card(text, _normalize(titles[0].text), titles[0].box))
    if len({c.number for c in cards}) != len(cards):
        return None
    # Group slightly staggered OCR baselines into visual rows.
    return sorted(cards, key=lambda c: (round(c.box.center[1] / (frame.shape[0] * .04)), c.box.x))


def detail_name(items, frame):
    if not _find(items, "服装介绍", frame, (450, 150, 430, 150)):
        return None
    names = [i for i in _items_in(items, frame, (480, 270, 390, 120)) if i.score >= .6]
    return _normalize(names[0].text) if len(names) == 1 else None


def detail_action(items, frame):
    return (_find(items, "获取途径", frame, (500, 1000, 350, 300))
            if detail_name(items, frame) else None)


def detect_state(items, frame):
    # Modal surfaces precede the visible underlying wardrobe/chapter page.
    if is_stage_clear_reward(items, frame):
        return State.SETTLEMENT
    if parse_stage_detail(items, frame):
        return State.STAGE
    if is_obtain_path(items, frame):
        return State.ACQUISITION
    if _find(items, "自定义主题", frame, (250, 350, 400, 120)):
        # A visible modal with ambiguous rows/radios must block its background.
        return State.THEME if theme_dialog(items, frame) else None
    if detail_action(items, frame):
        return State.DETAIL
    if is_chapter_map(items, frame):
        return State.CHAPTER
    if is_filtered_list(items, frame):
        return list_category(items, frame)
    if is_list(items, frame) and _find(items, "自定义主题", frame, (110, 130, 200, 110)):
        return State.WARDROBE
    if find_wardrobe_entry(items, frame):
        return State.HOME
    return None


def _poll(session, selector):
    return poll_target_with_common_popups(
        session, selector, timeout=6, interval=.5,
        loading_classifier=home_random_visit_loading_classifier,
        loading_timeout_state="QJNN_StaminaDailyLoadingTimeout")


def _click(session, selector):
    page = _poll(session, selector)
    return page is not None and _business_click(session, *page[2].center, confirm=selector)


def _back_to_detail(session, frame):
    """Return from an exhausted stage, observing fresh frames after interruptions."""
    def exhausted_stage(items, current):
        if detect_common_safe_popup(current, items).matched:
            return False
        stage = parse_stage_detail(items, current)
        return stage is not None and stage.attempts == 0

    def chapter_source(items, current):
        return (not detect_common_safe_popup(current, items).matched
                and is_chapter_map(items, current))

    def return_state(items, current):
        if detect_common_safe_popup(current, items).matched:
            return None
        if exhausted_stage(items, current):
            return State.STAGE
        if is_chapter_map(items, current):
            return State.CHAPTER
        if detail_action(items, current):
            return State.DETAIL
        return None

    stage_attempts = chapter_attempts = 0
    # A popup may consume the close/back click, then reveal the same source
    # page. Re-observe all legal return states after transient handling, rather
    # than waiting only for the expected destination. Never reuse the old frame.
    for _ in range(2 * MAX_RETURN_ATTEMPTS + 1):
        page = _poll(session, return_state)
        if page is None:
            return False
        current, _, state = page
        if state is State.DETAIL:
            return True
        if state is State.STAGE:
            if chapter_attempts or stage_attempts >= MAX_RETURN_ATTEMPTS:
                return False
            stage_attempts += 1
            if not _close_confirmed_popup(session, current, exhausted_stage):
                continue  # A popup/page change can cancel the pre-click check.
        else:
            if chapter_attempts >= MAX_RETURN_ATTEMPTS:
                return False
            chapter_attempts += 1
            if not _safe_back_click(session, current, chapter_source, chapter=True):
                continue
        session.sleep(.8)
    return False


def _find_stage(session):
    # Two upward sweeps then two downward sweeps cover a bounded neighbourhood.
    for step in range(MAX_SWIPES + 1):
        page = _poll(session, lambda i, f: True if is_obtain_path(i, f) else None)
        if page is None:
            return False
        if find_obtain_stage_route(page[1], page[0]):
            return _click(session, find_obtain_stage_route)
        if step == MAX_SWIPES:
            return False
        _wait_before_click(session)
        frame = session.capture_frame()
        items = session.recognize_text(frame)
        if detect_state(items, frame) is not State.ACQUISITION:
            return False
        area = _scaled_roi((470, 680, 340, 400), frame)
        x = area.center[0]
        top, bottom = area.y + area.height // 5, area.bottom - area.height // 5
        start, end = (bottom, top) if step < 2 else (top, bottom)
        session.swipe(x, start, x, end, 500)
        session.sleep(.8)
    return False


def run_stamina_daily(session, *, max_items=DEFAULT_MAX_ITEMS, capture=None, log=print, progress=None):
    if type(max_items) is not int or not 1 <= max_items <= MAX_ITEM_LIMIT:
        raise ValueError("体力任务处理件数必须是 1–999 的整数")
    capture = capture or DiagnosticFrameStore(DATA_ROOT / "images/debug/pipeline/stamina_daily").capture
    visited = set()
    current_name = None
    draining = False
    category = None
    saved = progress.read() if progress is not None else None
    if saved is not None:
        if (not isinstance(saved, dict) or type(saved.get('draining')) is not bool
                or not isinstance(saved.get('visited'), list)
                or saved.get('current_name') is not None and not isinstance(saved['current_name'], str)):
            raise ValueError('invalid stamina continuation state')
        for pair in saved['visited']:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2 or not isinstance(pair[1], str):
                raise ValueError('invalid stamina continuation item')
            item_state = State(pair[0])
            if item_state not in CATEGORIES:
                raise ValueError('invalid stamina continuation category')
            visited.add((item_state, pair[1]))
        current_name, draining = saved.get('current_name'), saved['draining']
        category = State(saved['category']) if saved.get('category') is not None else None
        if category is not None and category not in CATEGORIES:
            raise ValueError('invalid stamina continuation category')

    def save_progress():
        if progress is not None:
            progress.write({'visited': sorted((s.value, number) for s, number in visited),
                            'current_name': current_name, 'draining': draining,
                            'category': category.value if category is not None else None})
    list_viewports = {category: set() for category in CATEGORIES}
    list_swipes = {category: 0 for category in CATEGORIES}

    def finish(frame, *, available_exhausted=False):
        if not _safe_back_click(session, frame, is_filtered_list):
            return fail("home_back_unconfirmed")
        if _poll(session, lambda i, f: True if find_wardrobe_entry(i, f) else None) is None:
            return fail("home_not_reached")
        log(f"[StaminaDaily] items completed: {len(visited)}/{max_items}")
        if available_exhausted:
            log("[StaminaDaily] all categories exhausted; returned HOME")
        return Result(True, len(visited), "available_items_exhausted" if available_exhausted else "completed")

    def fail(reason):
        capture("QJNN_StaminaDaily_" + reason, session.capture_frame())
        log(f"[StaminaDaily] SafeStop: {reason}; processed={len(visited)}")
        return Result(False, len(visited), reason)

    for _ in range(MAX_STEPS + max_items * (MAX_LIST_SWIPES + 12) + len(CATEGORIES) * (MAX_LIST_SWIPES + 3)):
        page = _poll(session, detect_state)
        if page is None:
            return fail("state_unknown")
        frame, items, state = page
        log(f"[StaminaDaily] state={state.value}; processed={len(visited)}")
        if state is State.HOME:
            if len(visited) >= max_items:
                return Result(True, len(visited), "completed")
            if not _click(session, find_wardrobe_entry):
                return fail("wardrobe_entry_changed")
            if _poll(session, lambda i, f: True if detect_state(i, f) is State.WARDROBE else None) is None:
                return fail("wardrobe_not_reached")
        elif state is State.WARDROBE:
            if not _click(session, lambda i, f: _find(i, "自定义主题", f, (110, 130, 200, 110))
                          if detect_state(i, f) is State.WARDROBE else None):
                return fail("theme_entry_changed")
            if _poll(session, theme_dialog) is None:
                return fail("theme_not_reached")
        elif state is State.THEME:
            def select_second_theme(i, f):
                value = theme_dialog(i, f, log=log)
                return value[0] if value and not value[2] else None

            for attempt in range(3):
                popup = _poll(session, theme_dialog)
                if popup is None:
                    return fail("theme_unknown")
                if popup[2][2]:
                    break
                if not _click(session, select_second_theme):
                    return fail("theme_row_changed")
                # A click is not proof of selection. Re-capture and inspect the
                # second radio specifically before allowing confirmation.
                if _poll(session, lambda i, f: True if (value := theme_dialog(i, f)) and value[2] else None) is not None:
                    break
            else:
                return fail("theme_selection_unconfirmed")
            if not _click(session, lambda i, f: value[1] if (value := theme_dialog(i, f)) and value[2] else None):
                return fail("theme_confirm_changed")
            if _poll(session, lambda i, f: True if is_filtered_list(i, f) else None) is None:
                return fail("filter_unconfirmed")
            # A fresh flow starts with hair; continuation keeps its current category.
            if category is None:
                category = State.HAIR
        elif state in CATEGORIES:
            if category is None:
                # A list entry always starts at hair. Mid-stage recovery first
                # verifies its current garment before returning to list order.
                category = State.HAIR
            cards = visible_cards(items, frame)
            if cards is None:
                return fail("cards_ambiguous")
            if draining and state is not category and not any(c.name == current_name for c in cards):
                label = CATEGORY_LABELS[category]
                if not _click(session, lambda i, f: _find(i, label, f, (0, 200, 150, 1250))
                              if is_filtered_list(i, f) else None):
                    return fail("category_changed")
                continue
            if draining:
                matched = [c for c in cards if c.name == current_name]
                if len(matched) != 1:
                    return fail("return_item_unconfirmed")
                visited.add((state, matched[0].number))
                list_viewports[state].clear()
                list_swipes[state] = 0
                draining = False
                current_name = None
                save_progress()
                log(f"[StaminaDaily] items processed: {len(visited)}/{max_items}")
            if len(visited) >= max_items:
                return finish(frame)
            if state is not category:
                label = CATEGORY_LABELS[category]
                if not _click(session, lambda i, f: _find(i, label, f, (0, 200, 150, 1250))
                              if is_filtered_list(i, f) else None):
                    return fail("category_changed")
                if _poll(session, lambda i, f: True if detect_state(i, f) is category else None) is None:
                    return fail("category_not_reached")
                continue
            candidates = [c for c in cards if (state, c.number) not in visited]
            if not candidates:
                signature = tuple((c.number, c.name) for c in cards)
                if cards and signature not in list_viewports[state]:
                    if list_swipes[state] >= MAX_LIST_SWIPES:
                        return fail("list_scroll_limit")
                    list_viewports[state].add(signature)
                    # Recheck the category before scrolling only the card grid;
                    # the left category rail and top controls are untouched.
                    _wait_before_click(session)
                    fresh_frame = session.capture_frame()
                    fresh_items = session.recognize_text(fresh_frame)
                    fresh_cards = visible_cards(fresh_items, fresh_frame)
                    if (detect_state(fresh_items, fresh_frame) is not state or fresh_cards is None
                            or tuple((c.number, c.name) for c in fresh_cards) != signature):
                        return fail("list_changed_before_scroll")
                    x, y1 = _scaled_roi((750, 1300, 1, 1), fresh_frame).center
                    _, y2 = _scaled_roi((750, 650, 1, 1), fresh_frame).center
                    session.swipe(x, y1, x, y2, 500)
                    session.sleep(.7)
                    list_swipes[state] += 1
                    continue
                index = CATEGORIES.index(state)
                if index + 1 < len(CATEGORIES):
                    category = CATEGORIES[index + 1]
                    continue
                return finish(frame, available_exhausted=True)
            chosen = candidates[0]
            def select_card(i, f):
                if detect_state(i, f) is not state:
                    return None
                fresh = visible_cards(i, f)
                matches = [c for c in (fresh or []) if c.number == chosen.number and c.name == chosen.name]
                return matches[0].box if len(matches) == 1 else None
            if not _click(session, select_card):
                return fail("card_changed")
            current_name = chosen.name
            save_progress()
            if _poll(session, lambda i, f: True if detail_name(i, f) == current_name and detail_action(i, f) else None) is None:
                return fail("detail_not_reached")
        elif state is State.DETAIL:
            name = detail_name(items, frame)
            if current_name is not None and name != current_name:
                return fail("wrong_detail")
            current_name = name
            save_progress()
            if draining:
                if not _safe_back_click(session, frame, lambda i, f: detail_name(i, f) == current_name and detail_action(i, f)):
                    return fail("detail_back_unconfirmed")
                if _poll(session, lambda i, f: True if is_filtered_list(i, f) else None) is None:
                    return fail("list_not_reached")
            elif not _click(session, detail_action):
                return fail("acquisition_entry_changed")
            elif _poll(session, lambda i, f: True if is_obtain_path(i, f) else None) is None:
                return fail("acquisition_not_reached")
        elif state is State.ACQUISITION:
            if not _find_stage(session):
                return fail("stage_route_not_found")
            if _poll(session, lambda i, f: parse_stage_detail(i, f)) is None:
                return fail("stage_not_reached")
        elif state is State.STAGE:
            result = run_stage_flow(session, exhaust_attempts=True, return_from_stage=_back_to_detail, log=log)
            if not result.success:
                return fail(result.reason)
            draining = True
            save_progress()
        elif state is State.CHAPTER:
            # A restart on the map cannot prove 0/3; return to this item and verify it.
            if not _safe_back_click(session, frame, is_chapter_map, chapter=True):
                return fail("chapter_back_unconfirmed")
            if _poll(session, detail_action) is None:
                return fail("detail_not_restored")
        elif state is State.SETTLEMENT:
            if not dismiss_stage_clear_reward(session):
                return fail("settlement_changed")
            if wait_for_stage_detail(session) is None:
                return fail("settlement_stage_unknown")
    return fail("step_limit")


class StaminaDailyAction(CustomAction):
    def __init__(self, run_state=None):
        super().__init__()
        self._run_state = run_state

    def run(self, context, argv):
        session = None
        store = DiagnosticFrameStore(DATA_ROOT / "images/debug/pipeline/stamina_daily")
        try:
            params = json.loads(getattr(argv, "custom_action_param", "") or "null")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise ValueError("体力任务设置必须为对象")
            max_items = params.get("max_items", DEFAULT_MAX_ITEMS)
            if type(max_items) is not int or not 1 <= max_items <= MAX_ITEM_LIMIT:
                raise ValueError("体力任务处理件数必须是 1–999 的整数")
            session = _make_runtime_session(context)
            def swipe(x1, y1, x2, y2, duration):
                job = session.controller.post_swipe(x1, y1, x2, y2, duration)
                if job is None:
                    raise RuntimeError("stamina swipe returned no job")
                job.wait()
                if not job.succeeded:
                    raise RuntimeError("stamina swipe failed")
            session.swipe = swipe
            from qjnn_run_state import run_record
            return run_stamina_daily(session, max_items=max_items, capture=store.capture,
                **({'progress': run_record(self._run_state, 'stamina_daily_state.json')}
                   if self._run_state is not None else {})).success
        except Exception as exc:
            if session is not None:
                try:
                    store.capture("QJNN_StaminaDaily_exception", session.capture_frame())
                except Exception:
                    pass
            print(f"[StaminaDaily] SafeStop: {exc}")
            return False
