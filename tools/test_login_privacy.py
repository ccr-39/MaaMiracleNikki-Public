"""Login privacy boundaries with in-memory frames; no device or real tasks."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "agent"), str(ROOT / "tools")]

import qjnn_privacy as privacy
from qjnn_common_popup import run_common_popup_preflight
from qjnn_foreground import launcher_package
from maa_recovering_controller import RecoveringController
from gui.task_registry import TASKS
from tools.run_task import _run_pipeline, register_local_extensions, TaskRuntime


GAME = privacy.GAME_PACKAGE
QQ, WECHAT = "com.tencent.mobileqq", "com.tencent.mm"


class Clock:
    def __init__(self):
        self.now = 0.0
        self.on_sleep = lambda: None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.on_sleep()


class Native:
    def __init__(self, package=GAME, frames=()):
        self.package = package
        self.frames = list(frames)
        self.cached_image = np.full((32, 18, 3), 2, dtype=np.uint8)
        self.captures = 0
        self.on_capture = lambda: None

    def post_screencap(self):
        self.captures += 1
        if self.frames:
            self.package, value = self.frames.pop(0)
            self.cached_image = np.full((32, 18, 3), value, dtype=np.uint8)
        self.on_capture()
        return SimpleNamespace(wait=lambda: SimpleNamespace(succeeded=True))


class LoginPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stop = Event()
        self.clock = Clock()
        self.native = Native()
        self.output = self.stack.enter_context(redirect_stdout(StringIO()))
        self.save = self.stack.enter_context(patch.object(privacy.Tasker, "set_save_on_error", return_value=True))
        self.load = self.stack.enter_context(patch.object(privacy, "_load_template", side_effect=lambda p: str(p)))
        self.stack.enter_context(patch.object(privacy, "_matches", side_effect=self.matches))
        self.evidence = self.stack.enter_context(patch.object(
            privacy, "login_screen_evidence", side_effect=lambda frame, _: bool(frame[0, 0, 0] == 5)))
        self.focus = self.stack.enter_context(patch.object(privacy, "focused_package", side_effect=lambda _: self.native.package))
        self.launcher = self.stack.enter_context(patch.object(privacy, "launcher_package", return_value="app.launcher"))
        real_wait = privacy.wait_for_safe_game_screen
        self.wait = self.stack.enter_context(patch.object(privacy, "wait_for_safe_game_screen", side_effect=lambda *a, **kw: real_wait(
            *a, **kw, monotonic=self.clock.monotonic, sleep=self.clock.sleep)))
        self.guard = privacy.LoginPrivacyGuard(SimpleNamespace(info={}), cancelled=self.stop.is_set)

    @staticmethod
    def matches(frame, template, threshold):
        value = int(frame[0, 0, 0])
        return ("signin_entry" in template and value in {2, 3, 4}) or (
            "login_marker" in template and value == 5)

    def test_qq_and_wechat_wait_in_memory_before_preflight_ocr(self):
        for package in (QQ, WECHAT):
            with self.subTest(package=package):
                self.native = Native(package, [(package, 1), (GAME, 2)])
                self.native.on_capture = lambda: self.assertEqual(self.save.call_args.args, (False,))
                calls, captures = [], Mock()
                def capture():
                    calls.append("gate")
                    frame = self.guard.capture(self.native)
                    self.assertIsNotNone(frame)
                    return frame
                def ocr(frame):
                    self.assertEqual(self.native.package, GAME)
                    self.assertEqual(int(frame[0, 0, 0]), 2)
                    calls.append("ocr")
                    return []
                session = SimpleNamespace(capture_frame=capture, recognize_text=ocr,
                                          click=Mock(side_effect=AssertionError("unexpected click")))
                self.assertTrue(run_common_popup_preflight(session, capture=captures, log=lambda _: None))
                self.assertEqual(calls, ["gate", "ocr"])
                captures.assert_not_called()
                session.click.assert_not_called()
                self.assertIn(False, [c.args[0] for c in self.save.call_args_list])
        self.assertNotIn("sensitive-account", self.output.getvalue())

    def test_all_formal_entries_keep_preflight_then_original_task(self):
        for spec in TASKS:
            with self.subTest(entry=spec.entry):
                self.native = Native(WECHAT, [(WECHAT, 1), (GAME, 3)])
                names = []
                def post(name, **kwargs):
                    names.append(name)
                    frame = self.guard.capture(self.native)
                    self.assertIsNotNone(frame)
                    self.assertEqual(self.native.package, GAME)
                    self.assertIn(int(frame[0, 0, 0]), {2, 3, 4})
                    result = SimpleNamespace(status=SimpleNamespace(succeeded=True),
                                             nodes=[SimpleNamespace(name=name, completed=True)])
                    return SimpleNamespace(get=lambda wait=False: result)
                runtime = TaskRuntime()
                runtime.tasker = SimpleNamespace(post_task=post, stopping=False)
                runtime.controller = SimpleNamespace(post_screencap=lambda: SimpleNamespace(
                    wait=lambda: SimpleNamespace(succeeded=self.guard.capture(self.native) is not None)))
                self.assertEqual(runtime.execute(spec.entry), 0)
                self.assertEqual(names, ["QJNN_CommonPopupPreflight", spec.entry])

    def test_long_manual_login_finishes_before_pipeline_timeout_starts(self):
        self.native = Native(QQ, [(QQ, 1)] * 30 + [(GAME, 2)])
        runtime = TaskRuntime()
        runtime.tasker = Mock(stopping=False)
        runtime.controller = SimpleNamespace(post_screencap=lambda: SimpleNamespace(
            wait=lambda: SimpleNamespace(succeeded=self.guard.capture(self.native) is not None)))
        def submit(*args, **kwargs):
            self.assertGreater(self.clock.now, 15)
            self.assertEqual(self.native.package, GAME)
            return 0
        with patch("tools.run_task._run_pipeline", side_effect=submit) as pipeline:
            self.assertEqual(runtime.execute(TASKS[0].entry), 0)
        pipeline.assert_called_once()

    def test_cancel_manual_login_before_submission_returns_130(self):
        self.native = Native(QQ, [(QQ, 1)])
        self.clock.on_sleep = self.stop.set
        runtime = TaskRuntime()
        runtime.tasker = Mock(stopping=False)
        runtime.controller = SimpleNamespace(post_screencap=lambda: SimpleNamespace(
            wait=lambda: SimpleNamespace(succeeded=self.guard.capture(self.native) is not None)))
        self.assertEqual(runtime.execute(TASKS[0].entry, stop_event=self.stop), 130)
        runtime.tasker.post_task.assert_not_called()
        self.assertEqual(self.native.captures, 1)

    def test_cancellation_at_restore_boundary_never_returns_frame(self):
        def save(enabled):
            if enabled:
                self.stop.set()
            return True
        self.save.side_effect = save
        self.assertIsNone(self.guard.capture(self.native))
        self.assertEqual(self.save.call_args.args, (False,))

    def test_home_p1_p2_p3_frames_unchanged(self):
        for value in (2, 3, 4):
            with self.subTest(home=value):
                self.native = Native(GAME, [(GAME, value)])
                frame = self.guard.capture(self.native)
                self.assertIs(frame, self.native.cached_image)
                self.assertEqual(self.native.captures, 1)
        self.wait.assert_not_called()

    def test_unknown_foreground_blocks_capture_and_input(self):
        self.native.package = None
        self.assertIsNone(self.guard.capture(self.native))
        self.assertFalse(self.guard.allow_input("post_click"))
        self.assertEqual(self.native.captures, 0)
        self.assertNotIn(True, [c.args[0] for c in self.save.call_args_list])

    def test_other_app_is_not_a_trusted_launcher(self):
        self.native.package = "other.app"
        self.assertIsNone(self.guard.capture(self.native))
        self.assertFalse(self.guard.allow_input("post_start_app"))
        self.assertFalse(self.guard.allow_input("post_click"))
        self.assertEqual(self.native.captures, 0)

    def test_known_launcher_keeps_start_app_but_blocks_business_clicks(self):
        self.native = Native("app.launcher", [("app.launcher", 0)])
        self.assertIsNotNone(self.guard.capture(self.native))
        self.assertTrue(self.guard.allow_input("post_start_app"))
        self.assertFalse(self.guard.allow_input("post_click"))

    def test_game_login_text_uses_existing_manual_wait(self):
        self.native = Native(GAME, [(GAME, 5), (GAME, 2)])
        self.assertEqual(int(self.guard.capture(self.native)[0, 0, 0]), 2)
        self.wait.assert_called_once()

    def test_partial_login_evidence_blocks_input_without_waiting(self):
        self.evidence.return_value = None
        self.evidence.side_effect = None
        self.assertFalse(self.guard.allow_input("post_click", self.native))
        self.wait.assert_not_called()

    def test_failed_ocr_never_authorizes_input_or_releases_frame(self):
        self.evidence.side_effect = RuntimeError("private-text-must-not-be-logged")
        self.assertFalse(self.guard.allow_input("post_click", self.native))
        self.assertIsNone(self.guard.capture(self.native))
        self.assertNotIn("private-text-must-not-be-logged", self.output.getvalue())

    def test_foreground_change_during_ocr_never_authorizes_input(self):
        def recognize(*args):
            self.native.package = QQ
            return False
        self.evidence.side_effect = recognize
        self.assertFalse(self.guard.allow_input("post_click", self.native))

    def test_login_appearing_before_click_blocks_stale_coordinates(self):
        self.native = Native(GAME, [(GAME, 5), (GAME, 2)])
        self.assertFalse(self.guard.allow_input("post_click", self.native))
        self.assertEqual(self.native.captures, 1)
        self.wait.assert_not_called()

    def test_stop_during_preclick_check_does_not_authorize_input(self):
        self.native.on_capture = self.stop.set
        self.assertFalse(self.guard.allow_input("post_click", self.native))
        self.assertEqual(self.native.captures, 1)

    def test_normal_input_checks_only_in_memory_and_keeps_frame(self):
        self.assertTrue(self.guard.allow_input("post_click", self.native))
        self.assertEqual(self.native.captures, 1)
        self.wait.assert_not_called()

    def test_foreground_changes_during_capture_private_frame_not_returned(self):
        self.native = Native(GAME, [(QQ, 1), (GAME, 4)])
        self.assertEqual(int(self.guard.capture(self.native)[0, 0, 0]), 4)
        self.wait.assert_called_once()

    def test_package_query_failure_is_silent_and_closed(self):
        self.focus.side_effect = RuntimeError("sensitive-account")
        self.assertIsNone(self.guard.capture(self.native))
        self.assertNotIn("sensitive-account", self.output.getvalue())
        self.assertEqual(self.native.captures, 0)

    def test_cannot_disable_error_capture_means_no_screenshot(self):
        self.save.return_value = False
        self.assertIsNone(self.guard.capture(self.native))
        self.assertEqual(self.native.captures, 0)

    def test_stop_before_wait_never_captures(self):
        self.stop.set()
        self.assertIsNone(self.wait(self.native, timeout_seconds=300, interval_seconds=1,
                                    cancelled=self.stop.is_set))
        self.assertEqual(self.native.captures, 0)
        self.load.assert_not_called()

    def test_stop_after_capture_never_reads_next_frame(self):
        self.native = Native(QQ, [(QQ, 1), (GAME, 2)])
        self.native.on_capture = self.stop.set
        self.assertIsNone(self.guard.capture(self.native))
        self.assertEqual(self.native.captures, 1)
        self.assertFalse(self.guard.allow_input("post_click"))
        self.assertNotIn(True, [c.args[0] for c in self.save.call_args_list])

    def test_stop_during_poll_sleep_is_prompt_and_never_captures_again(self):
        self.native = Native(WECHAT, [(WECHAT, 1)])
        self.clock.on_sleep = self.stop.set
        self.assertIsNone(self.guard.capture(self.native))
        self.assertLessEqual(self.clock.now, .05)
        self.assertEqual(self.native.captures, 1)
        self.assertFalse(self.guard.allow_input("post_click"))

    def test_timeout_still_300_seconds_and_never_restores_error_captures(self):
        self.native = Native(QQ, [(QQ, 1)])
        self.assertIsNone(self.guard.capture(self.native))
        self.assertAlmostEqual(self.clock.now, 300)
        self.assertNotIn(True, [c.args[0] for c in self.save.call_args_list])

    def test_custom_action_stop_returns_failure_not_completion(self):
        context = SimpleNamespace(tasker=SimpleNamespace(controller=self.native, stopping=False))
        def cancel(**kwargs):
            context.tasker.stopping = True
        self.clock.on_sleep = cancel
        self.native.package = QQ
        self.assertFalse(privacy.PrivateLoginWaitAction().run(context, SimpleNamespace(custom_action_param="{}")))
        self.assertEqual(self.native.captures, 1)
        self.assertNotIn(True, [c.args[0] for c in self.save.call_args_list])

    def test_stopped_custom_action_never_captures(self):
        context = SimpleNamespace(tasker=SimpleNamespace(controller=self.native, stopping=True))
        self.assertFalse(privacy.PrivateLoginWaitAction().run(context, SimpleNamespace(custom_action_param="{}")))
        self.assertEqual(self.native.captures, 0)
        self.assertEqual(self.save.call_args.args, (False,))

    def test_stop_during_match_never_reports_safe_completion(self):
        def match(*args):
            self.stop.set()
            return True
        with patch.object(privacy, "_matches", side_effect=match):
            self.assertIsNone(self.wait(self.native, timeout_seconds=300, interval_seconds=1,
                                        cancelled=self.stop.is_set))
        self.assertEqual(self.native.captures, 1)

    def test_stop_between_preflight_and_business_returns_cancelled(self):
        names = []
        def post(name):
            names.append(name)
            self.stop.set()
            return SimpleNamespace(done=True)
        tasker = SimpleNamespace(post_task=post, post_stop=lambda: SimpleNamespace(wait=lambda: None))
        self.assertEqual(_run_pipeline(tasker, TASKS[0].entry, stop_event=self.stop), 130)
        self.assertEqual(names, ["QJNN_CommonPopupPreflight"])

    def test_queue_recovery_uses_same_guarded_controller(self):
        self.native = Native(QQ, [(QQ, 1), (GAME, 2)])
        runtime = TaskRuntime()
        recovery = SimpleNamespace(enabled=True, queue_recovery_blocked=False)
        runtime.resource = SimpleNamespace(_back_to_home_runtime=recovery)
        runtime.runtime_controller = SimpleNamespace(package_name=GAME)
        def post(name, **kwargs):
            frame = self.guard.capture(self.native)
            self.assertIsNotNone(frame)
            self.assertEqual(self.native.package, GAME)
            result = SimpleNamespace(status=SimpleNamespace(succeeded=True),
                                     nodes=[SimpleNamespace(name=name, completed=True)])
            return SimpleNamespace(get=lambda wait=False: result)
        runtime.tasker = SimpleNamespace(post_task=post)
        runtime.controller = SimpleNamespace(post_screencap=lambda: SimpleNamespace(
            wait=lambda: SimpleNamespace(succeeded=self.guard.capture(self.native) is not None)))
        self.assertTrue(runtime.recover_after_failure())

    def test_real_maa_controller_boundary_returns_only_safe_frame(self):
        from maa.controller import Controller
        self.native = Native(QQ, [(QQ, 1), (GAME, 2)])
        self.native.set_screenshot_use_raw_size = lambda _: True
        self.native.connected = True
        self.native.uuid = "offline-controller"
        self.native.post_connection = lambda: SimpleNamespace(
            wait=lambda: SimpleNamespace(succeeded=True))
        self.native.post_click = Mock()
        adb = SimpleNamespace(adb_path="fake-adb", serial="offline-device",
                              profile=SimpleNamespace(name="General"))
        adapter = RecoveringController(adb, native_factory=lambda *args: self.native)
        # Use the actual native callback/coordinate boundary, with no ADB.
        adapter.privacy_guard = self.guard
        controller = Controller(handle=adapter._handle)
        self.assertTrue(controller.post_connection().wait().succeeded)
        self.assertTrue(controller.post_screencap().wait().succeeded)
        self.assertTrue(np.all(controller.cached_image == 2))
        self.native.package = WECHAT
        self.assertFalse(controller.post_click(10, 20).wait().succeeded)
        self.native.post_click.assert_not_called()

    def test_technical_capture_failure_keeps_existing_recovery_path(self):
        self.native.post_screencap = lambda: SimpleNamespace(
            wait=lambda: SimpleNamespace(succeeded=False))
        with self.assertRaisesRegex(RuntimeError, "native screenshot failed"):
            self.guard.capture(self.native)

    def test_runtime_installs_guard_before_binding_and_uses_stop_event(self):
        controller = Mock()
        resource = Mock(loaded=True)
        tasker = Mock(inited=True, stopping=False)
        adb = SimpleNamespace(serial="offline-device", package_name=GAME)
        with ExitStack() as stack:
            stack.enter_context(patch("tools.run_task.create_runtime_controller", return_value=adb))
            stack.enter_context(patch("maa_recovering_controller.RecoveringController", return_value=controller))
            stack.enter_context(patch("maa.resource.Resource", return_value=resource))
            stack.enter_context(patch("maa.tasker.Tasker", return_value=tasker))
            stack.enter_context(patch("tools.run_task.register_local_extensions", return_value=True))
            tasker.bind.side_effect = lambda *args: self.assertIsInstance(
                controller.privacy_guard, privacy.LoginPrivacyGuard) or True
            with TaskRuntime(stop_event=self.stop):
                self.assertFalse(controller.privacy_guard.cancelled())
                self.stop.set()
                self.assertTrue(controller.privacy_guard.cancelled())
            tasker.post_stop.assert_called_once()

    def test_denied_diagnostic_capture_never_encodes_cached_image(self):
        actions = {}
        resource = SimpleNamespace(
            register_custom_action=lambda name, action: actions.setdefault(name, action) is not None,
            register_custom_recognition=lambda *args: True,
        )
        self.assertTrue(register_local_extensions(resource))
        controller = Mock()
        controller.post_screencap.return_value.wait.return_value.succeeded = False
        context = SimpleNamespace(tasker=SimpleNamespace(controller=controller))
        with patch("cv2.imencode") as encode:
            self.assertFalse(actions["qjnn_save_signin_failure"].run(context, None))
            self.assertFalse(actions["qjnn_save_mail_failure"].run(context, None))
        encode.assert_not_called()


class ControllerPrivacyBoundaryTests(unittest.TestCase):
    def test_policy_denial_never_reconnects_or_submits_click(self):
        adapter = RecoveringController(Mock())
        adapter._native = Mock()
        adapter.privacy_guard = Mock()
        adapter.privacy_guard.allow_input.return_value = False
        adapter.privacy_guard.capture.return_value = None
        self.assertFalse(adapter.click(10, 20))
        self.assertEqual(adapter.screencap().size, 0)
        adapter.adb.recover_after_controller_failure.assert_not_called()
        adapter._native.post_click.assert_not_called()
        adapter._native.post_screencap.assert_not_called()

    def test_native_click_failure_latches_without_retry_or_more_input(self):
        adapter = RecoveringController(Mock())
        adapter._native = Mock()
        adapter._native.post_click.return_value.wait.return_value.succeeded = False
        adapter.privacy_guard = Mock()
        adapter.privacy_guard.allow_input.side_effect = [True, False]
        with patch.object(adapter, "_connect_native"):
            self.assertFalse(adapter.click(10, 20))
        self.assertEqual(adapter._native.post_click.call_count, 1)
        self.assertTrue(adapter.input_uncertain)
        self.assertFalse(adapter.click(10, 20))
        self.assertEqual(adapter._native.post_click.call_count, 1)
        self.assertEqual(adapter.privacy_guard.allow_input.call_count, 1)
        adapter.adb.recover_after_controller_failure.assert_not_called()


class LauncherResolutionTests(unittest.TestCase):
    def test_exact_home_component_is_resolved_without_screenshot(self):
        controller = SimpleNamespace(info={"adb_path": "fake-adb", "adb_serial": "fake-device"})
        runner = Mock(return_value=SimpleNamespace(returncode=0, stdout="priority=0\napp.launcher/.Home\n"))
        self.assertEqual(launcher_package(controller, process_runner=runner), "app.launcher")
        self.assertIn("android.intent.category.HOME", runner.call_args.args[0])

    def test_unresolved_home_app_fails_closed(self):
        controller = SimpleNamespace(info={"adb_path": "fake-adb", "adb_serial": "fake-device"})
        runner = Mock(return_value=SimpleNamespace(returncode=0, stdout="No activity found"))
        self.assertIsNone(launcher_package(controller, process_runner=runner))

    def test_system_resolver_is_not_a_confirmed_launcher(self):
        controller = SimpleNamespace(info={"adb_path": "fake-adb", "adb_serial": "fake-device"})
        runner = Mock(return_value=SimpleNamespace(returncode=0, stdout="android/com.android.internal.app.ResolverActivity"))
        self.assertIsNone(launcher_package(controller, process_runner=runner))


if __name__ == "__main__":
    unittest.main()
