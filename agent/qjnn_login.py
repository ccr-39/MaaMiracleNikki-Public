"""Provider-neutral, read-only title-page recognition; never submits input."""
from __future__ import annotations

import re

from maa.custom_recognition import CustomRecognition


class LoginTextReader:
    """Separate OCR-only Tasker: safe to call inside a controller callback.

    Only two game-title UI regions enter OCR, never a third-party app frame.
    No controller is constructed or bound, and no screenshot/input is requested.
    """

    def __init__(self):
        self.tasker = None

    def __call__(self, image):
        from app_paths import RESOURCE_ROOT
        from maa.library import Library
        from maa.pipeline import JOCR, JRecognitionType
        from maa.resource import Resource
        from maa.tasker import Tasker

        if self.tasker is None:
            if not Tasker.set_save_draw(False):
                raise RuntimeError("Cannot disable OCR image recording")
            resource = Resource()
            models = RESOURCE_ROOT / "assets/resource/model/ocr"
            if not resource.post_ocr_model(models).wait().succeeded:
                raise RuntimeError("Cannot load login OCR model")
            tasker = Tasker()
            # Same official resource-only binding used by tools/test_ocr.py.
            tasker._resource_holder = resource
            if not Library.framework().MaaTaskerBindResource(tasker._handle, resource._handle):
                raise RuntimeError("Cannot bind login OCR resource")
            self.tasker = tasker
        task = self.tasker.post_recognition(
            JRecognitionType.OCR, JOCR(threshold=0.6), image
        ).get(wait=True)
        if task is None or not task.nodes or task.nodes[0].recognition is None:
            raise RuntimeError("Login OCR did not return a result")
        # Do not forward text, boxes or frames to the application logger.
        return [item.text for item in task.nodes[0].recognition.all_results if item.score >= 0.6]


def login_screen_evidence(frame, read_text) -> bool | None:
    """True: login text + title controls; None: partial evidence, fail closed.

    False means no login evidence, not proof of any actionable game state.
    Existing task-specific recognitions must still confirm every action.
    Coordinates are relative, supporting both raw and normalized screenshots.
    """
    height, width = frame.shape[:2]
    if width < 100 or height < width:
        return None
    button_band = frame[int(height * .68):int(height * .86), int(width * .02):int(width * .98)]
    title_controls = frame[:int(height * .34), int(width * .82):]
    buttons = [re.sub(r"\s+", "", text) for text in read_text(button_band)]
    controls = [re.sub(r"\s+", "", text) for text in read_text(title_controls)]
    login = any(re.search(r"(?:[Qq][Qq]|微信)登[录錄]", text) for text in buttons)
    chrome = sum(any(word in text for text in controls) for word in ("设置", "修复", "清除")) >= 2
    if login and chrome:
        return True
    if not login and chrome and any(re.fullmatch(r"点击开始|进[入人]游戏", text) for text in buttons):
        # The logged-in title uses the same side controls. This only releases
        # the frame; the existing OCR/template gate still owns the next action.
        return False
    if login or chrome or any("登录" in text or "登錄" in text for text in buttons):
        return None
    return False


class LoginScreenRecognition(CustomRecognition):
    def __init__(self):
        super().__init__()
        self.reader = LoginTextReader()

    def analyze(self, context, argv):
        from qjnn_foreground import focused_package
        from qjnn_privacy import GAME_PACKAGE

        matched = False
        if focused_package(context.tasker.controller) == GAME_PACKAGE:
            try:
                matched = login_screen_evidence(argv.image, self.reader) is True
            except Exception:
                pass  # No text, image or exception content enters diagnostic detail.
        return CustomRecognition.AnalyzeResult(
            box=[0, 0, 1, 1] if matched else None,
            detail={"login_screen": matched},
        )
