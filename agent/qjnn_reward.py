from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
import re

from maa.custom_recognition import CustomRecognition
from maa.custom_action import CustomAction


REFERENCE_SIZE = (720, 1280)


@lru_cache(maxsize=1)
def _mail_control_settings():
    nodes=json.loads((Path(__file__).resolve().parent.parent/'assets/resource/pipeline/50_mail.json').read_text(encoding='utf-8'))
    return {name:nodes[name] for name in ('QJNN_MailRewardSkip','QJNN_MailRewardConfirm')}


def has_common_reward_title(items) -> bool:
    return any(float(item.score) >= .6 and any(
        title in "".join(str(item.text).split()) for title in ("恭喜获得", "获得奖励")
    ) for item in items)


def common_reward_control(session, items, frame, kind, *, reward_visible=False):
    """Reuse existing skip/confirm OCR nodes without following mail next edges.

    The reward heading gates the underlying business UI; SafeRect is the fresh
    recognized control box, never an assumed coordinate or reward-item name.
    """
    from qjnn_alliance_commission import Rect

    nodes = {"skip": "QJNN_MailRewardSkip", "confirm": "QJNN_MailRewardConfirm"}
    if not reward_visible and not has_common_reward_title(items):
        return None
    box = session.recognize_node(nodes[kind], frame)
    if box is None:
        return None
    h, w = frame.shape[:2]
    if not (0 <= box.x < box.right <= w and h * .7 <= box.y < box.bottom <= h * .92):
        return None
    # Avoid the rim and keep every submitted click inside the recognized text.
    inset = max(1, min(box.width, box.height) // 6)
    if min(box.width, box.height) <= inset * 2:
        return None
    return Rect(box.x + inset, box.y + inset, box.width - 2 * inset, box.height - 2 * inset)


def common_reward_target(session, items, frame):
    """Prefer visible Skip/Confirm, otherwise dismiss the recognized card."""
    from qjnn_alliance_commission import Rect
    h, w = frame.shape[:2]
    heading = any(float(item.score) >= .6 and any(
        title in "".join(item.text.split()) for title in ("恭喜获得", "获得奖励")
    ) and w / 6 <= item.box.center[0] <= w * 5 / 6
       and h / 16 <= item.box.center[1] <= h * .4 for item in items)
    card = detect_reward_clothes_card(frame)
    if not heading and not card.matched:
        showcase = detect_reward_suit_showcase(frame)
        if not showcase.matched:
            return None
        x, y, width, height = REWARD_SUIT_SHOWCASE_SAFE_RECT
    else:
        x, y, width, height = REWARD_CLOTHES_SAFE_RECT
    # Clothes and suit showcases omit the common reward heading. Their shared
    # layout still authorizes the visible Skip/Confirm before a body dismissal.
    if hasattr(session, "recognize_node"):
        for kind in ("skip", "confirm"):
            control = common_reward_control(session, items, frame, kind, reward_visible=True)
            if control is not None:
                return control
    else:
        for label in ("跳过", "确定"):
            for item in items:
                box = item.box
                if (item.score >= .6 and "".join(item.text.split()) == label
                        and 0 <= box.x < box.right <= w
                        and h * .7 <= box.y < box.bottom <= h * .92):
                    return box
    return Rect(round(x * w / 720), round(y * h / 1280),
                round(width * w / 720), round(height * h / 1280))


def drain_common_rewards(session, complete, click, *, require_reward=True,
                         max_clicks=16, timeout=35.0, log=print,
                         select_target=common_reward_target, settle_retries=0):
    """Drain all current reward layers, then confirm the business page twice.

    click takes a selector and must re-read the frame before submitting input.
    A reward heading/card always blocks completion on the underlying page.
    """
    deadline = session.monotonic() + timeout
    seen = not require_reward
    stable = clicks = unsettled = 0
    for _ in range(int(timeout / .4) + 1):
        frame = session.capture_frame()
        items = session.recognize_text(frame)
        target = select_target(session, items, frame)
        if target is not None:
            seen, stable = True, 0
            if clicks >= max_clicks:
                log("[RewardFlow] reward dismiss limit reached")
                return False
            if not click(lambda fresh_items, fresh_frame: select_target(
                session, fresh_items, fresh_frame
            )):
                fresh_frame = session.capture_frame()
                fresh_items = session.recognize_text(fresh_frame)
                if select_target(session, fresh_items, fresh_frame) is not None:
                    unsettled += 1
                    if unsettled > settle_retries:
                        return False
                    log("[RewardFlow] reward changed before click; rechecking fresh frames")
                    if session.monotonic() >= deadline:
                        break
                    session.sleep(.4)
                continue
            unsettled = 0
            clicks += 1
            log(f"[RewardFlow] dismissed reward layer {clicks}/{max_clicks}")
        elif seen and complete(items, frame):
            stable += 1
            if stable >= 2:
                return True
        else:
            stable = 0
        if session.monotonic() >= deadline:
            break
        session.sleep(.4)
    return False


def _mail_detail_text(items):
    return any(float(item.score) >= .6 and "".join(str(item.text).split()) in
               {"邮件奖励已领取", "删除"} for item in items)


def claimed_mail_detail_target(items, frame):
    """Close a positively identified claimed-mail detail, never its Delete button."""
    from qjnn_alliance_commission import Rect
    if has_common_reward_title(items):
        return None
    h, w = frame.shape[:2]
    def find(label, roi):
        left, top, right, bottom = roi
        return next((item.box for item in items
                     if float(item.score) >= .8 and "".join(str(item.text).split()) == label
                     and w * left <= item.box.x < item.box.right <= w * right
                     and h * top <= item.box.y < item.box.bottom <= h * bottom), None)
    claimed = find("邮件奖励已领取", (.2, .32, .8, .6))
    delete = find("删除", (.3, .55, .7, .75))
    if claimed is None or delete is None or claimed.bottom >= delete.y:
        return None
    # Reuse QJNN_MailClose's top-center area outside the modal. In particular,
    # do not click the content, Delete, the top-left avatar or navigation arrows.
    return Rect(round(240 * w / 720), round(120 * h / 1280),
                round(240 * w / 720), round(48 * h / 1280))


class MailRewardAction(CustomAction):
    allow_back_to_home_recovery = False

    def __init__(self, run_state=None):
        super().__init__()
        self._run_state = run_state

    def run(self, context, argv):
        from qjnn_common_popup import _make_runtime_session
        from qjnn_alliance_commission import Rect
        session = _make_runtime_session(context)
        try:
            params = json.loads(getattr(argv, "custom_action_param", None) or "{}")
        except (TypeError, ValueError):
            return False
        # Maa serializes an omitted custom_action_param as the string "null".
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return False
        ready_node = params.get("ready_node", "QJNN_MailSystemDone")
        if ready_node not in {"QJNN_MailSystemDone", "QJNN_FriendMailReady", "QJNN_MailEitherTabReady"}:
            return False

        text_batch=[None,None]
        node_batch=[None,{}]
        original_text=getattr(session,'recognize_text',None)
        if callable(original_text):
            def recognize_text(frame):
                items=original_text(frame)
                text_batch[:]=[frame,items]
                # Invalidate even if a controller reuses the same ndarray.
                node_batch[:]=[frame,{}]
                return items
            session.recognize_text=recognize_text

        def recognize_node(name, frame):
            if node_batch[0] is not frame:node_batch[:]=[frame,{}]
            if name in node_batch[1]:return node_batch[1][name]
            if text_batch[0] is frame and name in {'QJNN_MailRewardSkip','QJNN_MailRewardConfirm'}:
                # This frame already went through the same OCR model. Reuse
                # the configured node's words/threshold instead of running two
                # additional full-frame OCR passes for every reward observation.
                node=_mail_control_settings()[name]
                found=[i.box for i in text_batch[1] if i.score>=node.get('threshold',.3)
                       and any(re.search(pattern,''.join(i.text.split())) for pattern in node['expected'])]
                if found:
                    value=found[0] if len(found)==1 else None
                    node_batch[1][name]=value
                    return value
                # ROI recognition may recover text omitted by full-frame OCR.
                # Keep that existing fallback; ambiguity never authorizes it.
            detail = context.run_recognition(name, frame)
            if detail is None or not detail.hit or detail.best_result is None:
                node_batch[1][name]=None
                return None
            # AndRecognitionResult has sub_results, not a box. Maa exposes
            # the aggregate target on RecognitionDetail itself.
            box = getattr(detail, "box", None)
            if box is None:
                box = getattr(detail.best_result, "box", None)
            if box is None:
                node_batch[1][name]=None
                return None
            values = (box.x, box.y, box.w, box.h) if hasattr(box, "w") else list(box)[:4]
            value=Rect(*map(int, values))
            node_batch[1][name]=value
            return value

        session.recognize_node = recognize_node
        detail_dismissed = False
        last_clicked = None

        def identity(items, frame):
            if _mail_detail_text(items):
                return ("claimed_detail",)
            # The home UI remains visible behind this modal. Its animated text
            # must not change the identity of an otherwise stable reward card.
            # Keep the reward contents and explicit controls, ignoring box jitter.
            h, w = frame.shape[:2]
            return ("reward", tuple(sorted({"".join(item.text.split()) for item in items
                    if item.score >= .8 and (
                        (w * .15 <= item.box.center[0] <= w * .85
                         and h * .3 <= item.box.center[1] <= h * .68)
                        or "".join(item.text.split()) in
                        {"恭喜获得", "获得奖励", "跳过", "确定"})})))

        def select_target(session, items, frame):
            from qjnn_home_random_visit import home_random_visit_loading_classifier
            from qjnn_common_popup import detect_common_safe_popup
            if (identity(items, frame) == last_clicked
                    or home_random_visit_loading_classifier(items, frame)
                    or detect_common_safe_popup(frame, items).matched):
                return None
            if _mail_detail_text(items) and not has_common_reward_title(items):
                # An incomplete/low-confidence detail must not fall through to
                # the visual card heuristic that caused repeated body clicks.
                return claimed_mail_detail_target(items, frame)
            # Real 14:07:11/13 frames were already the system-mail list. Its
            # pale card resembles a reward and body clicks open a single mail.
            # A positively identified list outranks visual reward heuristics.
            if (not has_common_reward_title(items)
                    and recognize_node(ready_node, frame) is not None):
                return None
            if detail_dismissed and not has_common_reward_title(items):
                return None
            return common_reward_target(session, items, frame)

        def click(selector):
            nonlocal detail_dismissed, last_clicked
            session.sleep(.8)
            first = session.capture_frame()
            first_items = session.recognize_text(first)
            previous = selector(first_items, first)
            if previous is None:
                return False
            key = identity(first_items, first)
            session.sleep(.4)
            frame = session.capture_frame()
            items = session.recognize_text(frame)
            target = selector(items, frame)
            detail = claimed_mail_detail_target(items, frame)
            if (target is None or identity(items, frame) != key
                    or (detail is not None and detail_dismissed)):
                return False
            tolerance = max(1, min(previous.width, previous.height, target.width, target.height) * .05)
            if any(abs(a-b) > tolerance for a, b in zip(asdict(previous).values(), asdict(target).values())):
                return False
            x, y = target.center
            if not (previous.x <= x < previous.right and previous.y <= y < previous.bottom):
                return False
            print(f"[MailReward] stable click kind={key[0]} SafeRect={target} point={target.center}; wait for state change")
            # A submitted input with an uncertain result must not be retried.
            # Settle retries only re-observe frames rejected before submission.
            last_clicked = key
            if session.click(*target.center) is False:
                return False
            if detail is not None:
                detail_dismissed = True
            return True

        def complete(items, frame):
            if _mail_detail_text(items) or has_common_reward_title(items):
                return False
            if recognize_node(ready_node, frame) is not None:
                return True
            # HOME behind a mail overlay is insufficient. Only accept HOME
            # after our verified detail-close click and two clean fresh frames.
            if detail_dismissed:
                from qjnn_home import detect_home_page
                return (bool(items) and not detect_reward_clothes_card(frame).matched
                        and not detect_reward_suit_showcase(frame).matched
                        and detect_home_page(frame).page in {"P1", "P2", "P3"})
            return False

        result = drain_common_rewards(
            session, complete, click, require_reward=False, select_target=select_target,
            settle_retries=3,
        )
        if result and ready_node == 'QJNN_MailEitherTabReady' and self._run_state is not None:
            record = self._run_state.record('friend_mail_state.json')
            if record.read() == 'pending':
                record.write('claimed')
        return result


REWARD_CARD_ROI = (90, 285, 540, 700)
REWARD_CARD_RECT = (103, 300, 512, 680)
REWARD_CLOTHES_SAFE_RECT = (205, 436, 308, 408)
SHARE_FORBIDDEN_RECT = (480, 960, 220, 140)
# Dark full-length clothes cover more of the pale body (reported mail card:
# 0.623). The independent bright frame and colored ribbon must still match.
REWARD_CLOTHES_THRESHOLD = 0.60
BRIGHTNESS_THRESHOLD = 240

REWARD_SUIT_SHOWCASE_ROI = (0, 250, 720, 930)
REWARD_SUIT_SHOWCASE_SAFE_RECT = (300, 700, 120, 120)
REWARD_SUIT_SHOWCASE_THRESHOLD = 0.80
SUIT_BOTTOM_CARD_PROBE = (210, 1060, 300, 105)
SUIT_RAY_PROBE = (0, 300, 720, 700)
SUIT_RAY_ORIGIN = (360, 610)
SUIT_RAY_RADIUS = (250, 570)

# The pale body/frame and colored top ribbon are shared by all reward cards.
# The description panel changes color with rarity and must not gate dismissal.
STRUCTURE_PROBES = {
    "upper_card": (121, 320, 476, 513),
    "right_frame": (590, 320, 25, 640),
}
REWARD_RIBBON_PROBE = (210, 240, 300, 45)


@dataclass(frozen=True)
class RewardClothesDetection:
    matched: bool
    score: float
    threshold: float
    probe_scores: dict[str, float]
    reward_card_rect: tuple[int, int, int, int]
    safe_rect: tuple[int, int, int, int]
    share_forbidden_rect: tuple[int, int, int, int]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def rect_contains(outer, inner) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return (
        ix >= ox
        and iy >= oy
        and ix + iw <= ox + ow
        and iy + ih <= oy + oh
    )


def rects_intersect(first, second) -> bool:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    return not (
        ax + aw <= bx
        or bx + bw <= ax
        or ay + ah <= by
        or by + bh <= ay
    )


def _normalize_frame(frame):
    import cv2

    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    target_width, target_height = REFERENCE_SIZE
    height, width = frame.shape[:2]
    if width == target_width and height == target_height:
        return frame
    if height <= width or abs(width / height - target_width / target_height) > 0.03:
        return None
    return cv2.resize(frame, REFERENCE_SIZE, interpolation=cv2.INTER_AREA)


def detect_reward_clothes_card(
    frame,
    *,
    threshold: float = REWARD_CLOTHES_THRESHOLD,
) -> RewardClothesDetection:
    """Detect the shared pale clothes-reward card without reading reward text."""
    import cv2
    import numpy as np

    normalized = _normalize_frame(frame)
    if normalized is None:
        return RewardClothesDetection(
            False,
            0.0,
            threshold,
            {},
            REWARD_CARD_RECT,
            REWARD_CLOTHES_SAFE_RECT,
            SHARE_FORBIDDEN_RECT,
        )

    gray = cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)
    probe_scores: dict[str, float] = {}
    for name, (x, y, width, height) in STRUCTURE_PROBES.items():
        probe = gray[y : y + height, x : x + width]
        probe_scores[name] = float(np.mean(probe >= BRIGHTNESS_THRESHOLD))

    hsv = cv2.cvtColor(normalized, cv2.COLOR_BGR2HSV)
    x, y, width, height = REWARD_RIBBON_PROBE
    ribbon = hsv[y:y + height, x:x + width]
    # Check colored ribbon coverage, accepting every hue and changing text.
    # A plain white list/background has no foreground ribbon at this location.
    coverage = float(np.mean((ribbon[:, :, 1] > 40) & (ribbon[:, :, 2] > 100)))
    probe_scores["top_ribbon"] = min(1.0, coverage * 2)
    score = float(min(probe_scores.values()))
    return RewardClothesDetection(
        score >= threshold,
        score,
        threshold,
        probe_scores,
        REWARD_CARD_RECT,
        REWARD_CLOTHES_SAFE_RECT,
        SHARE_FORBIDDEN_RECT,
    )


class RewardClothesRecognition(CustomRecognition):
    """Maa recognition for clothes/part reward cards; never performs input."""

    def analyze(self, context, argv: CustomRecognition.AnalyzeArg):
        try:
            param = json.loads(argv.custom_recognition_param or "{}")
            threshold = float(param.get("threshold", REWARD_CLOTHES_THRESHOLD))
        except (TypeError, ValueError, json.JSONDecodeError):
            threshold = REWARD_CLOTHES_THRESHOLD

        detection = detect_reward_clothes_card(argv.image, threshold=threshold)
        detail = detection.to_dict()
        detail["recognition_roi"] = REWARD_CARD_ROI
        detail["brightness_threshold"] = BRIGHTNESS_THRESHOLD
        detail["depends_on_new_obtained"] = False
        matched = detection.matched
        if matched:
            # Purchase confirmations share the pale card geometry. Their
            # Cancel/Confirm pair must never authorize a reward-body click.
            result = context.run_recognition("QJNN_AgentOcrBase", argv.image)
            items = list(getattr(result, "all_results", []) or [])
            if not items and getattr(result, "best_result", None) is not None:
                items = [result.best_result]
            texts = {"".join(str(i.text).split()) for i in items
                     if float(getattr(i, "score", 0.0)) >= .6}
            if "取消" in texts and texts & {"确定", "确认", "确认补签"}:
                matched = False
                detail["matched"] = False
                detail["purchase_modal_present"] = True
        return CustomRecognition.AnalyzeResult(
            box=list(REWARD_CARD_RECT) if matched else None,
            detail=detail,
        )


@dataclass(frozen=True)
class RewardSuitShowcaseDetection:
    matched: bool
    score: float
    threshold: float
    component_scores: dict[str, float]
    safe_rect: tuple[int, int, int, int]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _unit_interval(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def detect_reward_suit_showcase(
    frame,
    *,
    threshold: float = REWARD_SUIT_SHOWCASE_THRESHOLD,
) -> RewardSuitShowcaseDetection:
    """Detect the text-independent radial-ray and bottom-card showcase layout."""
    import cv2
    import numpy as np

    normalized = _normalize_frame(frame)
    if normalized is None:
        return RewardSuitShowcaseDetection(
            False,
            0.0,
            threshold,
            {},
            REWARD_SUIT_SHOWCASE_SAFE_RECT,
        )

    gray = cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)
    saturation = cv2.cvtColor(normalized, cv2.COLOR_BGR2HSV)[:, :, 1]

    card_x, card_y, card_width, card_height = SUIT_BOTTOM_CARD_PROBE
    card_gray = gray[card_y : card_y + card_height, card_x : card_x + card_width]
    card_saturation = saturation[
        card_y : card_y + card_height,
        card_x : card_x + card_width,
    ]
    bottom_card_white = float(
        np.mean((card_gray >= 210) & (card_saturation <= 65))
    )

    ray_x, ray_y, ray_width, ray_height = SUIT_RAY_PROBE
    ray_gray = gray[ray_y : ray_y + ray_height, ray_x : ray_x + ray_width]
    ray_saturation = saturation[
        ray_y : ray_y + ray_height,
        ray_x : ray_x + ray_width,
    ]
    ray_white = float(np.mean((ray_gray >= 210) & (ray_saturation <= 65)))

    yy, xx = np.indices(gray.shape)
    origin_x, origin_y = SUIT_RAY_ORIGIN
    radius = np.sqrt((xx - origin_x) ** 2 + (yy - origin_y) ** 2)
    angle = (np.arctan2(yy - origin_y, xx - origin_x) + np.pi) * 180 / np.pi
    inner_radius, outer_radius = SUIT_RAY_RADIUS
    angular_means: list[float] = []
    for start_angle in range(0, 360, 10):
        sector = (
            (radius >= inner_radius)
            & (radius <= outer_radius)
            & (angle >= start_angle)
            & (angle < start_angle + 10)
        )
        if np.any(sector):
            angular_means.append(float(np.mean(gray[sector])))
    angular_std = float(np.std(angular_means)) if angular_means else 0.0

    component_scores = {
        "bottom_progress_card": _unit_interval((bottom_card_white - 0.80) / 0.12),
        "radial_white_coverage": _unit_interval((ray_white - 0.50) / 0.20),
        "radial_angular_contrast": _unit_interval((angular_std - 20.0) / 10.0),
    }
    score = float(
        0.35 * component_scores["bottom_progress_card"]
        + 0.35 * component_scores["radial_white_coverage"]
        + 0.30 * component_scores["radial_angular_contrast"]
    )
    # Pale calendars/home backgrounds can erase the rays' angular contrast.
    # The showcase still has its beige name pill and regularly spaced progress
    # bars on a white bottom card. Read their structure, never the suit name or
    # the number of filled bars; white business pages alone cannot qualify.
    hsv = cv2.cvtColor(normalized, cv2.COLOR_BGR2HSV)
    pill_scores = []
    for x in (245, 430):
        probe = hsv[1083:1100, x:x + 35]
        pill_scores.append(float(np.mean((probe[:, :, 0] <= 18)
            & (probe[:, :, 1] >= 25) & (probe[:, :, 1] <= 100)
            & (probe[:, :, 2] >= 170))))
    bars = cv2.inRange(hsv[1125:1145, 300:370], (0, 10, 140), (18, 255, 255))
    components = cv2.connectedComponentsWithStats(bars)[2][1:]
    segments = sorted((int(x), int(y)) for x, y, w, h, area in components
                      if 4 <= w <= 12 and 10 <= h <= 18 and area >= 35 and 1 <= y <= 5)
    regular = (len(segments) >= 3
               and all(8 <= b[0] - a[0] <= 13 for a, b in zip(segments, segments[1:])))
    structured_card = (bottom_card_white >= .90 and ray_white >= .66
                       and min(pill_scores) >= .90 and regular)
    # Rays can cut the pale progress bars into disconnected pieces. Join only
    # short vertical gaps, then require a longer regular row plus both name-pill
    # ends and the white card. Never depend on the outfit name or filled count.
    fragmented_bars = cv2.inRange(hsv[1125:1145, 300:400], (0, 10, 140), (18, 255, 255))
    joined_bars = cv2.morphologyEx(fragmented_bars, cv2.MORPH_CLOSE,
                                  np.ones((5, 1), np.uint8))
    joined_components = cv2.connectedComponentsWithStats(joined_bars)[2][1:]
    joined_segments = sorted((int(x), int(y)) for x, y, w, h, area in joined_components
                             if 4 <= w <= 12 and 10 <= h <= 18 and area >= 35 and 1 <= y <= 5)
    joined_regular = (len(joined_segments) >= 6
                      and all(8 <= b[0] - a[0] <= 13
                              for a, b in zip(joined_segments, joined_segments[1:])))
    segmented_card = (bottom_card_white >= .90 and min(pill_scores) >= .90
                      and ray_white >= .55 and angular_std >= 21 and joined_regular)
    component_scores["showcase_card_structure"] = float(structured_card)
    component_scores["showcase_segmented_card"] = float(segmented_card)
    if (structured_card or segmented_card) and score < threshold:
        score = max(score, 1.0)
    return RewardSuitShowcaseDetection(
        score >= threshold,
        score,
        threshold,
        component_scores,
        REWARD_SUIT_SHOWCASE_SAFE_RECT,
    )


class RewardSuitShowcaseRecognition(CustomRecognition):
    """Maa recognition for full-character suit showcases; never performs input."""

    def analyze(self, context, argv: CustomRecognition.AnalyzeArg):
        try:
            param = json.loads(argv.custom_recognition_param or "{}")
            threshold = float(param.get("threshold", REWARD_SUIT_SHOWCASE_THRESHOLD))
        except (TypeError, ValueError, json.JSONDecodeError):
            threshold = REWARD_SUIT_SHOWCASE_THRESHOLD

        detection = detect_reward_suit_showcase(argv.image, threshold=threshold)
        detail = detection.to_dict()
        detail["recognition_roi"] = REWARD_SUIT_SHOWCASE_ROI
        detail["bottom_card_probe"] = SUIT_BOTTOM_CARD_PROBE
        detail["ray_probe"] = SUIT_RAY_PROBE
        detail["depends_on_suit_name"] = False
        detail["depends_on_progress_text"] = False
        return CustomRecognition.AnalyzeResult(
            box=list(REWARD_SUIT_SHOWCASE_ROI) if detection.matched else None,
            detail=detail,
        )
