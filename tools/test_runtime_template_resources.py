"""Runtime template regressions using real cropped assets and mock OCR only.

The replay frames below are test compositions, not captured game screenshots.
No device, downloaded model or private screenshot is needed for these checks.
"""
from pathlib import Path
import hashlib
import shutil
import sys

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

import qjnn_dreamweaver_commission as dream
import qjnn_world_tour as tour
from qjnn_alliance_commission import OcrItem, Rect
from offline_test_support import require_file


TEMPLATES = (
    ("assets/resource/image/world_tour/event_collected_stamp.png",
     "images/screenshots/world_tour/world_tour_event_collected.png",
     (600, 630, 105, 90),
     "9b7f15432f5d0a4226af8339dd6763cae8886ae1de04bf5dc0bf11886869fa4f"),
    ("assets/resource/image/dreamweaver/missing_clothes_plus.png",
     "images/screenshots/dreamweaver/commission_list_initial.png",
     (228, 925, 42, 43),
     "abe1039518204fa5a47a9382de61584e4b03de0740355b916b1b217011fe9bcc"),
)


@pytest.fixture
def source_only_templates(tmp_path, monkeypatch):
    for name, *_ in TEMPLATES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    assert not (tmp_path / "images").exists()
    monkeypatch.setattr(tour, "ROOT", tmp_path)
    monkeypatch.setattr(dream, "PROJECT_ROOT", tmp_path)
    # Cached reads must not conceal a dependency on the developer's workspace.
    tour._event_stamp_template.cache_clear()
    dream._missing_plus_template.cache_clear()
    original_read = np.fromfile

    def asset_read(path, *args, **kwargs):
        assert "images" not in Path(path).parts, "Runtime read of private capture"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(np, "fromfile", asset_read)
    yield tmp_path
    tour._event_stamp_template.cache_clear()
    dream._missing_plus_template.cache_clear()


@pytest.mark.parametrize("index", (0, 1))
def test_runtime_templates_keep_original_pixels(source_only_templates, index):
    loader = (tour._event_stamp_template, dream._missing_plus_template)[index]
    image = loader()
    _, _, (_, _, width, height), digest = TEMPLATES[index]
    assert image.shape == (height, width, 3)
    assert hashlib.sha256(image.tobytes()).hexdigest() == digest


@pytest.mark.parametrize("index", (0, 1))
def test_template_is_exact_legacy_crop_when_private_evidence_exists(index):
    name, original, (x, y, width, height), _ = TEMPLATES[index]
    reference_path = require_file(ROOT / original)
    reference = cv2.imdecode(np.fromfile(reference_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    actual = cv2.imdecode(np.fromfile(ROOT / name, dtype=np.uint8), cv2.IMREAD_COLOR)
    np.testing.assert_array_equal(actual, reference[y:y+height, x:x+width])


def event_frame(*, left=600, top=630):
    frame = np.zeros((1600, 900, 3), dtype=np.uint8)
    frame[top:top+90, left:left+105] = tour._event_stamp_template()
    return frame


def title(*, score=.99, box=Rect(330, 560, 150, 50), text="新事件！"):
    return OcrItem(text, box, score)


@pytest.mark.parametrize("heading", (
    title(score=.3), title(box=Rect(10, 10, 150, 50)),
    title(text="剧情提到新事件"), None,
))
def test_event_stamp_alone_never_confirms_event(source_only_templates, heading):
    state = tour.detect_world_tour_state([] if heading is None else [heading], event_frame())
    assert state is None or state.state is not tour.WorldTourState.EVENT_COLLECTED


def test_event_fallback_requires_stamp_in_unchanged_search_roi(source_only_templates):
    state = tour.detect_world_tour_state([title()], event_frame())
    assert state.state is tour.WorldTourState.EVENT_COLLECTED
    for frame in (np.zeros((1600, 900, 3), dtype=np.uint8), event_frame(left=100, top=900)):
        state = tour.detect_world_tour_state([title()], frame)
        assert state is None or state.state is not tour.WorldTourState.EVENT_COLLECTED


@pytest.mark.parametrize("score,expected", ((.899, False), (.9, True), (.901, True)))
def test_event_matching_threshold_unchanged(source_only_templates, monkeypatch, score, expected):
    monkeypatch.setattr(cv2, "minMaxLoc", lambda _: (0., score, (0, 0), (0, 0)))
    assert tour._event_stamp_layout(event_frame()) is expected


def plus_frame(index=1, *, duplicate=False):
    frame = np.zeros((1600, 900, 3), dtype=np.uint8)
    y = (715, 945, 1170)[index] - 21
    frame[y:y+43, 228:270] = dream._missing_plus_template()
    if duplicate:
        frame[y:y+43, 328:370] = dream._missing_plus_template()
    return frame


@pytest.mark.parametrize("index", (0, 1, 2))
@pytest.mark.parametrize("scale", (.8, 1.))
def test_missing_plus_remains_located_by_glyph_not_ocr_width(source_only_templates, index, scale):
    frame = cv2.resize(plus_frame(index), (round(900*scale), round(1600*scale)))
    slot = dream.SlotSnapshot(index, dream.SlotState.CLOTHES_MISSING, Rect(90, 700, 180, 30))
    found = dream.find_missing_plus(frame, slot)
    assert found is not None
    expected = (249*scale, (715, 945, 1170)[index]*scale)
    assert all(abs(actual - wanted) <= 2 for actual, wanted in zip(found.center, expected))


def test_missing_plus_keeps_unknown_ambiguous_and_wrong_slot_guards(source_only_templates):
    slot = dream.SlotSnapshot(1, dream.SlotState.CLOTHES_MISSING, Rect(90, 930, 180, 30))
    assert dream.find_missing_plus(np.zeros((1600, 900, 3), dtype=np.uint8), slot) is None
    assert dream.find_missing_plus(plus_frame(1, duplicate=True), slot) is None
    for state in (dream.SlotState.COMPLETED, dream.SlotState.UNKNOWN, dream.SlotState.SUBMITTABLE):
        assert dream.find_missing_plus(plus_frame(), dream.SlotSnapshot(1, state, None)) is None
    assert dream.find_missing_plus(plus_frame(0), slot) is None


class ReplaySession:
    def __init__(self, frame, items, *, lose_frame_on_delay=False):
        self.frame, self.items = frame, items
        self.lose_frame_on_delay = lose_frame_on_delay
        self.now = 0.
        self.clicks = []

    def capture_frame(self):
        return self.frame

    def recognize_text(self, frame):
        return self.items

    def sleep(self, seconds):
        self.now += seconds
        if self.lose_frame_on_delay:
            self.frame = np.zeros_like(self.frame)

    def monotonic(self):
        return self.now

    def click(self, x, y):
        self.clicks.append((x, y))
        self.frame, self.items = np.zeros_like(self.frame), []


@pytest.mark.parametrize("lose_frame", (False, True))
def test_world_event_branch_reconfirms_before_click(source_only_templates, lose_frame):
    session = ReplaySession(event_frame(), [title()], lose_frame_on_delay=lose_frame)
    result = tour.run_world_tour(session, capture=lambda *_: None, log=lambda _: None,
                                loading_classifier=lambda *_: False)
    # After the one confirmed event, an unknown next frame must still stop.
    assert not result.success
    assert session.clicks == ([] if lose_frame else [(450, 1420)])


@pytest.mark.parametrize("lose_frame", (False, True))
def test_missing_clothes_branch_reconfirms_before_click(source_only_templates, lose_frame):
    items = [OcrItem("学会委托", Rect(368, 350, 151, 41), .99),
             OcrItem("已拥有 0/1", Rect(95, 930, 130, 30), .99)]
    session = ReplaySession(plus_frame(), items, lose_frame_on_delay=lose_frame)
    slot = dream.classify_slots(items, session.frame)[1]
    result = dream.acquire_missing_clothes(session, slot, log=lambda _: None)
    assert not result.success
    assert result.reason == ("missing_plus_unconfirmed" if lose_frame else "obtain_route_unknown")
    assert session.clicks == ([] if lose_frame else [(249, 945)])


@pytest.mark.parametrize("index", (0, 1))
def test_missing_runtime_template_cannot_be_treated_as_success(source_only_templates, index):
    (source_only_templates / TEMPLATES[index][0]).unlink()
    loader = (tour._event_stamp_template, dream._missing_plus_template)[index]
    with pytest.raises(FileNotFoundError):
        loader()
