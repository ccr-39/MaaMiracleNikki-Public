"""Optional single makeup signin. Unknown prices/screens never authorize input."""
from dataclasses import dataclass, replace
import json
import re
from pathlib import Path
import cv2
import numpy as np

from qjnn_alliance_click import alliance_business_click
from qjnn_alliance_commission import OcrItem, Rect
from qjnn_common_popup import CommonPopupKind, detect_common_safe_popup, _make_runtime_session
from qjnn_reward import detect_reward_clothes_card, detect_reward_suit_showcase
from qjnn_signin import SIGNIN_PAGE_TEXT, SIGNIN_REWARD_TITLES, SigninState, detect_signin_state, signin_close_target
from qjnn_signin_makeup_record import MAKEUP_RECORD, MakeupRecord
from signin_options import MAKEUP_TIERS, validate_makeup_tiers

try:
    from maa.custom_action import CustomAction
except ImportError:
    CustomAction = object


MIN_SCORE = .90
DIAMOND_TEMPLATE = Path(__file__).resolve().parent.parent / 'assets/resource/image/daily/makeup_diamond.png'


def _text(item):
    return "".join(str(item.text).split())


def _rect(item, frame):
    box = item.box
    height, width = frame.shape[:2]
    if (box.width < 12 or box.height < 10 or box.x < 0 or box.y < 0
            or box.right > width or box.bottom > height):
        return None
    # Inset the recognized button text; never use a full-screen fallback.
    dx, dy = max(2, box.width // 8), max(2, box.height // 8)
    return Rect(box.x + dx, box.y + dy, box.width - 2 * dx, box.height - 2 * dy)


def _unique(items, text):
    found = [item for item in items if _text(item) == text]
    return found[0] if len(found) == 1 and found[0].score >= MIN_SCORE else None


def _lines(items):
    """Join only adjacent OCR fragments on the same line, preserving confidence."""
    rows = []
    for item in sorted(items, key=lambda i: (i.box.center[1], i.box.x)):
        row = next((r for r in rows if abs(r[0].box.center[1] - item.box.center[1])
                    <= min(r[0].box.height, item.box.height) * .35), None)
        if row is None:
            rows.append([item])
        else:
            row.append(item)
    for row in rows:
        row.sort(key=lambda i: i.box.x)
        yield row


def read_quote(items):
    """Only explicit makeup totals in diamonds or explicit free text qualify.

    Standalone balances, icon-only prices, VIP rules and fuzzy OCR are refused.
    Multiple quotes, low-confidence digits or contradictory text are refused.
    """
    quotes = []
    rows=list(_lines(items))
    for index,row in enumerate(rows):
        text = "".join(_text(i) for i in row)
        if "补签" not in text or not any(word in text for word in ("免费", "钻石", "消耗", "花费", "费用")):
            continue
        if (any(i.score < MIN_SCORE for i in row)
                or any(b.box.x < a.box.right or b.box.x - a.box.right > max(a.box.height, b.box.height) * 3
                       for a, b in zip(row, row[1:]))):
            return None
        if re.fullmatch(r"(?:本次)?(?:补签(?:1次|一次)?免费|免费补签(?:1次|一次)?)[。！!]?", text):
            quotes.append(0)
            continue
        match = re.fullmatch(
            r"(?:是否|确认)?(?:本次)?补签(?:1次|一次)?(?:需要|需消耗|消耗|花费|费用[为:：]?|需)"
            r"([0-9]+)(?:个)?钻石[？?。]?", text)
        if match is None:
            match = re.fullmatch(
                r"(?:是否|确认)?(?:需要|需消耗|消耗|花费)([0-9]+)(?:个)?钻石(?:进行)?补签(?:1次|一次)?[？?。]?", text)
        if match is None:
            # Actual first-confirmation wording uses 钻, 一次 and a harmless
            # notice that can wrap onto the next OCR row. Bind only this exact
            # nearby notice; never concatenate arbitrary lines or other prices.
            question=re.fullmatch(r'要花费([0-9]+)(?:个)?钻(?:石)?补签(?:1次|一次)吗[？?](.*)',text)
            if question is not None:
                suffix=question[2]
                if suffix not in {'','（本日不再提示）','(本日不再提示)'}:
                    if suffix not in {'（','('} or index+1>=len(rows):return None
                    notice=rows[index+1]
                    notice_text=''.join(_text(i) for i in notice)
                    if (notice_text != '本日不再提示'+('）' if suffix=='（' else ')')
                            or any(i.score<MIN_SCORE for i in notice)):
                        return None
                    left=min(i.box.x for i in row);right=max(i.box.right for i in row)
                    nleft=min(i.box.x for i in notice);nright=max(i.box.right for i in notice)
                    bottom=max(i.box.bottom for i in row);top=min(i.box.y for i in notice)
                    height=max(i.box.height for i in row+notice)
                    # Adjacent OCR line boxes may overlap by a few pixels.
                    # The reported 15:18 modal has a one-pixel overlap.
                    overlap = min(i.box.height for i in row+notice) * .25
                    if not (-overlap<=top-bottom<=height and abs((left+right)-(nleft+nright))<=height*4):
                        return None
                quotes.append(int(question[1]))
                continue
        if match is None:
            return None
        quotes.append(int(match[1]))
    return quotes[0] if len(quotes) == 1 else None


def read_signed_count(items):
    counts = []
    for row in _lines(items):
        text = "".join(_text(i) for i in row)
        match = re.fullmatch(r"(?:本月)?(?:已累计签到|累计签到|已签到)([0-9]+)天", text)
        if match:
            if any(i.score < MIN_SCORE for i in row):
                return None
            counts.append(int(match[1]))
    return counts[0] if len(counts) == 1 and 0 <= counts[0] <= 31 else None


@dataclass(frozen=True)
class Surface:
    kind: str
    count: int | None = None
    diamonds: int | None = None
    target: Rect | None = None
    grid: bool = False
    claimed: tuple[Rect, ...] = ()
    balance: int | None = None
    new_calendar: bool = False


def diamond_balance(items, frame):
    """Only a top-bar number with a neighboring diamond icon is a balance."""
    h,w=frame.shape[:2]; values=[]
    for i in items:
        if i.score<MIN_SCORE or not _text(i).isdigit() or not (.75*w<i.box.center[0]<.98*w and i.box.bottom<.06*h):
            continue
        b=i.box; size=round(b.height*1.6)
        icon=frame[max(0,b.y):min(h,b.bottom),b.right:min(w,b.right+size)]
        if not icon.size:continue
        hsv=cv2.cvtColor(icon,cv2.COLOR_BGR2HSV)
        pink=((hsv[:,:,0]>=140)&(hsv[:,:,1]>=40)&(hsv[:,:,2]>=100)).astype('uint8')
        # Bind to the adjacent glyph, not the percentage of the whole OCR-height
        # window. A 30px number box includes extra header padding: the genuine
        # diamond in the 1153 capture occupies only 28.9% of that window.
        _,_,parts,_=cv2.connectedComponentsWithStats(pink)
        valid=[]
        for x,y,cw,ch,area in parts[1:]:
            density=area/(cw*ch)
            if (.6*b.height<=cw<=1.6*b.height and .6*b.height<=ch<=1.2*b.height
                    and .3<=density<=.85 and x<=b.height*.6):valid.append((x,y,cw,ch))
        if len(valid)==1:values.append(int(_text(i)))
    return values[0] if len(values)==1 else None


def _confirmation_balance(items, frame):
    """Only used after the explicit diamond-confirmation dialog is verified.

    Its blue dimming layer obscures the icon color. Re-read the unique balance
    in the same reserved header column, and bind it to the pre-click balance in
    run_signin_makeup; never use this fallback to authorize an entry click.
    """
    h,w=frame.shape[:2]
    values=[int(_text(i)) for i in items if i.score>=MIN_SCORE and _text(i).isdigit()
            and .75*w<i.box.center[0]<.98*w and i.box.bottom<.06*h]
    return values[0] if len(values)==1 else None


def _grid_price_rect(frame, button):
    b = button.box
    h, w = frame.shape[:2]
    if not (.25*w <= b.center[0] <= .98*w and .35*h <= b.center[1] <= .8*h):
        return None
    left = max(0, b.x-b.width//4)
    return Rect(left, b.bottom, min(w-left, max(1, round(b.width*1.5))),
                min(h-b.bottom, max(1, round(b.height*1.5))))


def grid_quote(items, frame, button):
    """OCR amount immediately below this button, with its actual diamond glyph."""
    h,w=frame.shape[:2]
    price = _grid_price_rect(frame, button)
    if price is None:return None
    candidates=[i for i in items if price.x<=i.box.center[0]<=price.right and price.y<=i.box.center[1]<=price.bottom]
    free=[i for i in candidates if _text(i) in {'免费','免费补签','补签免费'}]
    # The tiny diamond glyph can itself be OCR'd as a low-confidence digit.
    # Require a confident amount and independently verify the currency image.
    numbers=[i for i in candidates if i.score >= MIN_SCORE and re.fullmatch('[0-9]+',_text(i))]
    if free:
        return 0 if len(free)==1 and free[0].score>=MIN_SCORE and not numbers else None
    if len(numbers)!=1 or numbers[0].score<MIN_SCORE:return None
    template=cv2.imdecode(np.fromfile(str(DIAMOND_TEMPLATE),np.uint8),cv2.IMREAD_COLOR)
    if template is None:return None
    scale=w/720;template=cv2.resize(template,None,fx=scale,fy=scale)
    crop=frame[price.y:min(h,price.bottom),price.x:min(w,price.right)]
    if crop.shape[0]<template.shape[0] or crop.shape[1]<template.shape[1]:return None
    score=cv2.minMaxLoc(cv2.matchTemplate(crop,template,cv2.TM_CCOEFF_NORMED))[1]
    # Reward artwork shows through the translucent price strip. Match the
    # pink glyph's red/green contrast as well, so its background is not part
    # of currency identity; retain the same strict shape threshold.
    def pink_contrast(image):
        return image[:, :, 2].astype(np.float32) - image[:, :, 1].astype(np.float32)
    score=max(score,cv2.minMaxLoc(cv2.matchTemplate(pink_contrast(crop),
        pink_contrast(template),cv2.TM_CCOEFF_NORMED))[1])
    return int(_text(numbers[0])) if score>=.90 else None


def read_local_grid_quote(session, items, frame, button):
    """Recover tiny prices missed by full-screen OCR, without guessing a tier."""
    price = _grid_price_rect(frame, button)
    if price is None or price.width <= 0 or price.height <= 0:
        return None
    # Do not replace conflicting confident totals or bypass a missing currency
    # glyph. Local OCR is only for a missing/low-confidence amount.
    if any(i.score >= MIN_SCORE and re.fullmatch('[0-9]+', _text(i))
           and price.x <= i.box.center[0] <= price.right
           and price.y <= i.box.center[1] <= price.bottom for i in items):
        return None
    crop = frame[price.y:price.bottom, price.x:price.right]
    values = []
    for scale in (3, 4):
        image = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        mapped = []
        for item in session.recognize_text(image):
            box = item.box
            if (box.x < 0 or box.y < 0 or box.width <= 0 or box.height <= 0
                    or box.right > image.shape[1] or box.bottom > image.shape[0]):
                continue
            mapped.append(OcrItem(item.text, Rect(price.x + round(box.x/scale),
                price.y + round(box.y/scale), max(1, round(box.width/scale)),
                max(1, round(box.height/scale))), item.score))
        value = grid_quote(mapped, frame, button)
        if value is None:
            return None
        values.append(value)
    return values[0] if values[0] == values[1] else None


def detect_surface(items, frame, *, home=False):
    texts = [_text(i) for i in items]
    # Rewards can expose the calendar and their own Confirm control. Dismiss
    # them through the normal signin reward flow before reading a makeup quote.
    if any(any(title in t for title in SIGNIN_REWARD_TITLES) for t in texts):
        return Surface("reward")
    # Modal actions take precedence over the signin page visible behind them.
    modal_words = {"确定", "确认", "取消", "补签确认", "确认补签"}
    modal = any(t in modal_words for t in texts)
    # A real purchase confirmation has Cancel (or an explicit makeup title).
    # Its pale rectangle can also match the clothes-card geometry. Verify that
    # modal before visual rewards; a reward's lone Confirm is not a purchase.
    purchase_modal = any(t in {"取消", "补签确认", "确认补签"} for t in texts)
    if not purchase_modal and (detect_reward_clothes_card(frame).matched
                               or detect_reward_suit_showcase(frame).matched):
        return Surface("reward")
    if modal:
        if _unique(items, SIGNIN_PAGE_TEXT) is None:
            return Surface("unknown")
        confirm = [i for i in items if _text(i) in {"确定", "确认", "确认补签"}]
        cancel = _unique(items, "取消")
        # Quote must belong to the central modal, above its unique action row.
        if len(confirm) != 1 or confirm[0].score < MIN_SCORE or cancel is None:
            return Surface("unknown")
        target = _rect(confirm[0], frame)
        h, w = frame.shape[:2]
        cx, cy = confirm[0].box.center
        lx, ly = cancel.box.center
        if (target is None or not (.2*w <= lx < cx <= .9*w and .4*h <= cy <= .85*h)
                or abs(cy - ly) > max(confirm[0].box.height, cancel.box.height)):
            return Surface("unknown")
        body = [i for i in items if .15*w <= i.box.center[0] <= .85*w
                and .25*h <= i.box.center[1] < min(confirm[0].box.y, cancel.box.y)]
        quote = read_quote(body)
        claimed = tuple(i.box for i in items if _text(i) == '已领取' and i.score >= MIN_SCORE
                        and .25*w <= i.box.center[0] and .35*h <= i.box.center[1] <= .8*h)
        return (Surface("confirm", count=read_signed_count(items), diamonds=quote, target=target,
                        claimed=claimed)
                if quote is not None else Surface("unknown"))
    # The calendar itself is a large light card, reported as UNKNOWN by the
    # generic popup detector. Its owned OCR state below must take precedence.
    if detect_common_safe_popup(frame, items).kind not in {CommonPopupKind.NONE, CommonPopupKind.UNKNOWN}:
        return Surface("unknown")
    state = detect_signin_state(items)
    if state.state is SigninState.CLAIMED and state.page_item.score >= MIN_SCORE:
        if any("点击领取" in t or "确认" in t or "取消" in t or "不足" in t for t in texts):
            return Surface("unknown")
        button = _unique(items, "立即补签")
        # A low-confidence/duplicated makeup button is uncertainty, not absence.
        if button is None and any("补签" in t and "立即" in t for t in texts):
            return Surface("unknown")
        count=read_signed_count(items)
        claimed=tuple(i.box for i in items if _text(i)=='已领取' and i.score>=MIN_SCORE
                      and .25*frame.shape[1]<=i.box.center[0] and .35*frame.shape[0]<=i.box.center[1]<=.8*frame.shape[0])
        rewards=[i for i in items if i.score>=MIN_SCORE and re.fullmatch(r'[0-9]+(?:金币|钻石|体力|星光币)',_text(i))
                 and i.box.center[0]>=.25*frame.shape[1] and .35*frame.shape[0]<=i.box.center[1]<=.8*frame.shape[0]]
        grid=count is None and len(claimed)>=1 and len(rewards)>=3
        if count is None and not claimed and signin_close_target(items, frame) is not None:
            # The next monthly calendar may appear after finishing 30 days.
            # Close it normally; no prior claimed cell proves a makeup target.
            return Surface("page", new_calendar=True)
        fee=read_quote(items)
        if grid and button is not None:
            fee=grid_quote(items,frame,button)
        return Surface("page", count, fee, _rect(button, frame) if button is not None else None,
                       grid,claimed)
    if home and not any("补签" in t or "确定" in t or "确认" in t for t in texts):
        button = _unique(items, "签到")
        return Surface("home", target=_rect(button, frame) if button is not None else None)
    return Surface("unknown")


def _observe(session):
    frame = session.capture_frame()
    items = session.recognize_text(frame)
    surface = detect_surface(items, frame)
    # The owned calendar/reward/modal already identifies this frame. Running
    # the strict HOME recognizer there repeats OCR and templates on every poll.
    if surface.kind == 'unknown' and session.is_home(frame):
        surface = detect_surface(items, frame, home=True)
    if surface.kind == 'page' and surface.grid and surface.diamonds is None:
        button = _unique(items, '立即补签')
        if button is not None:
            fee = read_local_grid_quote(session, items, frame, button)
            surface = replace(surface, diamonds=fee)
    return surface, frame


def _same_surface(first, second):
    """Allow tiny OCR box jitter while keeping operation/count/fee exact."""
    if (first.kind, first.count, first.diamonds, first.grid, first.new_calendar) != (second.kind, second.count, second.diamonds, second.grid, second.new_calendar):
        return False
    a, b = first.target, second.target
    if a is None or b is None:
        return a is b
    tolerance = min(3, max(1, min(a.width, a.height, b.width, b.height) * .1))
    if any(abs(x - y) > tolerance for x, y in zip(
            (a.x, a.y, a.width, a.height), (b.x, b.y, b.width, b.height))):
        return False
    return (a.x <= b.center[0] < a.right and a.y <= b.center[1] < a.bottom
            and b.x <= a.center[0] < b.right and b.y <= a.center[1] < b.bottom)


def _click(session, expected, *, before_submit=None):
    """Reuse random delay + safe business click, then recheck semantics/geometry."""
    if expected.target is None:
        return False

    def confirm():
        for index in range(2):
            current, _ = _observe(session)
            if not _same_surface(current, expected):
                return None
            if index == 0:
                session.sleep(.5)
        if before_submit is not None:
            before_submit()
        return current.target

    return alliance_business_click(session, *expected.target.center, confirm=confirm)


def _wait_surface(session, allowed):
    # Read-only wait, never a click retry. Two settled observations are required.
    previous = None
    for _ in range(8):
        current, _ = _observe(session)
        if current.kind in allowed and previous is not None and _same_surface(current, previous):
            return current
        previous = current
        session.sleep(.5)
    return None


def _return_home(session, page):
    if page.kind != "page":
        return False
    # Reuse the existing signin close safe rectangle, scaled by the runtime.
    return session.close_signin(page)


def run_signin_makeup(session, *, enabled=False, record_path=MAKEUP_RECORD, today=None, log=print, from_calendar=False,
                      tiers=None):
    if type(enabled) is not bool:
        raise ValueError("补签开关必须为布尔值")
    if type(from_calendar) is not bool:
        raise ValueError('from_calendar must be boolean')
    if not enabled:
        return True
    selected = validate_makeup_tiers(tiers) if tiers is not None else [50]
    limit = max(selected, default=-1)
    try:
        with MakeupRecord(record_path, today=today).transaction(fresh=True) as record:
            surface = _wait_surface(session, {"page", "confirm", "reward"} if from_calendar else {"home", "page", "confirm", "reward"})
            if surface is not None and surface.kind == "reward":
                if not session.handle_signin_rewards():
                    return False
                surface = _wait_surface(session, {"page", "confirm"})
            if surface is None:
                return False
            if surface.kind == "home":
                if surface.target is None:
                    return True  # No signin entry; no input.
                if not _click(session, surface):
                    return False
                surface = _wait_surface(session, {"page", "confirm"})
            if surface is None:
                return False
            used_fees = set()
            for _ in range(31):
                if surface.target is None or limit < 0:
                    return _return_home(session, surface)
                fee = surface.diamonds
                if fee is not None and fee > limit and (tiers is not None or fee >= 100):
                    log(f"[SigninMakeup] 当前补签费用 {fee} 钻石，超过所选最高档位 {limit} 钻石；退出，不再次扣费。")
                    return _return_home(session, surface)
                if fee not in MAKEUP_TIERS or fee > limit:
                    log(f"[SigninMakeup] 无法确认允许的补签费用（识别值：{fee}），未执行补签。")
                    return False
                if (fee > 0 and fee in used_fees) or (used_fees and fee < max(used_fees)):
                    log("[SigninMakeup] 补签费用未进入下一档，结束本次补签。")
                    return _return_home(session, surface)
                after = (_resume_makeup_confirmation(session, surface, record)
                         if surface.kind == 'confirm' else _claim_makeup(session, surface, record, log))
                if after is None:
                    return False
                used_fees.add(fee)
                log(f"[SigninMakeup] 已确认补签1次（{fee} 钻石）。")
                if tiers is None:
                    return _return_home(session, after)
                # Read two fresh settled frames after each verified reward.
                # Historical records never decide eligibility for the next fee.
                surface = _wait_surface(session, {"page"})
                if surface is None:
                    return False
            return _return_home(session, surface)
    except Exception as exc:
        log(f"[SigninMakeup] SafeStop：{exc}；普通签到结果保留，下次重新检查签到页费用。")
        return False


def _grid_progress(before, after, fee, reward_confirmed):
    """Use the fresh calendar's next quote after a confirmed reward."""
    if not reward_confirmed:
        return False
    if after.new_calendar:
        return True
    if after.target is None:
        return bool(after.claimed)
    return (after.diamonds is not None and after.diamonds >= fee
            and (after.diamonds > fee or len(after.claimed) > len(before.claimed)))


def _claim_makeup(session, surface, record, log):
    """Submit one observed fee and verify its reward/progress before continuing."""
    if ((not surface.grid and (surface.count is None or surface.count >= 31))
            or surface.diamonds is None):
        log("[SigninMakeup] 无法确认可补签进度或实际费用，安全停止。")
        return None
    before = surface.count if surface.count is not None else 0
    fee = surface.diamonds
    log(f"[SigninMakeup] 已确认单次补签费用：{fee} 钻石。")
    if not _click(session, surface, before_submit=lambda: record.arm(fee, before, "entry")):
        return None
    after = _wait_surface(session, {"confirm", "reward", "page"})
    if after is None:
        return None
    if after.kind == "confirm":
        if after.diamonds != fee or after.count != surface.count:
            return None
        if not _click(session, after, before_submit=lambda: record.arm(fee, before, "confirm")):
            return None
        after = _wait_surface(session, {"reward", "page"})
        if after is None:
            return None
    reward_confirmed = after.kind == "reward"
    if reward_confirmed:
        if not session.handle_signin_rewards():
            return None
        after = _wait_surface(session, {"page"})
    if after is None or after.kind != "page":
        return None
    if surface.grid:
        target = surface.target
        def same_cell(box):
            return (abs(box.center[0] - target.center[0]) <= target.width * .6
                    and abs(box.center[1] - target.center[1]) <= target.height * 1.5)
        if (not _grid_progress(surface, after, fee, reward_confirmed)
                or (not after.new_calendar and (not any(same_cell(b) for b in after.claimed)
                    or (after.target is not None and same_cell(after.target))))):
            log(f"[SigninMakeup] 补签结果未通过校验：费用={fee}，"
                f"已领取格={len(surface.claimed)}->{len(after.claimed)}，下一档={after.diamonds}，"
                f"奖励已确认={reward_confirmed}；停止，不重复提交。")
            return None
    elif after.count != before + 1:
        return None
    record.complete()
    return after


def _resume_makeup_confirmation(session, surface, record):
    """Confirm an already-visible, selected-fee makeup dialog exactly once."""
    fee, before = surface.diamonds, surface.count
    def submitted():
        record.arm(fee, before if before is not None else 0, 'entry')
        record.arm(fee, before if before is not None else 0, 'confirm')

    if not _click(session, surface, before_submit=submitted):
        return None
    after = _wait_surface(session, {'reward', 'page'})
    if after is None:
        return None
    reward_confirmed = after.kind == 'reward'
    if reward_confirmed:
        if not session.handle_signin_rewards():
            return None
        after = _wait_surface(session, {'page'})
    if after is None or after.kind != 'page':
        return None
    if before is not None:
        if after.count != before + 1:
            return None
    else:
        if not _grid_progress(surface, after, fee, reward_confirmed):
            return None
    record.complete()
    return after


def make_runtime_session(context):
    session = _make_runtime_session(context)

    def is_home(frame):
        result = context.run_recognition("QJNN_SigninHomeToP2", frame)
        return bool(result is not None and result.hit)

    def rewards():
        # This nested context cannot re-enter the optional branch or normal claim.
        result = context.clone().run_task("QJNN_SigninRewardFlow", {
            "QJNN_SigninScreenAfterClaim": {"next": []},
            "QJNN_SigninRewardHome": {"next": []},
            "QJNN_SigninCompletedBadgeAbsent": {"next": []},
            "QJNN_SigninSkip": {"next": []},
        })
        return bool(result is not None and result.status.succeeded
                    and not any(n.name in {"QJNN_SafeStop", "QJNN_SigninFailure"} for n in result.nodes))

    def close_signin(page):
        frame = session.capture_frame()
        h, w = frame.shape[:2]
        close = Rect(round(240*w/720), round(220*h/1280), round(240*w/720), round(48*h/1280))

        def confirm():
            current, _ = _observe(session)
            return close if _same_surface(current, page) else None

        return (alliance_business_click(session, *close.center, confirm=confirm)
                and _wait_surface(session, {"home"}) is not None)

    session.is_home = is_home
    session.handle_signin_rewards = rewards
    session.close_signin = close_signin
    return session


class SigninMakeupAction(CustomAction):
    def run(self, context, argv):
        try:
            params = json.loads(argv.custom_action_param or "{}")
            if params is None:params={}
            if not isinstance(params,dict):return False
            enabled = params.get("enabled", False)
            if type(enabled) is not bool:
                return False
            if not enabled:
                return True
            options = {}
            if 'tiers' in params:
                options['tiers'] = validate_makeup_tiers(params['tiers'])
            session=make_runtime_session(context)
            return run_signin_makeup(session, enabled=True,
                                     from_calendar=params.get('from_calendar',False), **options)
        except Exception as exc:
            print(f"[SigninMakeup] SafeStop：{exc}")
            return False
