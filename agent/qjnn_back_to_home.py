"""Lowest-priority, bounded recovery for the formal runtime (no device setup)."""
from __future__ import annotations

from app_paths import DATA_ROOT

import inspect
import json
import sys
import logging
import time
from logging.handlers import RotatingFileHandler
from uuid import uuid4
from datetime import datetime, timezone
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path

from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from qjnn_alliance_commission import Rect
from qjnn_common_popup import (
    CommonPopupKind, detect_common_safe_popup, _make_runtime_session,
)
from qjnn_home import detect_home_page

ROOT = Path(__file__).resolve().parent.parent
UNKNOWN_SECONDS = 6.0
POLL_SECONDS = 0.5
MAX_BACK_CLICKS = 6
QUEUE_RECOVERY_SECONDS = 60.0
RESTART_NODE = "QJNN_BackToHomeRestart"
BACK_ARROW_THRESHOLD = 0.90
BACK_ARROW_SCALE_FACTORS = (.85, .90, .95, 1.0, 1.05, 1.10, 1.15)
BACK_ARROW_ROI_REFERENCE_SIZE = (180, 180)
ARROW_PAIR_MIN_IOU = .85
ARROW_PAIR_MIN_SIZE_RATIO = .95
PIXEL_MEAN_LIMIT = 2.0
PIXEL_CHANGED_RATIO_LIMIT = .005
PIXEL_P99_LIMIT = 12.0
PIXEL_TILE_MEAN_LIMIT = 8.0
DIAGNOSTIC_PATH = DATA_ROOT / "images/debug/back_to_home/diagnostics.jsonl"


@lru_cache(maxsize=1)
def _diagnostic_logger():
    """Private file only: never propagate matcher details to GUI/stdout."""
    DIAGNOSTIC_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("maamiraclenikki.back_to_home.detail")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = RotatingFileHandler(DIAGNOSTIC_PATH, maxBytes=8 * 1024 * 1024,
                                 backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(handler)
    return logger


def diagnostic_event(event, **fields):
    try:
        _diagnostic_logger().debug(json.dumps({"time_utc": datetime.now(timezone.utc).isoformat(),
                                              "event": event, **fields}, ensure_ascii=False))
    except OSError:
        # A diagnostic disk failure must never weaken a protection or click gate.
        pass


def _box_values(box):
    return None if box is None else (box.x, box.y, box.width, box.height)
# Existing control appearances and their original screenshot widths. These are
# arrow assets, never UNKNOWN page names or page-specific recovery rules.
BACK_ARROW_TEMPLATES = (
    ("alliance/top_back_arrow.png", 900),
    ("alliance/commission_back_arrow.png", 900),
    ("common/chapter_back_arrow.png", 900),
    ("arena/back_button.png", 720),
    ("competition/home_back_button.png", 720),
    ("competition/judge_back_button.png", 720),
)


@lru_cache(maxsize=1)
def _load_back_templates():
    import cv2
    import numpy as np
    result = []
    for name, reference_width in BACK_ARROW_TEMPLATES:
        path = ROOT / "assets/resource/image" / name
        template = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if template is None:
            raise RuntimeError(f"back arrow template unavailable: {name}")
        result.append((name, reference_width, template))
    return tuple(result)


@dataclass
class RecoveryRuntime:
    enable_back_to_home_recovery: bool = True
    allow_back_to_home_recovery: bool = True
    entry: str = ""
    clicks: int = 0
    triggers: int = 0
    resumes: int = 0
    restarts: int = 0
    stop_event: object = None
    nodes: dict | None = None
    action_modules: set | None = None
    registered_modules: dict = field(default_factory=dict)
    disabled_nodes: set[str] = field(default_factory=set)
    queue_recovery_blocked: bool = False

    @property
    def enabled(self):
        return (self.enable_back_to_home_recovery
                and self.allow_back_to_home_recovery and bool(self.entry))

    def cancelled(self):
        return self.stop_event is not None and self.stop_event.is_set()

    def allows_node(self, node):
        return self.enabled and node not in self.disabled_nodes

    def restart(self, context, node):
        if self.restarts >= MAX_BACK_CLICKS or self.cancelled():
            return False
        self.restarts += 1
        return context.override_next(node, [RESTART_NODE])


@dataclass(frozen=True)
class ArrowMatch:
    template: str
    score: float
    box: Rect


def match_back_arrow(frame, *, diagnose=diagnostic_event, stage="arrow", trace="", return_details=False,
                     template_names=None):
    """HOME protection, then appearance-only matching inside the top-left ROI."""
    home = detect_home_page(frame)
    diagnose("home_protection", trace=trace, stage=stage, page=home.page,
             left_score=home.left_score, right_score=home.right_score)
    if home.page in {"P1", "P2", "P3"}:
        diagnose("arrow_result", trace=trace, stage=stage, matched=False,
                 reject_reason="HOME_protected")
        return None
    matches = [match for match in _back_arrow_matches(frame, diagnose=diagnose, stage=stage, trace=trace)
               if template_names is None or match[2] in template_names]
    best = max(matches, key=lambda match: match[0]) if matches else None
    diagnose("arrow_result", trace=trace, stage=stage, matched=best is not None,
             template=best[2] if best else None, score=best[0] if best else None,
             box=_box_values(best[1]) if best else None,
             reject_reason=None if best else "no_template_at_safety_threshold")
    if best is None:
        return None
    return ArrowMatch(best[2], best[0], best[1]) if return_details else best[1]


def _back_arrow_matches(frame, *, diagnose=diagnostic_event, stage="arrow", trace="",
                        scale_factors=BACK_ARROW_SCALE_FACTORS, roi_reference_size=None):
    """High-confidence control matches; no OCR or UNKNOWN page classification."""
    import cv2
    import numpy as np
    h, w = frame.shape[:2]
    # Search area only, never a click target: 180 x 180 on 900 x 1600.
    # Includes the existing 720-width circular control at +15% scale and a
    # margin. The former 150px ROI clipped those valid enlarged candidates.
    roi_width, roi_height = roi_reference_size or BACK_ARROW_ROI_REFERENCE_SIZE
    search = frame[:round(h * roi_height / 1600), :round(w * roi_width / 900)]
    diagnose("arrow_roi", trace=trace, stage=stage, frame_size=(w, h),
             roi=(0, 0, search.shape[1], search.shape[0]), threshold=BACK_ARROW_THRESHOLD)
    for name, reference_width, original in _load_back_templates():
        best = None
        for factor in scale_factors:
            scale = w / reference_width * factor
            template = cv2.resize(original, (max(1, round(original.shape[1] * scale)),
                                            max(1, round(original.shape[0] * scale))))
            th, tw = template.shape[:2]
            if search.shape[0] < th or search.shape[1] < tw:
                diagnose("template_score", trace=trace, stage=stage, template=name,
                         reference_width=reference_width, factor=factor, scale=scale,
                         template_size=(tw, th), score=None, box=None,
                         reject_reason="template_larger_than_fixed_ROI")
                continue
            _, score, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(
                search, template, cv2.TM_CCOEFF_NORMED))
            valid = bool(np.isfinite(score))
            box = Rect(x, y, tw, th)
            diagnose("template_score", trace=trace, stage=stage, template=name,
                     reference_width=reference_width, factor=factor, scale=scale,
                     template_size=(tw, th), score=float(score) if valid else None,
                     box=_box_values(box), threshold=BACK_ARROW_THRESHOLD,
                     reject_reason=None if valid and score >= BACK_ARROW_THRESHOLD
                     else "below_threshold" if valid else "nonfinite_score")
            if valid and (best is None or score > best[0]):
                best = (float(score), box, name)
        if best is not None and best[0] >= BACK_ARROW_THRESHOLD:
            yield best


def known_result(value):
    if value is None or value is False:
        return False
    state = getattr(value, "state", getattr(value, "kind", value))
    state = getattr(state, "value", state)
    return str(state).upper() not in {"UNKNOWN", "NONE"}


def module_probes(module):
    """Reuse read-only business recognizers exposed by a registered action.

    Recognizers must accept (items, frame), with no other required arguments.
    Never invoke action, checkpoint, click, or recovery functions here.
    """
    for name, fn in vars(module).items():
        if not inspect.isfunction(fn):
            continue
        if not (name.startswith("detect_") or name.startswith("find_")
                or name.startswith("is_") or name.endswith("loading_classifier")):
            continue
        params = list(inspect.signature(fn).parameters.values())
        if len(params) < 2 or [p.name for p in params[:2]] != ["items", "frame"]:
            continue
        if any(p.kind not in {p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD} for p in params[:2]):
            continue
        if any(p.default is inspect.Parameter.empty and p.kind not in {
            p.VAR_POSITIONAL, p.VAR_KEYWORD} for p in params[2:]):
            continue
        yield name, fn


class RuntimeClassifier:
    def __init__(self, runtime, context, modules=()):
        self.runtime, self.context = runtime, context
        self.probes = [probe for module in modules for probe in module_probes(module)]

    def __call__(self, frame, items):
        page = detect_home_page(frame).page
        if page in {"P1", "P2", "P3"}:
            diagnostic_event("state_protection", state="HOME", page=page)
            return "HOME"
        # Business first; exceptions are uncertainty and fail closed.
        for name, fn in self.probes:
            if "loading" not in name and known_result(fn(items, frame)):
                diagnostic_event("state_protection", state="BUSINESS", recognizer=name)
                return "BUSINESS"
        for name, node in (self.runtime.nodes or {}).items():
            recognition = node.get("recognition", "DirectHit")
            if node.get("inverse") or node.get("enabled") is False:
                continue
            if recognition == "DirectHit" or name.startswith("QJNN_BackToHome"):
                continue
            if recognition == "OCR" and not node.get("expected"):
                continue
            if node.get("custom_recognition") in {
                "qjnn_package_foreground", "qjnn_private_login_foreground"}:
                continue
            if name in {"QJNN_CommonPopupRuntime", "QJNN_CommonAchievementTitleOcr"}:
                continue
            detail = self.context.run_recognition(name, frame)
            if detail is not None and getattr(detail, "hit", False):
                diagnostic_event("state_protection", state="BUSINESS", pipeline_node=name)
                return "BUSINESS"
        if detect_common_safe_popup(frame, items).kind is not CommonPopupKind.NONE:
            diagnostic_event("state_protection", state="POPUP")
            return "POPUP"
        from qjnn_home_random_visit import home_random_visit_loading_classifier
        from qjnn_arena import find_arena_loading
        if (home_random_visit_loading_classifier(items, frame)
                or find_arena_loading(items, frame)):
            diagnostic_event("state_protection", state="LOADING", recognizer="shared_loading")
            return "LOADING"
        for name, fn in self.probes:
            if "loading" in name and known_result(fn(items, frame)):
                diagnostic_event("state_protection", state="LOADING", recognizer=name)
                return "LOADING"
        return "UNKNOWN"


class RecoveryCancelled(RuntimeError):
    pass


def return_home_after_failure(session, *, stop_event=None, can_navigate=lambda: True,
                              log=print, mail_detail_only=False):
    """Return between queue items without restarting the failed business task."""
    import qjnn_dreamweaver_commission as dream
    from qjnn_home_room_entry import home_toolbar_anchor_count
    from qjnn_home_random_visit import home_random_visit_loading_classifier
    from qjnn_arena import find_arena_loading
    from qjnn_world_tour import find_story_auto_play
    from qjnn_clothes_shop import find_theme_popup_cancel
    from qjnn_reward import claimed_mail_detail_target, _mail_detail_text
    from qjnn_decompose import find_workshop_page, workshop_back_target
    from qjnn_signin import signin_close_target
    from qjnn_competition import (find_like_buttons, parse_competition_remaining,
                                  find_competition_theme, find_competition_home)

    mail_detail_closed = False
    purchase_cancelled = False
    owned_closed = set()

    def classify(frame, items):
        if not can_navigate():
            return "FOREGROUND_UNCONFIRMED"
        # Mail modals can leave both HOME arrows/toolbars visible behind them.
        # Check the modal before HOME; incomplete evidence never permits input.
        if _mail_detail_text(items):
            return ("MAIL_CLAIMED_DETAIL" if claimed_mail_detail_target(items, frame) is not None
                    else "MAIL_DETAIL_UNCONFIRMED")
        if mail_detail_closed:
            from qjnn_reward import detect_reward_clothes_card, detect_reward_suit_showcase
            if (not items or detect_reward_clothes_card(frame).matched
                    or detect_reward_suit_showcase(frame).matched):
                return "MAIL_DETAIL_UNCONFIRMED"
        popup = detect_common_safe_popup(frame, items)
        popup_kind = popup.kind
        if popup_kind not in {CommonPopupKind.NONE, CommonPopupKind.UNKNOWN}:
            return "COMMON_POPUP" if getattr(popup, "matched", False) else "POPUP"
        if find_theme_popup_cancel(items, frame) is not None:
            return "THEME_CANCEL"
        if dream.find_purchase_cancel(items, frame) is not None:
            return "PURCHASE_CANCEL"
        texts = {"".join(item.text.split()) for item in items if item.score >= .6}
        if "取消" in texts and texts & {"确定", "确认", "购买"}:
            return "CONFIRMATION"
        if find_story_auto_play(items, frame) is not None:
            return "STORY"
        if find_workshop_page(items, frame) is not None:
            return "DECOMPOSE_WORKSHOP"
        if signin_close_target(items, frame) is not None:
            return "SIGNIN_CALENDAR"
        if home_random_visit_loading_classifier(items, frame) or find_arena_loading(items, frame):
            return "LOADING"
        # Owned competition exits do not require player names or globally still
        # outfit pixels. They still require page evidence and a stable matched
        # arrow on fresh frames below; the failed vote is never marked complete.
        competition_judge = (find_competition_theme(items, frame) is not None
                             and find_like_buttons(items, frame) is not None
                             and parse_competition_remaining(items, frame) is not None)
        competition_home = find_competition_home(items, frame) is not None
        if competition_judge or competition_home:
            from qjnn_reward import (has_common_reward_title, detect_reward_clothes_card,
                                     detect_reward_suit_showcase)
            if (popup_kind is not CommonPopupKind.NONE or has_common_reward_title(items)
                    or detect_reward_clothes_card(frame).matched or detect_reward_suit_showcase(frame).matched):
                return "COMPETITION_OVERLAY"  # Never click through an unhandled reward/modal.
            return "COMPETITION_JUDGE" if competition_judge else "COMPETITION_HOME"
        # These are owned business pages, even when the general light-card
        # detector reports UNKNOWN. Never treat an arbitrary light card as one.
        slots = dream.classify_slots(items, frame)
        if slots is not None and any(slot.state is not dream.SlotState.UNKNOWN for slot in slots):
            return "DREAMWEAVER_COMMISSION"
        if dream.is_obtain_path(items, frame):
            return "DREAMWEAVER_OBTAIN"
        if (dream.parse_shop_owned(items, frame) is not None
                and dream.find_buy_button(items, frame) is not None
                and dream._product_name(items, frame, (450, 130, 400, 150)) is not None):
            return "DREAMWEAVER_SHOP"
        if (dream.find_commission_menu_entry(items, frame) is not None
                and dream.find_float_island_entry(items, frame) is not None):
            return "DREAMWEAVER_MAIN"
        if (popup_kind is CommonPopupKind.NONE
                and detect_home_page(frame).page in {"P1", "P2", "P3"}
                and home_toolbar_anchor_count(items, frame) >= 2):
            return "HOME"
        if popup_kind is not CommonPopupKind.NONE:
            return "POPUP"
        return "UNKNOWN"

    runtime = RecoveryRuntime(entry="QJNN_QueueFailureReturnHome", stop_event=stop_event)

    def snapshot():
        if runtime.cancelled() or not can_navigate():
            raise RecoveryCancelled("queue navigation cancelled or foreground lost")
        frame = session.capture_frame()
        items = session.recognize_text(frame)
        if runtime.cancelled() or not can_navigate():
            raise RecoveryCancelled("queue navigation cancelled or foreground lost")
        return frame, items, classify(frame, items)

    def exit_target(state, frame, items):
        if state in {"COMPETITION_JUDGE", "COMPETITION_HOME"}:
            expected = ("competition/judge_back_button.png" if state == "COMPETITION_JUDGE"
                        else "competition/home_back_button.png")
            match = match_back_arrow(frame, return_details=True, template_names={expected})
            if match is None or match.template != expected:
                return None
            box = match.box
            inset = max(1, min(box.width, box.height) // 4)
            return Rect(box.x + inset, box.y + inset, box.width - 2*inset, box.height - 2*inset)
        if state == "DECOMPOSE_WORKSHOP":
            return workshop_back_target(frame)
        if state == "SIGNIN_CALENDAR":
            return signin_close_target(items, frame)
        if state == "PURCHASE_CANCEL":
            return dream.find_purchase_cancel(items, frame)
        if state == "MAIL_CLAIMED_DETAIL":
            return claimed_mail_detail_target(items, frame)
        if state == "THEME_CANCEL":
            return find_theme_popup_cancel(items, frame)
        if state in {"DREAMWEAVER_COMMISSION", "DREAMWEAVER_OBTAIN"}:
            # The existing business close area, above the modal and away from
            # submit, refresh, purchase, and currency controls.
            return dream._scaled_roi((440, 240, 20, 20), frame)
        if state in {"DREAMWEAVER_SHOP", "DREAMWEAVER_MAIN"}:
            match = match_back_arrow(frame, return_details=True)
            if match is None:
                return None
            box = match.box
            inset = max(1, min(box.width, box.height) // 4)
            if box.width <= 2 * inset or box.height <= 2 * inset:
                return None
            return Rect(box.x + inset, box.y + inset, box.width - 2 * inset, box.height - 2 * inset)
        return None

    known_exits = {"THEME_CANCEL", "DREAMWEAVER_COMMISSION", "DREAMWEAVER_OBTAIN",
                   "DREAMWEAVER_SHOP", "DREAMWEAVER_MAIN", "MAIL_CLAIMED_DETAIL", "PURCHASE_CANCEL",
                   "DECOMPOSE_WORKSHOP", "SIGNIN_CALENDAR", "COMPETITION_JUDGE", "COMPETITION_HOME"}
    if mail_detail_only:
        # Protected reward actions still forbid generic back/unknown outside
        # clicks. Only this positively identified, non-destructive exit is added.
        known_exits = {"MAIL_CLAIMED_DETAIL"}
    clock = getattr(session, "monotonic", time.monotonic)
    deadline = clock() + QUEUE_RECOVERY_SECONDS
    original_session = session

    class BudgetSession:
        """One deadline/input budget also covers nested popup/back handlers."""
        inputs = 0

        def __getattr__(self, name):
            return getattr(original_session, name)

        def check(self):
            if runtime.cancelled() or not can_navigate() or clock() >= deadline:
                raise RecoveryCancelled("queue recovery stopped, foreground lost or total timeout")

        def capture_frame(self):
            self.check()
            return original_session.capture_frame()

        def sleep(self, seconds):
            self.check()
            original_session.sleep(min(seconds, max(0., deadline - clock())))
            self.check()

        def click(self, x, y):
            self.check()
            if self.inputs >= MAX_BACK_CLICKS:
                raise RecoveryCancelled("queue recovery input budget exhausted")
            self.inputs += 1
            return original_session.click(x, y)

    session = BudgetSession()
    try:
        for _ in range(120):
            frame, items, state = snapshot()
            if state == "HOME":
                # Only two fresh HOME recognitions release the next queue item.
                session.sleep(POLL_SECONDS)
                if snapshot()[2] == "HOME":
                    return True
                continue
            if clock() >= deadline:
                break
            if state in known_exits:
                if state in owned_closed:
                    break
                if state == "MAIL_CLAIMED_DETAIL" and mail_detail_closed:
                    break
                if state == "PURCHASE_CANCEL" and purchase_cancelled:
                    break
                if runtime.clicks >= MAX_BACK_CLICKS or exit_target(state, frame, items) is None:
                    break
                dream._wait_before_click(session)
                frame, items, fresh_state = snapshot()
                if fresh_state != state:
                    continue
                target = exit_target(fresh_state, frame, items)
                if target is None:
                    break
                previous = target
                session.sleep(POLL_SECONDS)
                frame, items, stable_state = snapshot()
                target = exit_target(stable_state, frame, items) if stable_state == state else None
                if target is not None:
                    tolerance = max(1, min(previous.width, previous.height, target.width, target.height) * .05)
                    if any(abs(a-b) > tolerance for a, b in zip(_box_values(previous), _box_values(target))):
                        target = None
                if (target is None or runtime.cancelled() or not can_navigate()
                        or clock() >= deadline):
                    break
                runtime.clicks += 1
                if state in {'DECOMPOSE_WORKSHOP','SIGNIN_CALENDAR','COMPETITION_JUDGE','COMPETITION_HOME'}:
                    owned_closed.add(state)
                if state == "MAIL_CLAIMED_DETAIL":
                    mail_detail_closed = True
                if state == "PURCHASE_CANCEL":
                    purchase_cancelled = True
                log(f"[QueueFailureReturnHome] close={state} input={runtime.clicks}/{MAX_BACK_CLICKS}")
                if session.click(*target.center) is False:
                    break
                session.sleep(dream.PAGE_WAIT_SECONDS)
                continue
            if state == "COMMON_POPUP" and not mail_detail_only:
                from qjnn_common_popup import handle_transient_if_present, GlobalTransientResult
                # The existing whitelist owns geometry, sharing exclusion,
                # fresh retry checks and disappearance confirmation.
                previous = detect_common_safe_popup(frame, items)
                session.sleep(POLL_SECONDS)
                frame, items, fresh_state = snapshot()
                current = detect_common_safe_popup(frame, items)
                if (fresh_state != state or current.kind is not previous.kind
                        or current.safe_rect != previous.safe_rect):
                    continue
                before = session.inputs
                result, _, _ = handle_transient_if_present(session, frame, items, log=log)
                runtime.clicks += session.inputs - before
                log(f"[QueueFailureReturnHome] common popup result={result.value}; inputs={session.inputs}")
                if result is not GlobalTransientResult.HANDLED_POPUP:
                    break
                continue
            if state == "LOADING":
                session.sleep(POLL_SECONDS)
                continue
            if state != "UNKNOWN":
                break  # Unknown modals, purchases, stories and lost foreground stay protected.
            if mail_detail_only:
                break
            outcome = recover(session, classify, runtime, log=log)
            if outcome not in {"HOME", "RESUMED", "SKIP"}:
                # Generic recovery stops on any known layer after a back click.
                # Hand an explicitly recognized exit back to this bounded loop;
                # do not relax the generic UNKNOWN or confirmation protections.
                if snapshot()[2] not in known_exits | {"HOME", "LOADING", "COMMON_POPUP"}:
                    break
        log(f"[QueueFailureReturnHome] HOME unconfirmed state={state} inputs={runtime.clicks}; "
            "preserving failed and pending tasks")
    except RecoveryCancelled:
        log("[QueueFailureReturnHome] cancelled or foreground lost; no further input")
    return False


class QueueFailureReturnHomeAction(CustomAction):
    def __init__(self, runtime):
        super().__init__()
        self.runtime = runtime

    def run(self, context, argv):
        from qjnn_foreground import focused_package
        from adb_controller import DEFAULT_PACKAGE_NAME
        params = json.loads(argv.custom_action_param or "{}")
        package = params.get("package_name", DEFAULT_PACKAGE_NAME)
        return return_home_after_failure(
            _make_runtime_session(context), stop_event=self.runtime.stop_event,
            can_navigate=lambda: focused_package(context.tasker.controller) == package,
            mail_detail_only=bool(params.get("mail_detail_only", False)),
        )


def recover(session, classify, runtime, *, arrow=None, log=print, diagnose=diagnostic_event):
    """Observe continuously, then click once per stable, freshly matched page.

    HOME means re-enter, RESUMED means re-recognize, SKIP/STOP preserve SafeStop.
    The six-click budget belongs to the entire top-level run, not each episode.
    """
    trace = uuid4().hex[:12]
    stage = "initial"

    def detail(event, **fields):
        diagnose(event, trace=trace, stage=stage, **fields)

    def check_cancelled():
        if runtime.cancelled():
            raise RecoveryCancelled("recovery cancelled by stop_event")

    def reject(reason, **fields):
        detail("reject", reject_reason=reason, **fields)
        return "STOP"

    if not runtime.enabled or runtime.cancelled():
        return reject("disabled" if not runtime.enabled else "cancelled")

    def snapshot():
        check_cancelled()
        started = time.monotonic()
        detail("snapshot_start")
        frame = session.capture_frame()
        check_cancelled()
        items = session.recognize_text(frame)
        check_cancelled()
        detail("classify_start", frame_size=(frame.shape[1], frame.shape[0]))
        state = classify(frame, items)
        detail("classify_end", state=state, elapsed=time.monotonic() - started)
        check_cancelled()
        return frame, state

    def find_arrow(frame):
        check_cancelled()
        match = (match_back_arrow(frame, diagnose=diagnose, stage=stage, trace=trace, return_details=True)
                 if arrow is None else arrow(frame))
        # Existing injected offline session matchers return only a Rect.
        if isinstance(match, Rect):
            match = ArrowMatch("offline_injected", 1.0, match)
        detail("frame_confirmation", matched=match is not None,
               template=match.template if match else None, score=match.score if match else None,
               box=_box_values(match.box) if match else None,
               reject_reason=None if match else "arrow_absent_or_below_threshold")
        check_cancelled()
        return match

    try:
        frame, state = snapshot()
        if state != "UNKNOWN":
            detail("skip", reject_reason="protected_state", state=state)
            return "SKIP"
        started = session.monotonic()
        deadline = started + UNKNOWN_SECONDS
        next_poll = started + POLL_SECONDS
        log("[UnknownObservation] fixed 6s, fresh screenshot every 0.5s")
        stage = "UNKNOWN_observation"
        while session.monotonic() < deadline:
            session.sleep(max(0.0, min(next_poll, deadline) - session.monotonic()))
            frame, state = snapshot()
            if state != "UNKNOWN":
                return "HOME" if state == "HOME" else "RESUMED"
            next_poll += POLL_SECONDS
            # Recognition time is included in the fixed window, not added to
            # it. Skip missed ticks instead of replaying stale/queued frames.
            now = session.monotonic()
            if next_poll < now:
                next_poll = min(deadline, started + (int((now - started) / POLL_SECONDS) + 1) * POLL_SECONDS)
        runtime.triggers += 1
        log(f"[BackToHomeRecovery] trigger={runtime.triggers}")
        while True:
            stage = "frame1"
            frame, state = snapshot()
            if state == "HOME":
                return "HOME"
            if state != "UNKNOWN":
                return "RESUMED"
            if runtime.clicks >= MAX_BACK_CLICKS:
                log("[BackToHomeRecovery] six-click limit; SafeStop")
                return reject("six_click_limit")
            first_match = find_arrow(frame)
            if first_match is None:
                return reject("frame1_arrow_absent")
            # A separate fresh frame must still show the same stable page and
            # the same arrow. Disappearance/change never permits a replay.
            session.sleep(POLL_SECONDS)
            stage = "frame2"
            fresh, state = snapshot()
            if state == "HOME":
                return "HOME"
            if state != "UNKNOWN":
                return "RESUMED"
            second_match = find_arrow(fresh)
            confirmation = confirm_arrow_pair(first_match, second_match, frame, fresh)
            detail("double_frame", **confirmation)
            if not confirmation["stable"]:
                return reject(confirmation["reject_reason"])
            # Use the actual fresh-frame box, including an accepted small shift.
            box = second_match.box
            inset = max(1, min(box.width, box.height) // 4)
            if box.width <= inset * 2 or box.height <= inset * 2:
                return reject("invalid_safe_rect")
            safe = Rect(box.x + inset, box.y + inset,
                        box.width - 2 * inset, box.height - 2 * inset)
            check_cancelled()
            runtime.clicks += 1  # Count attempted submissions, including failure.
            detail("click", box=_box_values(box), safe_rect=_box_values(safe),
                   point=safe.center, back_count=runtime.clicks)
            log(f"[BackToHomeRecovery] back={runtime.clicks}/6 SafeRect={safe}")
            if session.click(*safe.center) is False:
                return reject("click_failed")
            # Wait for stable fresh frames before considering another arrow.
            previous = None
            stage = "post_click"
            for _ in range(10):
                session.sleep(POLL_SECONDS)
                frame, state = snapshot()
                if state == "HOME":
                    return "HOME"
                if state != "UNKNOWN":
                    return reject("post_click_state_contradiction", state=state)
                if find_arrow(frame) is None:
                    return reject("post_click_arrow_disappeared")
                if previous is not None and stable_frame(previous, frame):
                    break
                previous = frame
            else:
                return reject("post_click_stability_timeout")
    except RecoveryCancelled as exc:
        detail("reject", reject_reason="cancelled", error=str(exc))
        log("[BackToHomeRecovery] cancelled; SafeStop")
        return "STOP"
    except Exception as exc:
        detail("reject", reject_reason="exception", exception_type=type(exc).__name__, error=str(exc))
        log(f"[BackToHomeRecovery] uncertain state; SafeStop: {exc}")
        return "STOP"


def stable_frame(before, after):
    delta = frame_delta(before, after)
    return pixels_stable(delta)


def pixels_stable(delta):
    # Max is diagnostic only. Sparse flashes/antialiasing do not veto a pair.
    return (delta is not None and delta["mean"] <= PIXEL_MEAN_LIMIT
            and delta["changed_ratio"] <= PIXEL_CHANGED_RATIO_LIMIT
            and delta["p99"] <= PIXEL_P99_LIMIT
            and delta["max_tile_mean"] <= PIXEL_TILE_MEAN_LIMIT)


def frame_delta(before, after):
    import numpy as np
    if before.shape != after.shape:
        return None
    delta = np.abs(before.astype('float32') - after.astype('float32'))
    per_pixel = delta.max(axis=2)
    tile_means = [float(tile.mean()) for row in np.array_split(delta.mean(axis=2), 8, axis=0)
                  for tile in np.array_split(row, 8, axis=1) if tile.size]
    return {"mean": float(delta.mean()), "max": float(delta.max()),
            "p99": float(np.percentile(per_pixel, 99)),
            "changed_ratio": float((per_pixel > PIXEL_P99_LIMIT).mean()),
            "max_tile_mean": max(tile_means, default=0.)}


def confirm_arrow_pair(first, second, before, after):
    """Control evidence is primary; robust pixel summaries reject real scene changes."""
    import math
    delta = frame_delta(before, after)
    result = {"frame1": first is not None, "frame2": second is not None,
              "template1": first.template if first else None,
              "template2": second.template if second else None,
              "score1": first.score if first else None, "score2": second.score if second else None,
              "box1": _box_values(first.box) if first else None,
              "box2": _box_values(second.box) if second else None,
              "pixel_delta": delta, "stable": False, "reject_reason": None}

    def reject(reason):
        result["reject_reason"] = reason
        return result

    if first is None or second is None:
        return reject("frame2_arrow_disappeared" if second is None else "frame1_arrow_absent")
    if not all(math.isfinite(m.score) and m.score >= BACK_ARROW_THRESHOLD for m in (first, second)):
        return reject("arrow_score_below_threshold")
    family = {name for name, _ in BACK_ARROW_TEMPLATES}
    same_type = (first.template == second.template
                 or first.template in family and second.template in family)
    result["same_arrow_type"] = same_type
    if not same_type:
        return reject("arrow_type_mismatch")
    a, b = first.box, second.box
    if min(a.width, a.height, b.width, b.height) <= 0:
        return reject("invalid_arrow_box")
    intersection = max(0, min(a.right, b.right) - max(a.x, b.x)) * max(0, min(a.bottom, b.bottom) - max(a.y, b.y))
    iou = intersection / (a.width * a.height + b.width * b.height - intersection)
    dx = abs((a.x + a.width / 2) - (b.x + b.width / 2))
    dy = abs((a.y + a.height / 2) - (b.y + b.height / 2))
    width_ratio = min(a.width, b.width) / max(a.width, b.width)
    height_ratio = min(a.height, b.height) / max(a.height, b.height)
    result.update(iou=iou, center_offset=(dx, dy), width_ratio=width_ratio, height_ratio=height_ratio)
    if (iou < ARROW_PAIR_MIN_IOU or width_ratio < ARROW_PAIR_MIN_SIZE_RATIO
            or height_ratio < ARROW_PAIR_MIN_SIZE_RATIO
            or dx > max(2., min(a.width, b.width) * .05)
            or dy > max(2., min(a.height, b.height) * .05)):
        return reject("double_frame_box_mismatch")
    if not pixels_stable(delta):
        return reject("double_frame_pixels_changed")
    result["stable"] = True
    return result


def next_names(values):
    """Context.get_node_data returns normalized objects; override_next needs strings."""
    if isinstance(values, str):
        return [values]
    return [value if isinstance(value, str) else
            ("[JumpBack]" if value.get("jump_back") else "")
            + ("[Anchor]" if value.get("anchor") else "") + value["name"]
            for value in values]


class GuardedAction(CustomAction):
    """Task-specific action/recovery always runs before the shared fallback."""
    def __init__(self, delegate, runtime):
        super().__init__()
        self.delegate, self.runtime = delegate, runtime

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def run(self, context, argv):
        node = argv.node_name
        try:
            result = self.delegate.run(context, argv)
        except Exception:
            if getattr(self.delegate, "allow_back_to_home_recovery", True) is False:
                self.runtime.queue_recovery_blocked = True
            raise
        if not result and getattr(self.delegate, "allow_back_to_home_recovery", True) is False:
            self.runtime.queue_recovery_blocked = True
        if result or not self.runtime.allows_node(node):
            return result
        if getattr(self.delegate, "allow_back_to_home_recovery", True) is False:
            return False
        module = sys.modules[type(self.delegate).__module__]
        outcome = recover(_make_runtime_session(context),
                          RuntimeClassifier(self.runtime, context, [module]), self.runtime)
        if outcome not in {"HOME", "RESUMED"}:
            return False
        if outcome == "RESUMED":
            self.runtime.resumes += 1
            if self.runtime.resumes > MAX_BACK_CLICKS:
                return False
        # End only this pipeline attempt. The owning runtime submits the same
        # entry again, preserving this run's recovery budget and business caps.
        return self.runtime.restart(context, node)


class RecoveryRecognition(CustomRecognition):
    def __init__(self, runtime):
        super().__init__()
        self.runtime = runtime

    def analyze(self, context, argv):
        if not self.runtime.enabled:
            return None
        try:
            session = _make_runtime_session(context)
            modules = [sys.modules[name] for name in self.runtime.action_modules or ()]
            classify = RuntimeClassifier(self.runtime, context, modules)
            frame = session.capture_frame()
            if classify(frame, session.recognize_text(frame)) != "UNKNOWN":
                return None
            return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail="UNKNOWN")
        except Exception:
            return None


class RecoveryAction(CustomAction):
    def __init__(self, runtime):
        super().__init__()
        self.runtime = runtime

    def run(self, context, argv):
        if not self.runtime.enabled:
            return False
        params = json.loads(argv.custom_action_param)
        if not self.runtime.allows_node(params.get("source_node")):
            return False
        modules = [sys.modules[name] for name in self.runtime.action_modules or ()]
        outcome = recover(_make_runtime_session(context),
                          RuntimeClassifier(self.runtime, context, modules), self.runtime)
        if outcome == "STOP":
            return False
        if outcome == "HOME":
            return self.runtime.restart(context, argv.node_name)
        return context.override_next(argv.node_name, next_names(params["resume"]))


class RecoveryResource:
    """Registration adapter: newly registered business actions inherit the guard."""
    def __init__(self, resource, runtime):
        self.resource, self.runtime = resource, runtime

    def __getattr__(self, name):
        return getattr(self.resource, name)

    def register_custom_action(self, name, instance):
        self.runtime.registered_modules[name] = type(instance).__module__
        if not name.startswith(("qjnn_common_popup", "qjnn_back_to_home", "qjnn_save_")) and name != "qjnn_private_login_wait":
            instance = GuardedAction(instance, self.runtime)
        return self.resource.register_custom_action(name, instance)


def _reachable_task_nodes(nodes, entries):
    """Follow executable edges, including JumpBack calls, without task-name rules."""
    reachable = {}
    pending = list(entries)
    while pending:
        name = pending.pop()
        if not isinstance(name, str):
            name = name.get("name", "")
        name = name.removeprefix("[JumpBack]")
        if name in reachable or name not in nodes or name == "QJNN_SafeStop":
            continue
        node = nodes[name]
        reachable[name] = node
        for edge in ("next", "on_error"):
            values = node.get(edge, [])
            pending.extend([values] if isinstance(values, str) else values)
    return reachable


def build_runtime_override(runtime, entry, popup_override, *, nodes=None):
    """Derive coverage and inherited opt-outs from public task configuration."""
    if nodes is None:
        nodes = {}
        for path in sorted((ROOT / "assets/resource/pipeline").glob("*.json")):
            nodes.update(json.loads(path.read_text(encoding="utf-8")))
    runtime.entry = entry
    runtime.clicks = runtime.triggers = runtime.resumes = runtime.restarts = 0
    runtime.queue_recovery_blocked = False
    interface = json.loads((ROOT / "assets/interface.json").read_text(encoding="utf-8"))
    policy = next((t for t in interface["task"] if t["entry"] == entry), {})
    runtime.allow_back_to_home_recovery = policy.get("allow_back_to_home_recovery", True)
    disabled_entries = [task["entry"] for task in interface["task"]
                        if task.get("allow_back_to_home_recovery") is False]
    runtime.disabled_nodes = set(_reachable_task_nodes(nodes, disabled_entries))
    reachable = _reachable_task_nodes(nodes, [entry])
    runtime.nodes = reachable
    runtime.action_modules = {runtime.registered_modules[n["custom_action"]]
                              for n in reachable.values()
                              if n.get("custom_action") in runtime.registered_modules}
    if not runtime.enabled:
        return popup_override
    override = dict(popup_override)
    override[RESTART_NODE] = {"recognition": "DirectHit", "action": "StopTask"}
    for name, node in reachable.items():
        if (not runtime.allows_node(name) or name.startswith("QJNN_CommonPopup")
                or name == "QJNN_PrivateLoginWait"):
            continue
        next_list = override.get(name, node).get("next", [])
        if isinstance(next_list, str):
            next_list = [next_list]
        if not next_list:
            continue
        fallback = "QJNN_BackToHome_" + name
        guarded = list(next_list)
        index = next((i for i, n in enumerate(guarded)
                      if n == "QJNN_SafeStop" or isinstance(n, dict)
                      and n.get("name") == "QJNN_SafeStop"), len(guarded))
        guarded.insert(index, fallback)
        override[name] = {"next": guarded}
        override[fallback] = {
            "recognition": "Custom", "custom_recognition": "qjnn_back_to_home_unknown",
            "action": "Custom", "custom_action": "qjnn_back_to_home_recovery",
            "custom_action_param": {"resume": next_list, "source_node": name},
            "next": [entry], "on_error": ["QJNN_SafeStop"], "timeout": 30000,
        }
    return override


def run_with_recovery(tasker, entry, override, runtime, wait):
    """Restart outside the completed attempt so max_hit=1 remains unchanged."""
    attempts = 0
    while True:
        result = wait(tasker.post_task(entry, pipeline_override=override))
        if result is None or not any(n.name == RESTART_NODE and n.completed for n in result.nodes):
            return result
        attempts += 1
        if runtime is None or runtime.cancelled() or attempts > MAX_BACK_CLICKS:
            return None
        print(f"[BackToHomeRecovery] re-enter current top-level task: {entry}; "
              f"back={runtime.clicks}/6 restart={runtime.restarts}/6")
