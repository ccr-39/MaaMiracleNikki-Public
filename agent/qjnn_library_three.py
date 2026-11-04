"""Left-side free organize plus two coin rounds, with a durable payment fence."""
from dataclasses import dataclass, replace
import re
import cv2
from app_paths import DATA_ROOT
from qjnn_library import (
    LibraryResult, find_library_page, find_organize_button, find_reward_popup,
    _scaled_roi, _in_roi, LIBRARY_FREE_STATUS_ROI, _retry_snapshot,
    LIBRARY_REWARD_DISMISS_POINT, LIBRARY_TOP_BACK_CLICK_POINT,
    LIBRARY_TOP_BACK_THRESHOLD, find_alliance_home, map_alliance_reference_point,
)
from qjnn_alliance_commission import Rect
from qjnn_alliance_click import alliance_business_click
from qjnn_purchase_record import PurchaseJournal
from qjnn_common_popup import detect_common_safe_popup, CommonPopupKind

RECORD = DATA_ROOT / 'state' / 'library_organize_payments.json'


def text(item):
    return ''.join(item.text.split())


def unique(items, label):
    found = [i for i in items if text(i) == label]
    return found[0] if len(found) == 1 and found[0].score >= .9 else None


def limit_notice(items, frame):
    if find_library_page(items, frame) is None:
        return False
    h, w = frame.shape[:2]
    return any(i.score >= .85 and .15*w <= i.box.center[0] <= .85*w
               and .2*h <= i.box.center[1] <= .85*h
               and re.search(r'(?:今日|今天).*(?:次数|整理).*(?:已用完|已用尽|已达上限)', text(i))
               for i in items)


def coin_number(item, frame):
    """Read a numeric amount only with an adjacent gold icon, excluding diamonds."""
    match = re.fullmatch(r'(?:花费)?([0-9]+)', text(item))
    if item.score < .9 or match is None:
        return None
    h, w = frame.shape[:2]
    b = item.box
    if not (0 <= b.x < b.right <= w and 0 <= b.y < b.bottom <= h):
        return None
    # Match the icon height to the text; a much larger window dilutes a real
    # coin with the surrounding gray price badge (notably merged 花费 8800 OCR).
    size = round(b.height * 1.2)
    for left in (max(0, b.x-size), b.right):
        icon = frame[max(0,b.y-b.height//10):min(h,b.bottom+b.height//10), left:min(w,left+size)]
        if not icon.size:
            continue
        hsv = cv2.cvtColor(icon, cv2.COLOR_BGR2HSV)
        gold = (hsv[:,:,0]>=16)&(hsv[:,:,0]<=42)&(hsv[:,:,1]>=65)&(hsv[:,:,2]>=110)
        pink = (hsv[:,:,0]>=140)&(hsv[:,:,1]>=65)
        if gold.mean() >= .35 and pink.mean() <= .1:
            return int(match[1])
    return None


def amount(items, frame, roi):
    h,w = frame.shape[:2]
    region = _scaled_roi(roi,w,h)
    values = []
    for i in items:
        if not _in_roi(i.box,region) or not re.fullmatch(r'(?:花费)?[0-9]+',text(i)):
            continue
        value=coin_number(replace(i,score=1.0),frame)
        if value is not None:
            if i.score < .9:
                return None
            values.append(value)
    return values[0] if len(values)==1 and values[0] is not None else None


@dataclass(frozen=True)
class State:
    count: int | None  # Optional game counter; most versions show only 免费完成 1/1.
    fee: int | None
    balance: int | None
    button: Rect
    disabled: bool


def state(items, frame):
    if (find_library_page(items,frame) is None or unique(items,'图书馆') is None or unique(items,'整理资料') is None
            or find_reward_popup(items,frame) is not None
            or any(text(i) in {'确定','确认','取消'} for i in items)
            or detect_common_safe_popup(frame,items).kind is not CommonPopupKind.NONE):
        return None
    counts=[]
    for i in items:
        match=re.fullmatch(r'(?:今日已整理|已整理|已完成整理资料|今日已完成)([0-3])/3',text(i))
        if match:
            if i.score < .9:
                return None
            counts.append(int(match[1]))
    button=find_organize_button(items,frame)
    if len(counts)>1 or button is None:
        return None
    if not counts and any('/3' in text(i) and '今日' in text(i) for i in items):
        return None  # An unreadable counter must not be treated as absent.
    h,w=frame.shape[:2]
    roi=_scaled_roi(LIBRARY_FREE_STATUS_ROI,w,h)
    free=[i for i in items if _in_roi(i.box,roi) and text(i) in {'免费完成','免费完成1/1'}]
    paid_fee=amount(items,frame,LIBRARY_FREE_STATUS_ROI)
    if free and paid_fee is not None:
        return None  # Conflicting free/paid evidence must never authorize input.
    count=counts[0] if counts else None
    fee=0 if len(free)==1 and free[0].score>=.9 and count in (None,0) else paid_fee
    crop=frame[button.y:button.bottom,button.x:button.right]
    if not crop.size:
        return None
    hsv=cv2.cvtColor(crop,cv2.COLOR_BGR2HSV)
    # White lettering belongs to the gray disabled button too. Excluding
    # bright pixels made the real disabled button look enabled after 3 uses.
    gray=((hsv[:,:,1]<20)&(hsv[:,:,2]>60)).mean()>.8
    return State(count,fee,amount(items,frame,(0,0,900,100)),button,bool(gray or limit_notice(items,frame)))


def observe(session):
    frame=session.capture_frame()
    return frame,session.recognize_text(frame)


def same_state(first,second):
    if first is None or second is None:return False
    return ((first.count,first.fee,first.balance,first.disabled)==(second.count,second.fee,second.balance,second.disabled)
            and all(abs(a-b)<=3 for a,b in zip((first.button.x,first.button.y,first.button.width,first.button.height),
                                              (second.button.x,second.button.y,second.button.width,second.button.height))))


def click(session, selector, before=None):
    def confirm():
        first,items=observe(session); old=selector(items,first)
        if old is None:
            return None
        session.sleep(.5)
        frame,items=observe(session); new=selector(items,frame)
        if new is None or any(abs(a-b)>3 for a,b in zip((old.x,old.y,old.width,old.height),(new.x,new.y,new.width,new.height))):
            return None
        if before is not None:
            before()
        return new
    return alliance_business_click(session,0,0,confirm=confirm)


def settled(session):
    previous=None
    for _ in range(10):
        frame,items=observe(session); current=state(items,frame)
        if same_state(current,previous):
            return current
        previous=current;session.sleep(.5)
    return None


def modal(items,frame,expected):
    if unique(items,'整理资料') is None or find_library_page(items,frame) is None:
        return None
    yes=unique(items,'确定'); no=unique(items,'取消')
    if yes is None or no is None or yes.box.x<=no.box.x:
        return None
    fee=amount(items,frame,(120,700,660,450))
    # If the game exposes a counter, it must still match. It is not required.
    count=[text(i) for i in items if re.fullmatch(r'(?:今日已整理|已整理|已完成整理资料|今日已完成)[0-3]/3',text(i)) and i.score>=.9]
    if (([int(c[-3]) for c in count] != ([] if expected.count is None else [expected.count])) or fee!=expected.fee
            or amount(items,frame,(0,0,900,100))!=expected.balance):
        return None
    h,w=frame.shape[:2]; b=yes.box
    if not (0<=b.x<b.right<=w and .5*h<=b.y<b.bottom<=.9*h and abs(b.y-no.box.y)<=b.height):
        return None
    return Rect(b.x+2,b.y+2,b.width-4,b.height-4) if min(b.width,b.height)>4 else None


def run_library_three(session, *, record_path=RECORD, log=print, capture=None):
    free_claimed=False
    completed_here=None
    def fail(reason):
        log(f'[LibraryThree] SafeStop: {reason}; no payment retry')
        if capture is not None:
            try:
                frame,_=observe(session);capture('QJNN_AllianceLibraryUncertain',frame)
            except Exception:
                pass
        return LibraryResult(False,free_claimed,reason)
    def leave(final):
        def back(items,frame):
            if not same_state(state(items,frame),final) or session.back_arrow_score(frame)<LIBRARY_TOP_BACK_THRESHOLD:
                return None
            h,w=frame.shape[:2];x,y=map_alliance_reference_point(LIBRARY_TOP_BACK_CLICK_POINT,w,h)
            return Rect(x,y,1,1)
        if not click(session,back) or _retry_snapshot(session,find_alliance_home) is None:
            return fail('library_return_failed')
        return LibraryResult(True,free_claimed,'completed')
    try:
        initial=settled(session)
        if initial is None:
            return fail('library_progress_unconfirmed')
        # Terminal game UI needs only Back, even when an older attempted input
        # left a journal fence. Preserve that fence; never replay or erase it.
        if initial.disabled or initial.count==3:
            log('图书馆：今日整理次数已用完，跳过整理资料，返回联盟首页。')
            return leave(initial)
        if callable(record_path):
            record_path=record_path()
        with PurchaseJournal(record_path).transaction() as journal:
            for _ in range(3):
                current=settled(session)
                if current is None:
                    return fail('library_progress_unconfirmed')
                # Game state owns progress. Successful history from another
                # account must never suppress this account's free/coin rounds.
                completed=current.count if current.count is not None else (completed_here if completed_here is not None else 0 if current.fee==0 else 1)
                if completed==3 or current.disabled:
                    break
                if current.fee is None or current.fee<0 or (completed>0 and current.fee==0) or (completed==0 and current.fee!=0):
                    return fail('library_price_unknown')
                if current.fee and (current.balance is None or current.balance<current.fee):
                    return fail('library_balance_insufficient_or_unknown')
                def target(items,frame):
                    value=state(items,frame)
                    return value.button if same_state(value,current) else None
                arm=(lambda: journal.arm(current.fee,f'整理资料:{completed+1}',1,completed)) if current.fee else None
                log(f'图书馆：左侧整理资料第{completed+1}/3次，' + ('免费' if current.fee==0 else f'金币{current.fee}，余额{current.balance}'))
                if not click(session,target,arm):
                    return fail('library_input_uncertain')
                # Observe after the submitted input; never resubmit the organize button.
                reward=False; confirmed=False
                for _poll in range(16):
                    frame,items=observe(session)
                    if limit_notice(items,frame):
                        exhausted=settled(session)
                        if exhausted is not None and (exhausted.disabled or exhausted.count==3):
                            log('图书馆：已确认今日次数用完提示，不再点击整理资料。')
                            return leave(exhausted)
                        return fail('library_limit_unconfirmed')
                    if find_reward_popup(items,frame) is not None:
                        reward=True;break
                    confirm=modal(items,frame,current)
                    if confirm is not None:
                        if confirmed or not click(session,lambda its,img: modal(its,img,current)):
                            return fail('library_confirmation_uncertain')
                        confirmed=True
                    session.sleep(.5)
                if not reward:
                    return fail('library_reward_unknown')
                def dismiss(items,frame):
                    if find_reward_popup(items,frame) is None:
                        return None
                    h,w=frame.shape[:2]
                    x,y=map_alliance_reference_point(LIBRARY_REWARD_DISMISS_POINT,w,h)
                    return Rect(x,y,1,1)
                if not click(session,dismiss):
                    return fail('library_reward_close_failed')
                after=settled(session)
                if (after is None or (current.count is not None and after.count!=completed+1)
                        or (current.count is None and after.count not in (None,completed+1))
                        or (not after.disabled and (after.fee==0 or (completed+1<3 and after.fee is None)))):
                    return fail('library_result_unconfirmed')
                if current.fee:
                    if after.balance!=current.balance-current.fee:
                        return fail('library_payment_unconfirmed')
                    journal.complete()
                else:
                    if current.balance is not None and after.balance!=current.balance:
                        return fail('library_free_balance_changed')
                    free_claimed=True
                completed_here=completed+1
            final=settled(session)
            if final is None or not (final.count==3 or final.disabled or completed_here==3):
                return fail('library_limit_unconfirmed')
            return leave(final)
    except Exception as exc:
        log(f'[LibraryThree] SafeStop; payment retry blocked: {exc}')
        return fail('library_record_unresolved')
