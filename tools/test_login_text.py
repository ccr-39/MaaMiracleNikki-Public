"""Offline provider-neutral login regression; private samples never exported."""
from pathlib import Path
import sys

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "agent")]
from qjnn_login import LoginTextReader, login_screen_evidence


@pytest.mark.parametrize("label", ["QQ 登录", "微信登录", "微 信 登 录"])
def test_either_provider_needs_title_controls(label):
    responses = iter([[label], ["设置", "修复", "清除"]])
    assert login_screen_evidence(np.zeros((1280, 720, 3), np.uint8), lambda _: next(responses)) is True


@pytest.mark.parametrize("responses,expected", [
    ([["QQ登录"], []], None),
    ([[], ["设置", "修复"]], None),
    ([["登录"], []], None),
    ([["系统邮件"], ["返回"]], False),
])
def test_partial_evidence_cannot_be_treated_as_a_safe_negative(responses, expected):
    values = iter(responses)
    assert login_screen_evidence(np.zeros((1280, 720, 3), np.uint8), lambda _: next(values)) is expected


@pytest.fixture(scope="module")
def reader():
    from maa.tasker import Tasker
    assert Tasker.set_save_on_error(False)
    return LoginTextReader()


@pytest.mark.parametrize("provider", ["qq", "wechat"])
@pytest.mark.parametrize("width", [720, 900])
def test_real_title_with_all_provider_icons_masked(reader, provider, width):
    path = ROOT / "images/screenshots/start/login_001.png"
    if not path.is_file():
        pytest.skip("Private local login sample absent")
    frame = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
    # Mask the icons and the other provider's entire button, keeping just text.
    frame[1200:1310, 40:165] = 255
    frame[1200:1310, 500:610] = 255
    if provider == "qq":
        frame[1190:1320, :450] = 255
    else:
        frame[1190:1320, 450:] = 255
    frame = cv2.resize(frame, (width, round(frame.shape[0] * width / frame.shape[1])))
    assert login_screen_evidence(frame, reader) is True


@pytest.mark.parametrize("relative", [
    "start/start_001.png", "start/enter_game_001.png", "home/home_001.png",
    "daily/daily_002.png", "daily/daily_006.png",
])
def test_existing_non_login_pages_have_no_login_evidence(reader, relative):
    path = ROOT / "images/screenshots" / relative
    if not path.is_file():
        pytest.skip("Private local sample absent")
    frame = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
    assert login_screen_evidence(frame, reader) is False
