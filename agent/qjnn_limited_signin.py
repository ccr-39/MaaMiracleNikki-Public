"""Optional free event signin, reachable only after the normal signin returns HOME."""
from dataclasses import dataclass
import json

from maa.custom_action import CustomAction
from maa.custom_recognition import CustomRecognition
from app_paths import DATA_ROOT
from qjnn_alliance_click import alliance_business_click, wait_before_alliance_click
from qjnn_alliance_commission import Rect
from qjnn_common_popup import (_make_runtime_session, _context_ocr_items,
                               detect_common_safe_popup, CommonPopupKind)
from qjnn_diagnostic import DiagnosticFrameStore
from qjnn_reward import detect_reward_clothes_card, detect_reward_suit_showcase
from qjnn_signin import SIGNIN_REWARD_TITLES

MAX_CLAIMS = 10
MAX_POLLS = 12
TIMEOUT = 90
MIN_SCORE = .9


def _text(item):
    return ''.join(item.text.split())


def _target(item, frame):
    h, w = frame.shape[:2]
    b = item.box
    if b.x < 0 or b.y < 0 or b.right > w or b.bottom > h or b.width < 12 or b.height < 10:
        return None
    dx, dy = max(2, b.width // 8), max(2, b.height // 8)
    return Rect(b.x + dx, b.y + dy, b.width - 2 * dx, b.height - 2 * dy)


def _same_target(a, b):
    if a is None or b is None:
        return a is b
    return all(abs(x - y) <= 3 for x, y in zip(
        (a.x, a.y, a.width, a.height), (b.x, b.y, b.width, b.height)))


def find_entry(items, frame):
    """Exact free entry beside the normal calendar; never fuzzy-match a gift pack."""
    h, w = frame.shape[:2]
    anchors = [i for i in items if i.score >= MIN_SCORE and _text(i) == '签到']
    entries = [i for i in items if i.score >= MIN_SCORE and _text(i) == '限时签到']
    if len(anchors) != 1 or len(entries) != 1:
        return None
    a, e = anchors[0].box, entries[0]
    x, y = e.box.center
    if not (x < .45*w and .1*h < y < .65*h
            and abs(x-a.center[0]) < .22*w and abs(y-a.center[1]) < .22*h):
        return None
    # A merged/overlapping paid label makes the entry uncertain.
    for i in items:
        if any(t in _text(i) for t in ('特惠', '礼包', '购买', '充值', '支付')):
            b = i.box
            if b.x < e.box.right and b.right > e.box.x and b.y < e.box.bottom and b.bottom > e.box.y:
                return None
    return _target(e, frame)


@dataclass(frozen=True)
class Surface:
    kind: str
    target: Rect | None = None
    back: Rect | None = None


def find_event_close_target(items, frame):
    """Tap above the owned modal, like normal signin; never send Android Back."""
    h, w = frame.shape[:2]
    headings = [i for i in items if i.score >= .8 and _text(i).strip('*★☆✦ ') == '活动'
                and .2*w < i.box.center[0] < .8*w and .15*h < i.box.center[1] < .35*h]
    if len(headings) != 1:
        return None
    heading = headings[0].box
    # Derive a small outside area from the modal header, away from HOME offers.
    gap, height = max(8, round(.012*h)), max(12, round(.015*h))
    width = max(20, round(.16*w))
    target = Rect(heading.center[0]-width//2, heading.y-gap-height, width, height)
    if target.y < .1*h or target.x < .25*w or target.right > .75*w:
        return None
    if any(i.score >= .8 and any(t in _text(i) for t in ('礼包', '购买', '充值', '支付'))
           and i.box.x < target.right and i.box.right > target.x
           and i.box.y < target.bottom and i.box.bottom > target.y for i in items):
        return None
    return target


def _observe(session, *, entered):
    frame = session.capture_frame()
    items = session.recognize_text(frame)
    confident = [i for i in items if i.score >= MIN_SCORE]
    texts = {_text(i) for i in confident}
    h, w = frame.shape[:2]
    content = [i for i in confident if .12*w < i.box.center[0] < .95*w
               and .22*h < i.box.center[1] < .9*h]
    exit_title = [i for i in confident if _text(i).rstrip('?？') == '是否退出游戏'
                  and .75*h < i.box.center[1] < .9*h]
    cancels = [i for i in confident if _text(i) == '取消'
               and .2*w < i.box.center[0] < .8*w and .9*h < i.box.center[1] < .99*h]
    # Recover only this known legacy Back-key dialog over an owned activity.
    if (len(exit_title) == len(cancels) == 1 and find_event_close_target(items, frame) is not None
            and any(_text(i) == '已领取' for i in content)
            and any('活动剩余时间' in _text(i) for i in confident)):
        return Surface('exit_cancel', _target(cancels[0], frame)), frame
    # Paid confirmation copy can itself say "获得奖励". It must never enter
    # the shared reward-dismiss flow, even when a calendar shows behind it.
    reward = (any(any(title in _text(i) for title in SIGNIN_REWARD_TITLES) for i in confident)
              or detect_reward_clothes_card(frame).matched or detect_reward_suit_showcase(frame).matched)
    if (entered or reward) and ('取消' in texts or any(any(t in _text(i) for t in (
            '购买', '充值', '支付', '付费', '立即补签', '消耗', '花费', '售价', '特惠礼包'))
            for i in content)):
        return Surface('unknown'), frame
    if reward:
        return Surface('reward'), frame
    if texts.intersection({'确定', '确认', '取消', '补签确认', '确认补签'}):
        return Surface('unknown'), frame
    if not entered:
        # A stopped task may already be on the owned event page. The generic
        # activity header and countdown identify it without any event name/date.
        heading = any(i.score >= .8 and _text(i).strip('*★☆✦ ') == '活动'
                      and .2*w < i.box.center[0] < .8*w
                      and .15*h < i.box.center[1] < .35*h for i in items)
        countdown = any(i.score >= .8 and '活动剩余时间' in _text(i)
                        and .15*h < i.box.center[1] < .4*h for i in items)
        owned = any(_text(i) in {'点击领取', '已领取'} for i in content)
        if heading and countdown and owned:
            return _observe_owned_page(session, frame, items, confident, texts, content)
        return (Surface('home', find_entry(items, frame)) if session.is_home(frame)
                else Surface('unknown')), frame
    return _observe_owned_page(session, frame, items, confident, texts, content)


def _observe_owned_page(session, frame, items, confident, texts, content):
    # Shared by normal activity entry and interruption recovery.
    if any(any(t in _text(i) for t in ('购买', '充值', '支付', '付费', '立即补签',
                                      '消耗', '花费', '售价', '特惠礼包')) for i in content):
        return Surface('unknown'), frame
    if '暖暖月签到' in texts:
        return Surface('unknown'), frame
    popup = detect_common_safe_popup(frame, items).kind
    if popup not in {CommonPopupKind.NONE, CommonPopupKind.UNKNOWN}:
        return Surface('unknown'), frame
    claims = [i for i in content if _text(i) == '点击领取']
    if claims or any(_text(i) == '已领取' for i in content):
        if any(_text(i) == '点击领取' and i.score < MIN_SCORE for i in items):
            return Surface('unknown'), frame
        targets = [_target(i, frame) for i in sorted(claims, key=lambda i: (i.box.y, i.box.x))]
        if any(t is None for t in targets):
            return Surface('unknown'), frame
        return Surface('page', targets[0] if targets else None), frame
    if session.is_home(frame):
        return Surface('home'), frame
    back = find_event_close_target(items, frame)
    if back is not None and len(content) >= 2:
        return Surface('page', back=back), frame
    return Surface('unknown'), frame


def run_limited_signin(session, *, capture=None, log=print):
    attempted = False
    last_frame = None
    deadline = session.monotonic() + TIMEOUT

    def observe(entered=True):
        nonlocal last_frame
        if session.monotonic() >= deadline:
            raise TimeoutError('限时活动签到超时')
        surface, last_frame = _observe(session, entered=entered)
        return surface

    def wait(kinds):
        for _ in range(MAX_POLLS):
            surface = observe()
            if surface.kind in kinds:
                return surface
            session.sleep(.5)
        raise RuntimeError('活动页面状态未确认')

    def click(surface, entered):
        def confirm():
            nonlocal attempted
            for index in range(2):
                fresh = observe(entered)
                if fresh.kind != surface.kind or not _same_target(fresh.target, surface.target):
                    return None
                if index == 0:
                    session.sleep(.5)
            if not entered:
                attempted = True
            return fresh.target
        return alliance_business_click(session, *surface.target.center, confirm=confirm)

    try:
        home = observe(False)
        if home.kind == 'exit_cancel':
            attempted = True
            if not click(home, False):
                raise RuntimeError('退出游戏弹窗取消未确认')
            session.sleep(.8)
            home = observe(False)
        if home.kind in {'page', 'reward'}:
            attempted = True
            surface = home
            log('[LimitedSignin] 接续当前限时签到页面。')
        elif home.kind != 'home' or home.target is None:
            log('[LimitedSignin] 未检测到主页限时签到入口，正常跳过。')
            return True
        else:
            # Revalidate before submission; if the entry disappears, skip without input.
            opened = click(home, False)
            if not opened:
                if not attempted:
                    log('[LimitedSignin] 入口在点击前变化，正常跳过。')
                    return True
                raise RuntimeError('活动入口点击结果未确认')
            session.sleep(.8)
            surface = wait({'page', 'reward'})
        claims = reward_passes = 0
        for _ in range(MAX_CLAIMS + MAX_POLLS + 13):
            if surface.kind == 'reward':
                if reward_passes >= 13:
                    raise RuntimeError('活动奖励关闭次数达到上限')
                reward_passes += 1
                if not session.handle_signin_rewards():
                    raise RuntimeError('活动奖励处理失败')
                surface = wait({'reward', 'page', 'home'})
                continue
            if surface.kind == 'home':
                return True
            if surface.target is not None:
                if claims >= MAX_CLAIMS:
                    raise RuntimeError('活动领取次数达到上限')
                previous = surface.target
                if not click(surface, True):
                    raise RuntimeError('领取前页面变化，不重复点击')
                claims += 1
                session.sleep(.8)
                surface = wait({'reward', 'page', 'home'})
                if surface.kind == 'page' and _same_target(surface.target, previous):
                    raise RuntimeError('领取状态未变化，不重复点击')
                continue
            # Rewards can appear after the claimed page, including during this
            # final confirmation. Drain them through the same bounded loop.
            wait_before_alliance_click(session)
            close = None
            for index in range(2):
                fresh = observe()
                if fresh.kind != 'page' or fresh.target is not None:
                    surface = fresh if fresh.kind in {'reward', 'home', 'page'} else wait({'reward', 'page', 'home'})
                    break
                current = session.find_close(last_frame)
                if index and not _same_target(close, current):
                    raise RuntimeError('退出前框外关闭区域变化')
                close = current
                if index == 0:
                    session.sleep(.5)
            else:
                if close is None:
                    raise RuntimeError('活动框外关闭区域未确认')
                if not session.click(*close.center):
                    raise RuntimeError('活动返回点击失败')
                session.sleep(.8)
                surface = wait({'home', 'reward'})
                if surface.kind == 'home':
                    log('[LimitedSignin] 活动签到结束，已确认返回 HOME。')
                    return True
            # No outside click was submitted when an overlay replaced the page.
            continue
        else:
            raise RuntimeError('活动处理步骤达到上限')
    except Exception as exc:
        log(f'[LimitedSignin] {"SafeStop" if attempted else "未进入活动，跳过"}: {exc}')
        if attempted and capture is not None:
            try:
                capture('QJNN_LimitedSigninFailure', last_frame)
            except Exception as capture_exc:
                log(f'[LimitedSignin] 诊断保存失败：{capture_exc}')
        return not attempted


def make_runtime_session(context):
    session = _make_runtime_session(context)

    def is_home(frame):
        result = context.run_recognition('QJNN_LimitedSigninHome', frame)
        return bool(result is not None and result.hit)

    def rewards():
        # Scoped clone: ordinary signin, makeup and event completion hooks cannot run here.
        result = context.clone().run_task('QJNN_SigninRewardFlow', {
            'QJNN_SigninRewardFlow': {'next': [
                'QJNN_SigninRewardPopup', 'QJNN_RewardSuitShowcase',
                'QJNN_RewardClothesScreen', 'QJNN_LimitedSigninRewardSettled']},
        })
        return bool(result is not None and result.status.succeeded and not any(
            n.name in {'QJNN_SafeStop', 'QJNN_SigninFailure'} for n in result.nodes))

    session.is_home = is_home
    session.find_close = lambda frame: find_event_close_target(session.recognize_text(frame), frame)
    session.handle_signin_rewards = rewards
    return session


class LimitedSigninAction(CustomAction):
    def run(self, context, argv):
        try:
            params = json.loads(argv.custom_action_param or '{}')
            if not isinstance(params, dict) or type(params.get('enabled', False)) is not bool:
                return False
            if not params.get('enabled', False):
                return True
            capture = DiagnosticFrameStore(DATA_ROOT / 'images/debug/pipeline/limited_signin')
            return run_limited_signin(make_runtime_session(context), capture=capture.capture)
        except Exception as exc:
            print(f'[LimitedSignin] SafeStop: {exc}')
            return False


class LimitedSigninPageRecognition(CustomRecognition):
    def analyze(self, context, argv):
        from types import SimpleNamespace
        items = _context_ocr_items(context, 'QJNN_AgentOcrBase', argv.image)
        surface, _ = _observe(SimpleNamespace(
            capture_frame=lambda: argv.image, recognize_text=lambda _: items,
            is_home=lambda _: False, find_back=lambda _: None), entered=False)
        return CustomRecognition.AnalyzeResult(
            box=[0, 0, 1, 1] if surface.kind in {'page', 'exit_cancel'} else None,
            detail={'surface': surface.kind})
