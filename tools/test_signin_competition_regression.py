"""Focused offline regressions; no device, private image or user configuration."""
from pathlib import Path
from types import SimpleNamespace as NS
import sys
from threading import Event

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / 'agent')]
import qjnn_competition as competition
import qjnn_back_to_home as recovery
import qjnn_reward as reward
from qjnn_signin import SigninStateRecognition
from qjnn_alliance_commission import OcrItem, Rect


def item(text, box, score=.99):
    return OcrItem(text, Rect(*box), score)


def judge_items():
    return [item('本期搭配主题', (280, 12, 150, 30)),
            item('赞', (145, 1130, 60, 40)), item('赞', (495, 1130, 60, 40)),
            item('次数27/30', (495, 1228, 160, 30))]


def detail(rows, hit=True):
    converted = [NS(text=i.text, box=[i.box.x, i.box.y, i.box.width, i.box.height], score=i.score) for i in rows]
    return NS(all_results=converted, best_result=converted[0] if converted else None, hit=hit)


@pytest.mark.parametrize('page,claim', [(False, False), (True, False), (True, True)])
def test_signin_only_skips_claim_fallback_outside_calendar(monkeypatch, page, claim):
    for name in ('detect_reward_clothes_card', 'detect_reward_suit_showcase'):
        monkeypatch.setattr(reward, name, lambda _: NS(matched=False))
    calls = []
    class Context:
        def run_recognition(self, node, frame):
            calls.append(node)
            if node == 'QJNN_AgentOcrBase':
                return detail([item('暖暖月签到', (200, 200, 180, 30))] if page else [])
            return detail([item('点击领取', (300, 600, 90, 30))] if claim else [], hit=claim)
    result = SigninStateRecognition().analyze(Context(), NS(image=np.zeros((1280, 720, 3), dtype=np.uint8),
        custom_recognition_param='{"state":"CLAIMED"}'))
    assert ('QJNN_SigninClaim' in calls) == page
    assert result.detail['state'] == ('NOT_SIGNIN' if not page else 'CLAIMABLE' if claim else 'CLAIMED')


@pytest.mark.parametrize('score,expected', [(.99, ('new-left', 'new-right')), (.69, None)])
def test_focused_names_preserve_coordinates_and_confidence(score, expected):
    frame = np.zeros((1280, 720, 3), dtype=np.uint8)
    calls = []
    class Context:
        def run_recognition(self, node, image):
            calls.append(image.shape)
            if image.shape == frame.shape:
                return detail(judge_items())
            name = 'new-left' if len(calls) == 2 else 'new-right'
            return detail([item(name, (20, 45, 90, 30), score)])
    rows = competition._recognize_competition_text(Context(), frame)
    assert len(calls) == 3
    assert competition.find_player_pair(rows, frame) == expected
    assert rows[-1].box.x == 460


def test_crop_fallback_does_not_run_on_unknown_page():
    calls = []
    class Context:
        def run_recognition(self, node, frame):
            calls.append(node)
            return detail([])
    assert competition._recognize_competition_text(Context(), np.zeros((1280, 720, 3), dtype=np.uint8)) == []
    assert len(calls) == 1


def test_existing_names_are_not_replaced_or_recognized_again():
    frame = np.zeros((1280, 720, 3), dtype=np.uint8)
    rows = judge_items() + [item('left', (110, 1070, 80, 30)), item('right', (480, 1070, 80, 30))]
    calls = []
    class Context:
        def run_recognition(self, node, image):
            calls.append(image.shape)
            return detail(rows)
    assert competition.find_player_pair(competition._recognize_competition_text(Context(), frame), frame) == ('left', 'right')
    assert len(calls) == 1


def test_vote_refresh_still_requires_two_consecutive_name_observations():
    frame = np.zeros((1280, 720, 3), dtype=np.uint8)
    previous = competition.CompetitionJudgeState(Rect(145, 1130, 60, 40), Rect(495, 1130, 60, 40),
                                                28, 'old-left', 'old-right')
    tracker = competition.CompetitionVoteRefreshTracker(previous)
    fresh = judge_items() + [item('new-right', (480, 1070, 90, 30))]
    assert tracker.observe(fresh, frame) is None
    assert tracker.observe(judge_items(), frame) is None
    assert tracker.observe(fresh, frame) is None
    assert tracker.observe(fresh, frame) is not None


class ExitReplay:
    def __init__(self, blocked_arrow=False, never_home=False, confirmation=False):
        self.page = 0
        self.t = 0.
        self.captures = 0
        self.clicks = []
        self.blocked_arrow = blocked_arrow
        self.never_home = never_home
        self.confirmation = confirmation

    def capture_frame(self):
        self.t += .1
        self.captures += 1
        frame = np.full((1280, 720, 3), self.captures % 50 + 80, dtype=np.uint8)
        frame[0, 0, 0] = self.page + 1
        if self.page < 2 and not self.blocked_arrow:
            name = 'judge_back_button.png' if self.page == 0 else 'home_back_button.png'
            template = cv2.imdecode(np.fromfile(ROOT / 'assets/resource/image/competition' / name, dtype=np.uint8), cv2.IMREAD_COLOR)
            h, w = template.shape[:2]
            frame[16:16+h, 28:28+w] = template
        return frame

    def recognize_text(self, frame):
        if self.page == 0:
            return judge_items() + ([item('取消', (300, 600, 50, 30)), item('确定', (400, 600, 50, 30))] if self.confirmation else [])
        if self.page == 1:
            return [item('我当评委', (280, 1040, 160, 50))]
        return []

    def click(self, x, y):
        self.clicks.append((x, y))
        self.page = min(1 if self.never_home else 2, self.page + 1)
        return True

    def sleep(self, seconds):
        self.t += seconds

    def monotonic(self):
        return self.t


@pytest.fixture
def owned_recovery(monkeypatch):
    import qjnn_home_room_entry as home
    monkeypatch.setattr(recovery, 'diagnostic_event', lambda *a, **k: None)
    monkeypatch.setattr(recovery, '_diagnostic_logger', lambda: NS(debug=lambda _: None))
    monkeypatch.setattr(recovery, 'detect_home_page', lambda f: NS(page='P3' if f[0, 0, 0] == 3 else 'Unknown', left_score=0., right_score=0.))
    monkeypatch.setattr(recovery, 'detect_common_safe_popup', lambda *a: NS(kind=recovery.CommonPopupKind.NONE))
    monkeypatch.setattr(home, 'home_toolbar_anchor_count', lambda *a: 2)


def test_owned_competition_exit_handles_animated_outfits(owned_recovery):
    session = ExitReplay()
    assert recovery.return_home_after_failure(session, log=lambda _: None)
    assert len(session.clicks) == 2 and session.page == 2
    assert all(28 <= x <= 138 and 16 <= y <= 126 for x, y in session.clicks)
    assert session.captures >= 8  # Multiple fresh owned-page and HOME confirmations.


@pytest.mark.parametrize('options', [{'blocked_arrow': True}, {'confirmation': True}, {'never_home': True}])
def test_competition_recovery_does_not_claim_unconfirmed_home(owned_recovery, options):
    session = ExitReplay(**options)
    assert not recovery.return_home_after_failure(session, log=lambda _: None)
    assert len(session.clicks) == (2 if options.get('never_home') else 0)


def test_competition_recovery_obeys_cancellation(owned_recovery):
    session = ExitReplay()
    stop = Event(); stop.set()
    assert not recovery.return_home_after_failure(session, stop_event=stop, log=lambda _: None)
    assert session.clicks == []


@pytest.mark.parametrize('overlay', ['unknown', 'reward_title', 'reward_card'])
def test_competition_recovery_never_clicks_through_overlay(owned_recovery, monkeypatch, overlay):
    session = ExitReplay()
    if overlay == 'unknown':
        monkeypatch.setattr(recovery, 'detect_common_safe_popup', lambda *a: NS(kind=recovery.CommonPopupKind.UNKNOWN))
    elif overlay == 'reward_title':
        original = session.recognize_text
        session.recognize_text = lambda f: original(f) + [item('恭喜获得', (280, 400, 150, 30))]
    else:
        monkeypatch.setattr(reward, 'detect_reward_clothes_card', lambda _: NS(matched=True))
    assert not recovery.return_home_after_failure(session, log=lambda _: None)
    assert session.clicks == []
