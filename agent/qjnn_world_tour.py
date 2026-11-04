from __future__ import annotations

from app_paths import DATA_ROOT

import re
import random
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path

from qjnn_alliance_click import alliance_business_click, wait_before_alliance_click
from qjnn_alliance_commission import OcrItem, Rect
from qjnn_common_popup import (
    CommonPopupKind, _make_runtime_session, detect_common_safe_popup,
    poll_target_with_common_popups,
)
from qjnn_diagnostic import DiagnosticFrameStore
from qjnn_home import detect_home_page
from qjnn_home_random_visit import home_random_visit_loading_classifier

try:
    from maa.custom_action import CustomAction
except ImportError:
    CustomAction = object


ROOT = Path(__file__).resolve().parent.parent
MAX_STEPS = 40
MAX_RANDOM_STEPS = 24
POLL_INTERVAL = 0.5
PAGE_WAIT = 0.8
BACK_SAFE_RECT = (30, 25, 80, 80)
# Audited outside all three reference overlays; away from top controls and 留影.
OVERLAY_OUTSIDE_SAFE_RECT = (390, 1380, 120, 80)
HOME_ENTRY_ROI = (140, 390, 240, 180)
CITY_STATUS_ROI = (340, 1110, 500, 290)
GUIDE_FREE_ROI = (590, 280, 250, 140)
TOP_STATUS_ROI = (420, 150, 410, 90)
INTRO_PROMPT_ROI = (80, 550, 740, 500)
EVENT_TITLE_ROI = (300, 540, 210, 90)
EVENT_STAMP_ROI = (470, 600, 250, 140)
STORY_AUTO_PLAY_ROI = (500, 1480, 300, 120)
STORY_REVIEW_ROI = (0, 1480, 200, 120)
STORY_AUTO_PLAY_TIMEOUT = 60.0
COUNTRIES = ("苹果联邦", "云端帝国", "莉莉斯王国", "信鸽王国", "北地王国", "荒原共和国", "废墟孤岛")
MAP_CITY_ROI = (65, 330, 770, 700)


class WorldTourState(str, Enum):
    HOME = "home_p1"
    WORLD_MAP = "world_map"
    CITY_ENDED = "city_ended"
    CITY_SELECT = "city_select"
    CITY_GUIDE = "city_guide"
    CONFIRM = "confirm"
    CITY_MAIN = "city_main"
    CITY_ACTIVE = "city_active"
    SPOT_GUIDE = "spot_guide"
    INTRO_PROMPT = "intro_prompt"
    STORY = "story"
    STORY_AUTO_PLAY = "story_auto_play"
    EVENT_COLLECTED = "event_collected"
    REWARD = "reward"
    SPOT_ACTIVE = "spot_active"


@dataclass(frozen=True)
class WorldTourSnapshot:
    state: WorldTourState
    target: Rect | None = None


@dataclass(frozen=True)
class WorldTourResult:
    success: bool
    reason: str


def selected_country(items, frame):
    """Require the expanded header and matching map caption."""
    for index, name in enumerate(COUNTRIES):
        if (_find(items, frame, name, (45, 215, 800, 90))
                and _find(items, frame, name, (40, 1020, 600, 130))):
            return index
    return None


def country_tab(index, selected, frame):
    # Seven tabs: 85px each, with the selected tab expanded by 190px.
    # The measured strip is x=50..840 at the 900x1600 reference size.
    if not 0 <= index < 6 or not 0 <= selected < 7:
        raise ValueError("unavailable country tab")
    x = 50 + index * 85 + (190 if index > selected else 0)
    width = 275 if index == selected else 85
    return scaled_rect((x + 20, 240, width - 40, 45), frame)


def map_cities(items, frame):
    """City labels only; do not maintain a destination-name catalogue."""
    area = scaled_rect(MAP_CITY_ROI, frame)
    candidates = {}
    for item in sorted(items, key=lambda i: i.score, reverse=True):
        text, box = _normalize(item.text), item.box
        if (item.score >= .6 and re.fullmatch(r"[\u4e00-\u9fff]{2,10}", text)
                and text not in COUNTRIES
                and box.width > 0 and box.height > 0
                and area.x <= box.x and box.right <= area.right
                and area.y <= box.y and box.bottom <= area.bottom):
            candidates.setdefault(text, box)
    return candidates


def scaled_rect(rect, frame) -> Rect:
    h, w = frame.shape[:2]
    x, y, rw, rh = rect
    left, top = round(x * w / 900), round(y * h / 1600)
    return Rect(left, top, max(1, round((x + rw) * w / 900) - left),
                max(1, round((y + rh) * h / 1600) - top))


def _normalize(text):
    return re.sub(r"\s+", "", text).replace("：", ":")


def _find(items, frame, text, roi, *, contains=False):
    area = scaled_rect(roi, frame)
    for item in sorted(items, key=lambda i: i.score, reverse=True):
        box = item.box
        value = _normalize(item.text)
        if (box.width > 0 and box.height > 0 and item.score >= 0.6
                and area.x <= box.x and area.y <= box.y
                and box.right <= area.right and box.bottom <= area.bottom
                and (text in value if contains else text == value)):
            return box
    return None


def _story_panel(frame) -> bool:
    """Stable paper panel and red heading; no scene/name/dialogue pixels used.

    The fixed border and blank paper strips belong to the overlay UI. Unknown
    dialogue layouts fail closed rather than treating arbitrary OCR as a story.
    """
    import numpy as np

    def fraction(roi, predicate, *, border=False):
        area = scaled_rect(roi, frame)
        pixels = frame[area.y:area.bottom, area.x:area.right].astype("int16")
        if not pixels.size:
            return 0.0
        mask = predicate(pixels)
        return float(np.max(np.mean(mask, axis=1)) if border else np.mean(mask))

    border = fraction((190, 480, 570, 10), lambda p: (p[..., 0] > 200) & (p[..., 1] > 200) & (p[..., 2] > 200), border=True)
    heading = fraction((380, 515, 330, 30), lambda p: (p[..., 2] > p[..., 0] + 25) & (p[..., 2] > p[..., 1] + 10))
    paper = fraction((200, 730, 540, 30), lambda p: (p[..., 0] > 180) & (p[..., 1] > 190) & (p[..., 2] > 195))
    return border > 0.65 and heading > 0.65 and paper > 0.85


@lru_cache(maxsize=1)
def _event_stamp_template():
    import cv2
    import numpy as np

    # The original (600, 630, 105, 90) crop, without the private full screenshot.
    path = ROOT / "assets/resource/image/world_tour/event_collected_stamp.png"
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def _event_stamp_layout(frame) -> bool:
    import cv2

    normalized = cv2.resize(frame, (900, 1600))
    search = normalized[600:750, 550:730]
    score = cv2.minMaxLoc(cv2.matchTemplate(
        search, _event_stamp_template(), cv2.TM_CCOEFF_NORMED))[1]
    return score >= 0.9


def find_story_auto_play(items: list[OcrItem], frame) -> Rect | None:
    """Bottom controls only: never depend on changing story content."""
    play_items = [item for item in items
                  if re.fullmatch(r"自动播放[▶►▷▸>]*", _normalize(item.text))]
    play = _find(play_items, frame, "自动播放", STORY_AUTO_PLAY_ROI, contains=True)
    # OCR can merge the circular replay icon into the label as “回顾0”.
    # Keep the bottom ROI/confidence gates and allow only that icon suffix.
    review_items = [item for item in items
                    if re.fullmatch(r"回顾[0〇○]?", _normalize(item.text))]
    review = _find(review_items, frame, "回顾", STORY_REVIEW_ROI, contains=True)
    return play if play is not None and review is not None else None


def detect_world_tour_state(items: list[OcrItem], frame) -> WorldTourSnapshot | None:
    def find(text, roi, contains=False):
        return _find(items, frame, text, roi, contains=contains)

    # The travel-ended message is split into three OCR lines on 720px frames.
    # Match the complete message before considering the map behind its modal.
    message_area = scaled_rect((180, 650, 540, 180), frame)
    lines = sorted((i for i in items if i.score >= .8
                    and message_area.x <= i.box.x and i.box.right <= message_area.right
                    and message_area.y <= i.box.y and i.box.bottom <= message_area.bottom),
                   key=lambda i: (i.box.y, i.box.x))
    message = re.sub(r"[，,。！!]", "", "".join(_normalize(i.text) for i in lines))
    if "上一个城市的旅行已经结束" in message:
        confirm = find("确定", (330, 850, 240, 140))
        expected = "上一个城市的旅行已经结束选择心仪的城市开始下一段旅程吧"
        return (WorldTourSnapshot(WorldTourState.CITY_ENDED, confirm)
                if message == expected and confirm is not None else None)

    # Overlays MUST precede the active timer behind them.
    if find("恭喜获得", (230, 280, 460, 180)):
        return WorldTourSnapshot(WorldTourState.REWARD)
    # OCR sometimes turns the decorative exclamation into a dot. Match the
    # fixed heading with bounded punctuation, never the changing event name.
    event_titles = [i for i in items if re.fullmatch(r"新事件[!！.。．·、:：]{0,2}", _normalize(i.text))]
    if (_find(event_titles, frame, "新事件", EVENT_TITLE_ROI, contains=True)
            and (find("收藏", EVENT_STAMP_ROI) or find("收录", EVENT_STAMP_ROI)
                 or _event_stamp_layout(frame))):
        return WorldTourSnapshot(WorldTourState.EVENT_COLLECTED)
    play = find_story_auto_play(items, frame)
    if play is not None:
        return WorldTourSnapshot(WorldTourState.STORY_AUTO_PLAY, play)
    dialog = (180, 620, 540, 370)
    confirm = find("确定", dialog)
    if (confirm and find("是否要前往该城市开始旅行", dialog, True)
            and find("旅行指南", (90, 280, 570, 130), True)
            and find("前往城市", (300, 1240, 310, 160))):
        # Never resume an unrelated/payment confirmation.
        if any(re.search(r"门票|钻石|花费|消耗", item.text)
               for item in items if scaled_rect(dialog, frame).y <= item.box.center[1] <= scaled_rect(dialog, frame).bottom):
            return None
        return WorldTourSnapshot(WorldTourState.CONFIRM, confirm)

    accelerate = find("加速游览", TOP_STATUS_ROI)
    event_ui = find("偶遇事件", (200, 150, 200, 100))
    # Full-screen welcome overlay has no paper panel. Only the stable prefix
    # and tour controls authorize advancing; destination/background are random.
    intro_items = [i for i in items if _normalize(i.text).startswith("欢迎来到")]
    if accelerate and event_ui and _find(intro_items, frame, "欢迎来到", INTRO_PROMPT_ROI, contains=True):
        return WorldTourSnapshot(WorldTourState.INTRO_PROMPT)
    if accelerate and event_ui and _story_panel(frame):
        return WorldTourSnapshot(WorldTourState.STORY)

    current = find("当前位置", CITY_STATUS_ROI)
    city_ui = find("城市导览", (250, 380, 400, 110))
    if current and city_ui:
        status = "".join(_normalize(i.text) for i in items
                         if scaled_rect(CITY_STATUS_ROI, frame).y <= i.box.center[1] <= scaled_rect(CITY_STATUS_ROI, frame).bottom
                         and scaled_rect(CITY_STATUS_ROI, frame).x <= i.box.center[0] <= scaled_rect(CITY_STATUS_ROI, frame).right)
        if "游览剩余时间" in status and re.search(r"正在景点.*游览中", status):
            return WorldTourSnapshot(WorldTourState.CITY_ACTIVE)
        choose = find("选择景点", CITY_STATUS_ROI)
        if choose and "目前暂无行程" in status:
            return WorldTourSnapshot(WorldTourState.CITY_MAIN, choose)
        return None

    guide = find("旅行指南", (90, 280, 570, 130), True)
    if guide:
        free = find("免费", GUIDE_FREE_ROI)
        random = find("随便逛逛", GUIDE_FREE_ROI)
        if free and random:
            return WorldTourSnapshot(WorldTourState.SPOT_GUIDE, random)
        forward = find("前往城市", (300, 1240, 310, 160))
        if (forward and find("城市导览", (250, 380, 400, 150))
                and find("城市景点", (250, 720, 400, 120))):
            return WorldTourSnapshot(WorldTourState.CITY_GUIDE, forward)
        return None  # Paid spots and unknown free-status never provide a target.

    if accelerate and event_ui and find("游览剩余时间", TOP_STATUS_ROI, True):
        # Unrecognized central text can be a prompt covering a running timer.
        # Fail closed; a timer alone must never authorize dismissing/leaving it.
        center = scaled_rect((80, 280, 750, 980), frame)
        # The horse-field scene has a small numbered wooden sign. Its OCR
        # (e.g. 217-1-V) is scenery, not an overlay. Permit only this bounded
        # code/position with the visible photo control; other text still stops.
        photo_roi = (30, 1320, 180, 150)
        photo = (find("留影", photo_roi)
                 or (find("留", photo_roi) and find("影", photo_roi)))
        sign_roi = scaled_rect((120, 1080, 200, 130), frame)

        def scene_sign(item):
            box = item.box
            numbered_sign = (re.fullmatch(r"[A-Za-z]?[0-9]{2,4}[-－][0-9]{1,2}[-－][A-Za-z0-9]{1,3}", _normalize(item.text))
                    and sign_roi.x <= box.x and sign_roi.y <= box.y
                    and box.right <= sign_roi.right and box.bottom <= sign_roi.bottom)
            # Small low-confidence Latin fragments occur on background street
            # signs. Require the tour/photo chrome; do not whitelist arbitrary
            # central dialogue or high-confidence instructions as scenery.
            latin_fragment = (.6 <= item.score < .8
                              and re.fullmatch(r"[A-Za-z]{1,8}", _normalize(item.text))
                              and box.width <= .15*frame.shape[1]
                              and box.height <= .04*frame.shape[0])
            return bool(photo and (numbered_sign or latin_fragment))

        if any(i.score >= 0.6 and len(_normalize(i.text)) >= 2
               and center.x <= i.box.center[0] <= center.right
               and center.y <= i.box.center[1] <= center.bottom
               and not scene_sign(i) for i in items):
            return None
        return WorldTourSnapshot(WorldTourState.SPOT_ACTIVE)
    if (selected_country(items, frame) is not None
            and find("请在地图中选择", (30, 1130, 780, 140), True)):
        cities = map_cities(items, frame)
        if cities:
            return WorldTourSnapshot(WorldTourState.CITY_SELECT, next(iter(cities.values())))
    cloud = find("云端帝国", (580, 740, 300, 160), True)
    if cloud and find("北地王国", (180, 240, 360, 140), True):
        return WorldTourSnapshot(WorldTourState.WORLD_MAP, cloud)
    if detect_home_page(frame).page == "P1":
        entry = find("世界巡游", HOME_ENTRY_ROI)
        first, second = find("世界", HOME_ENTRY_ROI), find("巡游", HOME_ENTRY_ROI)
        if entry is None and first and second:
            entry = first.union(second)
        return WorldTourSnapshot(WorldTourState.HOME, entry)
    return None


def run_world_tour(session, *, capture=None, log=print,
                   loading_classifier=home_random_visit_loading_classifier,
                   story_auto_play_guard=None, choose=None) -> WorldTourResult:
    capture = capture or DiagnosticFrameStore(DATA_ROOT / "images/debug/pipeline/world_tour").capture
    choose = choose or random.choice
    last_frame = None

    def stop(reason):
        capture(f"QJNN_WorldTour_{reason}", last_frame)
        log(f"[WorldTour] SafeStop: {reason}")
        return WorldTourResult(False, reason)

    def poll(allowed=None):
        def on_timeout(frame, items):
            nonlocal last_frame
            last_frame = frame
            capture("QJNN_WorldTour_PollTimeout", frame)

        def select(items, frame):
            nonlocal last_frame
            last_frame = frame
            if loading_classifier(items, frame):
                return None
            if detect_common_safe_popup(frame, items).kind is CommonPopupKind.ACHIEVEMENT:
                return None
            state = detect_world_tour_state(list(items), frame)
            return state if state and (allowed is None or state.state in allowed) else None
        return poll_target_with_common_popups(
            session, select, timeout=2.0, interval=POLL_INTERVAL,
            loading_classifier=loading_classifier,
            loading_timeout_state="QJNN_WorldTour_LoadingTimeout",
            loading_capture=capture,
            on_timeout=on_timeout, log=log,
        )

    S = WorldTourState
    random_states = {S.INTRO_PROMPT, S.STORY, S.STORY_AUTO_PLAY, S.EVENT_COLLECTED, S.REWARD, S.SPOT_ACTIVE}
    next_states = {
        S.HOME: {S.CITY_ENDED, S.WORLD_MAP, S.CITY_SELECT, S.CITY_GUIDE, S.CONFIRM, S.CITY_MAIN, S.CITY_ACTIVE, S.SPOT_GUIDE} | random_states,
        S.CITY_ENDED: {S.WORLD_MAP, S.CITY_SELECT},
        S.WORLD_MAP: {S.CITY_SELECT}, S.CITY_SELECT: {S.CITY_GUIDE},
        S.CITY_GUIDE: {S.CONFIRM, S.CITY_MAIN, S.CITY_ACTIVE},
        S.CONFIRM: {S.CITY_MAIN, S.CITY_ACTIVE},
        S.CITY_MAIN: {S.SPOT_GUIDE}, S.SPOT_GUIDE: random_states,
        S.INTRO_PROMPT: random_states, S.STORY: random_states,
        S.EVENT_COLLECTED: random_states - {S.EVENT_COLLECTED}, S.REWARD: random_states,
        S.SPOT_ACTIVE: {S.CITY_ACTIVE}, S.CITY_ACTIVE: {S.HOME},
    }
    # A preceding daily task commonly leaves HOME P2/P3. Navigate through the
    # shared, freshly verified arrows before looking for the P1 tour entrance.
    last_frame = session.capture_frame()
    if detect_home_page(last_frame).page in {"P2", "P3"}:
        from qjnn_home_navigation import navigate_home_page
        navigation = navigate_home_page(
            "P1", session.capture_frame, session.click, sleep=session.sleep,
            detect_page=detect_home_page, log=log,
        )
        if not navigation.success:
            return stop("home_navigation_failed")
    snapshot = poll()
    returning_home = False
    random_steps = 0
    story_auto_play_clicked = False
    city_end_confirmed = False
    destination_country = None
    for _ in range(MAX_STEPS):
        if snapshot is None:
            return stop("state_unconfirmed")
        frame, items, current = snapshot
        log(f"[WorldTour] state={current.state.value}")
        if current.state is S.HOME and returning_home:
            log("[WorldTour] HOME P1 confirmed; completed")
            return WorldTourResult(True, "completed")
        if current.state is S.CITY_SELECT:
            if destination_country is None:
                destination_country = choose(tuple(range(6)))
            country = selected_country(items, frame)
            switching = country != destination_country
            city = None if switching else choose(tuple(map_cities(items, frame)))
            # The expanded tab shifts every following icon. Re-read the page
            # after the normal click delay before deriving its target.
            wait_before_alliance_click(session)
            last_frame = session.capture_frame()
            fresh_items = list(session.recognize_text(last_frame))
            fresh = detect_world_tour_state(fresh_items, last_frame)
            if (loading_classifier(fresh_items, last_frame)
                    or detect_common_safe_popup(last_frame, fresh_items).kind is not CommonPopupKind.NONE
                    or fresh is None or fresh.state is not S.CITY_SELECT
                    or selected_country(fresh_items, last_frame) != country):
                return stop("map_changed_before_click")
            if switching:
                target = country_tab(destination_country, country, last_frame)
                log(f"[WorldTour] random country={COUNTRIES[destination_country]}")
            else:
                target = map_cities(fresh_items, last_frame).get(city)
                if target is None:
                    return stop("city_label_disappeared")
                log(f"[WorldTour] OCR city={city}")
            if session.click(*target.center) is False:
                return stop("map_click_failed")
            session.sleep(PAGE_WAIT)
            snapshot = poll({S.CITY_SELECT} if switching else {S.CITY_GUIDE})
            if switching and (snapshot is None
                    or selected_country(snapshot[1], snapshot[0]) != destination_country):
                return stop("country_selection_unconfirmed")
            continue
        if current.state in random_states - {S.SPOT_ACTIVE}:
            random_steps += 1
            if random_steps > MAX_RANDOM_STEPS:
                return stop("random_loop_limit")
        if current.state is S.STORY_AUTO_PLAY:
            # Keep this protected wait inside the business action. An unknown
            # frame during/after playback must not hand control to global Back.
            if story_auto_play_guard is not None:
                story_auto_play_guard(True)
            if story_auto_play_clicked:
                return stop("story_auto_play_reentered")
            wait_before_alliance_click(session)
            last_frame = session.capture_frame()
            fresh_items = list(session.recognize_text(last_frame))
            fresh = detect_world_tour_state(fresh_items, last_frame)
            if (loading_classifier(fresh_items, last_frame)
                    or detect_common_safe_popup(last_frame, fresh_items).kind is not CommonPopupKind.NONE
                    or fresh is None or fresh.state is not S.STORY_AUTO_PLAY or fresh.target is None):
                return stop("story_auto_play_unconfirmed")
            box = fresh.target
            inset = max(1, min(box.width, box.height) // 5)
            if box.width <= 2 * inset or box.height <= 2 * inset:
                return stop("story_auto_play_target_invalid")
            safe = Rect(box.x + inset, box.y + inset, box.width - 2 * inset, box.height - 2 * inset)
            story_auto_play_clicked = True  # Fence before even an uncertain submission.
            log(f"[WorldTour] story auto-play once; SafeRect={safe}")
            if session.click(*safe.center) is False:
                return stop("story_auto_play_click_failed")
            deadline = session.monotonic() + STORY_AUTO_PLAY_TIMEOUT
            # Read-only polling: no popup dismissals, dialogue clicks or Back.
            # Playback can finish directly on the active spot without showing
            # a collectible event. Require consecutive fresh confirmations for
            # this handoff before releasing playback protection.
            active_seen = False
            for _ in range(int(STORY_AUTO_PLAY_TIMEOUT / POLL_INTERVAL) + 1):
                last_frame = session.capture_frame()
                fresh_items = list(session.recognize_text(last_frame))
                fresh = detect_world_tour_state(fresh_items, last_frame)
                safe_frame = (not loading_classifier(fresh_items, last_frame)
                        and detect_common_safe_popup(last_frame, fresh_items).kind is CommonPopupKind.NONE
                        and fresh is not None)
                active = safe_frame and fresh.state is S.SPOT_ACTIVE
                if safe_frame and (fresh.state is S.EVENT_COLLECTED or (active and active_seen)):
                    snapshot = last_frame, fresh_items, fresh
                    if active:
                        log("[WorldTour] playback finished directly on active spot; confirmed on two fresh frames")
                    if story_auto_play_guard is not None:
                        story_auto_play_guard(False)
                    break
                active_seen = active
                remaining = deadline - session.monotonic()
                if remaining <= 0:
                    return stop("story_auto_play_timeout")
                session.sleep(min(POLL_INTERVAL, remaining))
            else:
                return stop("story_auto_play_timeout")
            continue
        if current.state in {S.SPOT_ACTIVE, S.CITY_ACTIVE}:
            # Require a second fresh confirmation before leaving a stable page.
            session.sleep(POLL_INTERVAL)
            stable = poll({current.state})
            if stable is None:
                return stop("active_not_stable")
            frame, items, current = stable
            target = scaled_rect(BACK_SAFE_RECT, frame)
            returning_home = current.state is S.CITY_ACTIVE
        elif current.state in {S.INTRO_PROMPT, S.STORY, S.EVENT_COLLECTED, S.REWARD}:
            target = scaled_rect(OVERLAY_OUTSIDE_SAFE_RECT, frame)
        else:
            target = current.target
        if target is None:
            return stop("safe_target_missing")
        if current.state is S.CITY_ENDED:
            if city_end_confirmed:
                return stop("city_ended_reentered")
            wait_before_alliance_click(session)
            last_frame = session.capture_frame()
            fresh_items = list(session.recognize_text(last_frame))
            fresh = detect_world_tour_state(fresh_items, last_frame)
            if (loading_classifier(fresh_items, last_frame) or fresh is None
                    or fresh.state is not S.CITY_ENDED or fresh.target is None):
                return stop("city_ended_unconfirmed")
            box = fresh.target
            inset = max(1, min(box.width, box.height) // 5)
            safe = Rect(box.x + inset, box.y + inset, box.width - 2 * inset, box.height - 2 * inset)
            city_end_confirmed = True
            log("[WorldTour] previous city ended; confirm once, then select a new city")
            session.click(*safe.center)
        elif current.state is S.EVENT_COLLECTED:
            # Preserve the existing cadence, then verify on a fresh frame just
            # before clicking. A changed/unknown page never receives this click.
            wait_before_alliance_click(session)
            last_frame = session.capture_frame()
            fresh_items = list(session.recognize_text(last_frame))
            fresh = detect_world_tour_state(fresh_items, last_frame)
            if (loading_classifier(fresh_items, last_frame) or fresh is None
                    or fresh.state is not S.EVENT_COLLECTED):
                return stop("state_unconfirmed")
            target = scaled_rect(OVERLAY_OUTSIDE_SAFE_RECT, last_frame)
            session.click(*target.center)
        else:
            def confirmed_target():
                previous = None
                for index in range(2):
                    fresh_frame = session.capture_frame()
                    fresh_items = list(session.recognize_text(fresh_frame))
                    fresh = detect_world_tour_state(fresh_items, fresh_frame)
                    if (loading_classifier(fresh_items, fresh_frame)
                            or detect_common_safe_popup(fresh_frame, fresh_items).matched
                            or fresh is None or fresh.state is not current.state):
                        return None
                    next_target = (scaled_rect(OVERLAY_OUTSIDE_SAFE_RECT, fresh_frame)
                                   if fresh.state in {S.INTRO_PROMPT, S.STORY, S.REWARD}
                                   else scaled_rect(BACK_SAFE_RECT, fresh_frame)
                                   if fresh.state in {S.SPOT_ACTIVE, S.CITY_ACTIVE}
                                   else fresh.target)
                    if next_target is None:
                        return None
                    if previous is not None and fresh.state is S.HOME:
                        # Split/animated HOME labels change OCR box dimensions.
                        # Click only the shared interior of both observations.
                        left, top = max(previous.x, next_target.x), max(previous.y, next_target.y)
                        right, bottom = min(previous.right, next_target.right), min(previous.bottom, next_target.bottom)
                        if (right-left < .5*min(previous.width, next_target.width)
                                or bottom-top < .5*min(previous.height, next_target.height)):
                            return None
                        next_target = Rect(left, top, right-left, bottom-top)
                    elif previous is not None and any(abs(a-b) > 3 for a, b in zip(
                            (previous.x, previous.y, previous.width, previous.height),
                            (next_target.x, next_target.y, next_target.width, next_target.height))):
                        return None
                    previous = next_target
                    if index == 0:
                        session.sleep(.4)
                return next_target
            if not alliance_business_click(session, *target.center, confirm=confirmed_target):
                return stop("target_unstable_before_click")
        session.sleep(PAGE_WAIT)
        snapshot = poll(next_states[current.state])
    return stop("step_limit")


class WorldTourAction(CustomAction):
    def run(self, context, argv) -> bool:
        self.allow_back_to_home_recovery = True
        store = DiagnosticFrameStore(DATA_ROOT / "images/debug/pipeline/world_tour")
        session = _make_runtime_session(context)
        def story_guard(active):
            self.allow_back_to_home_recovery = not active
        try:
            return run_world_tour(session, capture=store.capture,
                                  story_auto_play_guard=story_guard).success
        except Exception as exc:
            print(f"[WorldTour] SafeStop: {exc}")
            try:
                store.capture("QJNN_WorldTour_Exception", session.capture_frame())
            except Exception:
                pass
            return False
