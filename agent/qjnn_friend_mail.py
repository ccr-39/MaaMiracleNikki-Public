"""Optional friend-mail batch claim; never inspect or open individual messages."""
from types import SimpleNamespace
from maa.custom_action import CustomAction
from qjnn_alliance_click import alliance_business_click
from qjnn_alliance_commission import Rect
from qjnn_common_popup import _make_runtime_session, detect_common_safe_popup, CommonPopupKind
from qjnn_reward import MailRewardAction, has_common_reward_title
from qjnn_home_navigation import HomeNavigateAction


def find(items, frame, label, *, bottom=False):
    h, w = frame.shape[:2]
    matches = [i for i in items if ''.join(i.text.split()) == label
               and (not bottom or (.15*w <= i.box.x < i.box.right <= .85*w
                                    and .6*h <= i.box.y < i.box.bottom <= .92*h))]
    if len(matches) != 1 or matches[0].score < .85:
        return None
    box = matches[0].box
    if not (0 <= box.x < box.right <= w and 0 <= box.y < box.bottom <= h):
        return None
    if bottom and not (.15*w <= box.x < box.right <= .85*w
                       and .6*h <= box.y < box.bottom <= .92*h):
        return None
    inset = max(1, min(box.width, box.height)//6)
    if min(box.width, box.height) <= 2*inset:
        return None
    return Rect(box.x+inset, box.y+inset, box.width-2*inset, box.height-2*inset)


def toast(items):
    return any('回赠' in i.text and ('成功' in i.text or '失败' in i.text) for i in items)


def snapshot(session):
    frame = session.capture_frame()
    return frame, session.recognize_text(frame)


def stable_click(session, selector):
    def confirm():
        first, items = snapshot(session)
        old = selector(items, first)
        if old is None:
            return None
        session.sleep(.4)
        fresh, items = snapshot(session)
        new = selector(items, fresh)
        if new is None:
            return None
        tolerance = min(3, max(1, min(old.width, old.height, new.width, new.height) * .1))
        return new if all(abs(a-b) <= tolerance for a,b in zip(
            (old.x,old.y,old.width,old.height), (new.x,new.y,new.width,new.height))) else None
    return alliance_business_click(session, 0, 0, confirm=confirm)


def run_friend_mail(session, *, progress=None):
    phase = progress.read() if progress is not None else None
    if phase not in (None, 'pending', 'claimed'):
        return False
    def save(value):
        nonlocal phase
        phase = value
        if progress is not None:
            progress.write(value)
    def clear(items, frame):
        popup = detect_common_safe_popup(frame, items)
        # Mail subjects such as “赛季结算奖励” overlap the generic reward
        # popup probe. Confirmed mailbox chrome owns that business content.
        mail_list = (on_mail(items, frame)
                     and find(items, frame, '系统邮件') is not None)
        return (not toast(items) and not has_common_reward_title(items)
                and (popup.kind is CommonPopupKind.NONE
                     or popup.kind is CommonPopupKind.UNKNOWN and mail_list))

    def on_mail(items, frame):
        return find(items, frame, '邮箱') is not None and find(items, frame, '好友邮件') is not None

    frame, items = snapshot(session)
    if phase == 'claimed' and session.home(frame):
        return session.home_p2()
    if phase == 'pending':
        # Resolve the previous input from positive evidence; never submit it again.
        if has_common_reward_title(items):
            if not session.rewards():
                return False
        elif not (session.ready(frame) and find(items, frame, '暂无未领取邮件')):
            return False
        save('claimed')
        frame, items = snapshot(session)
    if not on_mail(items, frame) and session.home(frame):
        # Other daily tasks can leave any HOME page. The mailbox lives on P2;
        # absence of its label on P1/P3 is not evidence that mail is empty.
        if not session.home_p2():
            print('[FriendMail] unable to confirm HOME P2; no mailbox click')
            return False
        if not stable_click(session, lambda its, img: find(its, img, '邮箱')
                            if session.home(img) and clear(its, img) else None):
            return False
        session.sleep(.8)
    for _ in range(20):
        frame, items = snapshot(session)
        if on_mail(items, frame) and clear(items, frame):
            break
        session.sleep(.4)
    else:
        return False
    if not session.ready(frame) and not stable_click(session, lambda its, img: find(its, img, '好友邮件')
                        if on_mail(its, img) and clear(its, img) else None):
        return False
    stable = 0
    for _ in range(20):
        frame, items = snapshot(session)
        stable = stable+1 if session.ready(frame) and clear(items, frame) else 0
        if stable >= 2:
            break
        session.sleep(.4)
    if stable < 2:
        return False
    empty = lambda its, img: find(its, img, '暂无未领取邮件') is not None
    if phase != 'claimed' and not empty(items, frame):
        def claim(its, img):
            target = ((find(its, img, '一键领取', bottom=True)
                       or find(its, img, '键领取', bottom=True))
                      if session.ready(img) and clear(its, img) and not empty(its, img) else None)
            if target is not None:
                save('pending')
            return target
        if not stable_click(session, claim):
            return False
        # Only positive reward/empty evidence resolves the one submitted claim.
        # A list still containing messages is permitted after reward processing.
        evidence = False
        for _ in range(30):
            frame, items = snapshot(session)
            if has_common_reward_title(items) or session.ready(frame) and empty(items, frame):
                evidence = True
                break
            session.sleep(.4)  # Includes success/failure toasts; never re-submit.
        if not evidence or not session.rewards():
            return False
    save('claimed')
    stable = 0
    for _ in range(30):
        frame, items = snapshot(session)
        stable = stable+1 if session.ready(frame) and clear(items, frame) else 0
        if stable >= 2:
            break
        session.sleep(.4)
    if stable < 2:
        return False
    def close(its, img):
        if not session.ready(img) or not clear(its, img):
            return None
        h, w = img.shape[:2]
        return Rect(round(240*w/720), round(120*h/1280), round(240*w/720), round(48*h/1280))
    if not stable_click(session, close):
        return False
    stable = 0
    for _ in range(20):
        frame, items = snapshot(session)
        stable = stable+1 if session.home(frame) and not on_mail(items, frame) and clear(items, frame) else 0
        if stable >= 2:
            return session.home_p2()
        session.sleep(.4)
    return False


class FriendMailAction(CustomAction):
    allow_back_to_home_recovery = False

    def __init__(self, run_state=None):
        super().__init__()
        self._run_state = run_state
        self.uncertain = False

    def run(self, context, argv):
        if self.uncertain and self._run_state is None:
            return False
        self.uncertain = True  # No replay after an unknown input acknowledgement.
        try:
            session = _make_runtime_session(context)
            def hit(name, frame):
                result = context.run_recognition(name, frame)
                return bool(result is not None and result.hit)
            session.home = lambda frame: hit('QJNN_FriendHomeAnyPage', frame)
            session.ready = lambda frame: hit('QJNN_FriendMailReady', frame)
            session.rewards = lambda: MailRewardAction().run(context.clone(), SimpleNamespace(
                custom_action_param='{"ready_node":"QJNN_FriendMailReady"}'))
            session.home_p2 = lambda: HomeNavigateAction().run(context, SimpleNamespace(
                custom_action_param='{"target_page":"P2"}'))
            from qjnn_run_state import run_record
            result = run_friend_mail(session, **({'progress': run_record(
                self._run_state, 'friend_mail_state.json')} if self._run_state is not None else {}))
            if result:
                self.uncertain = False
            return result
        except Exception as exc:
            print(f'[FriendMail] result uncertain; no retry: {exc}')
            return False  # QJNN_MailFailure owns diagnostics and SafeStop.
