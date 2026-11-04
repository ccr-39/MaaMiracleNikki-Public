"""Offline update regressions; no real device or ADB process is used."""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "agent"), str(ROOT / "tools")]
from qjnn_startup_update import GAME_PACKAGE, StartupUpdateWaitAction, wait_for_update


class Clock:
    now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeContext:
    def __init__(self, frames, *, captured=True):
        self.frames = iter(frames)
        self.frame = (False, None)
        self.calls = []
        self.tasker = SimpleNamespace(stopping=False)
        self.tasker.controller = SimpleNamespace(
            cached_image=np.zeros((10, 10, 3), dtype=np.uint8),
            post_screencap=self.capture,
        )
        self.captured = captured

    def capture(self):
        self.frame = next(self.frames, self.frame)
        return SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=self.captured))

    def run_recognition(self, name, frame, pipeline_override=None):
        self.calls.append(name)
        progress, ready = self.frame
        hit = progress if name == "QJNN_UpdateProgressText" else name == ready
        return SimpleNamespace(hit=hit)


class UpdateWaitTests(unittest.TestCase):
    def run_wait(self, context, package=GAME_PACKAGE, timeout=10, sleep=None):
        clock = Clock()
        result = wait_for_update(
            context, timeout_seconds=timeout, interval_seconds=1,
            package_resolver=lambda _: package,
            monotonic=clock.monotonic, sleep=sleep or clock.sleep,
        )
        return result, clock.now

    def test_progress_blocks_visible_underlying_login_until_stable(self):
        login = "QJNN_LoginScreenGate"
        context = FakeContext([(True, login)] * 4 + [(False, login)] * 3)
        ready, elapsed = self.run_wait(context)
        self.assertEqual(ready, login)
        self.assertEqual(elapsed, 6)
        self.assertEqual(context.calls[:4], ["QJNN_UpdateProgressText"] * 4)

    def test_loading_and_flickering_screens_cannot_finish_wait(self):
        start = "QJNN_StartButtonTemplateGate"
        home = "QJNN_HomeScreenTemplateGate"
        context = FakeContext([(False, start), (False, None), (False, home),
                               (False, start), (False, start), (False, start)])
        self.assertEqual(self.run_wait(context), (start, 5))

    def test_progress_timeout_never_clicks_or_completes(self):
        context = FakeContext([(True, "QJNN_HomeScreenTemplateGate")])
        self.assertEqual(self.run_wait(context, timeout=4), (None, 4))

    def test_failed_capture_does_not_accept_cached_ready_frame(self):
        context = FakeContext([(False, "QJNN_HomeScreenTemplateGate")], captured=False)
        self.assertIsNone(self.run_wait(context, timeout=3)[0])
        self.assertEqual(context.calls, [])

    def test_unknown_foreground_does_not_ocr_or_advance(self):
        context = FakeContext([(False, "QJNN_HomeScreenTemplateGate")])
        self.assertIsNone(self.run_wait(context, package=None, timeout=3)[0])
        self.assertEqual(context.calls, [])

    def test_third_party_login_routes_without_ocr(self):
        context = FakeContext([])
        self.assertEqual(self.run_wait(context, package="com.tencent.mobileqq")[0],
                         "QJNN_PrivateLoginForeground")
        self.assertEqual(context.calls, [])

    def test_stop_interrupts_wait(self):
        context = FakeContext([(True, None)])
        def stop(_):
            context.tasker.stopping = True
        self.assertIsNone(self.run_wait(context, sleep=stop)[0])

    def test_action_routes_only_to_recognized_page(self):
        context = SimpleNamespace(override_next=lambda name, targets: targets == ["QJNN_HomeScreenTemplateGate"])
        with patch("qjnn_startup_update.Tasker.set_save_on_error", return_value=True), \
             patch("qjnn_startup_update.wait_for_update", return_value="QJNN_HomeScreenTemplateGate"):
            self.assertTrue(StartupUpdateWaitAction().run(context, SimpleNamespace(node_name="QJNN_UpdateWait")))

    def test_action_timeout_does_not_resume_pipeline(self):
        self.assertFalse(StartupUpdateWaitAction.allow_back_to_home_recovery)
        with patch("qjnn_startup_update.Tasker.set_save_on_error", return_value=True), \
             patch("qjnn_startup_update.wait_for_update", return_value=None):
            self.assertFalse(StartupUpdateWaitAction().run(SimpleNamespace(), SimpleNamespace()))

    def test_failed_progress_recognition_does_not_accept_ready_screen(self):
        context = FakeContext([(False, "QJNN_HomeScreenTemplateGate")])
        context.run_recognition = lambda *args: None
        self.assertIsNone(self.run_wait(context, timeout=3)[0])


class UpdatePromptNativeTests(unittest.TestCase):
    def test_native_and_requires_every_cue_and_returns_confirm_box(self):
        from maa.custom_recognition import CustomRecognition
        from maa.library import Library
        from maa.pipeline import JAnd, JRecognitionType
        from maa.resource import Resource
        from maa.tasker import Tasker

        pipeline = json.loads((ROOT / "assets/resource/pipeline/10_startup.json").read_text(encoding="utf-8"))
        confirm = pipeline["QJNN_UpdateConfirm"]

        class CueRecognition(CustomRecognition):
            def analyze(self, context, argv):
                param = json.loads(argv.custom_recognition_param)
                hit = param["enabled"] and (not param["expected"] or
                    any(re.search(pattern, param["text"]) for pattern in param["expected"]))
                return CustomRecognition.AnalyzeResult(box=param["box"] if hit else None, detail={})

        if not all((ROOT / "assets/resource/model/ocr" / name).is_file()
                   for name in ("det.onnx", "rec.onnx", "keys.txt")):
            self.skipTest("Optional local OCR models are not present in source-only checkout")
        resource = Resource()
        self.assertTrue(resource.post_bundle(ROOT / "assets/resource").wait().succeeded)
        self.assertTrue(resource.register_custom_recognition("test_update_cue", CueRecognition()))
        tasker = Tasker()
        tasker._resource_holder = resource
        self.assertTrue(Library.framework().MaaTaskerBindResource(tasker._handle, resource._handle))
        texts = ["", "检测到有新版本", "更新包大小:2.399兆", "确 定"]
        boxes = [[0, 0, 1, 1], [180, 400, 220, 30], [160, 440, 260, 30], [300, 650, 90, 35]]
        # Execute the actual native And/box_index semantics with deterministic
        # recognition callbacks instead of OCR or a device connection.
        for missing in (None, 0, 1, 2, 3):
            with self.subTest(missing_cue=missing):
                parts = []
                for index, part in enumerate(confirm["all_of"]):
                    parts.append({"recognition": "Custom", "custom_recognition": "test_update_cue",
                        "custom_recognition_param": {"enabled": missing != index, "text": texts[index],
                            "expected": part.get("expected", []) if isinstance(part, dict) else [],
                            "box": boxes[index]}})
                result = tasker.post_recognition(JRecognitionType.And,
                    JAnd(all_of=parts, box_index=confirm["box_index"]),
                    np.zeros((1280, 720, 3), dtype=np.uint8)).get(wait=True)
                self.assertIsNotNone(result)
                detail = result.nodes[0].recognition
                self.assertEqual(detail.hit, missing is None)
                if missing is None:
                    box = detail.box
                    self.assertEqual([box.x, box.y, box.w, box.h], boxes[3])

    def test_update_precedes_underlying_login_or_start_screen(self):
        pipeline = json.loads((ROOT / "assets/resource/pipeline/10_startup.json").read_text(encoding="utf-8"))
        for entry in ("QJNN_StartAndEnterHome", "QJNN_WaitForGameState"):
            candidates = pipeline[entry]["next"]
            self.assertEqual(candidates[:2], ["QJNN_PrivateLoginForeground", "QJNN_UpdateConfirm"])


if __name__ == "__main__":
    unittest.main()
