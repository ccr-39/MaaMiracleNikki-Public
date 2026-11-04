from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from qjnn_foreground import (  # noqa: E402
    PackageForegroundRecognition,
    focused_package,
    parse_focused_package,
)
from qjnn_privacy import PrivateLoginForegroundRecognition  # noqa: E402
from check_project import check_pipeline_references  # noqa: E402


class FakeController:
    info = {
        "adb_path": r"D:\fake\adb.exe",
        "adb_serial": "127.0.0.1:16416",
    }


def completed(stdout: str, returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


class ForegroundParsingTests(unittest.TestCase):
    def test_android_15_top_resumed_activity(self):
        output = (
            "topResumedActivity=ActivityRecord{67614038 u0 "
            "app.lawnchair/.LawnchairLauncher t2}"
        )
        self.assertEqual(parse_focused_package(output), "app.lawnchair")

    def test_legacy_current_focus(self):
        output = (
            "mCurrentFocus=Window{123 u0 "
            "com.tencent.tmgp.qjnn/com.tencent.tmgp.qjnn.MainActivity}"
        )
        self.assertEqual(parse_focused_package(output), "com.tencent.tmgp.qjnn")

    def test_activity_query_falls_back_to_window(self):
        calls = []

        def runner(command, *, timeout):
            calls.append((command, timeout))
            if command[-2:] == ["activity", "activities"]:
                return completed("no resumed activity")
            return completed(
                "mFocusedApp=ActivityRecord{1 u0 "
                "com.tencent.tmgp.qjnn/.MainActivity t1}"
            )

        self.assertEqual(
            focused_package(FakeController(), process_runner=runner),
            "com.tencent.tmgp.qjnn",
        )
        self.assertEqual(len(calls), 2)


class ForegroundRecognitionTests(unittest.TestCase):
    @staticmethod
    def context():
        return SimpleNamespace(tasker=SimpleNamespace(controller=FakeController()))

    @staticmethod
    def argv(package: str = "com.tencent.tmgp.qjnn"):
        return SimpleNamespace(custom_recognition_param=json.dumps({"package": package}))

    def test_desktop_does_not_match_qjnn(self):
        recognition = PackageForegroundRecognition(lambda _: "app.lawnchair")
        result = recognition.analyze(self.context(), self.argv())
        self.assertIsNone(result.box)
        self.assertFalse(result.detail["foreground"])

    def test_unknown_foreground_fails_closed(self):
        recognition = PackageForegroundRecognition(lambda _: None)
        result = recognition.analyze(self.context(), self.argv())
        self.assertIsNone(result.box)
        self.assertFalse(result.detail["foreground"])

    def test_qjnn_matches_exact_package_only(self):
        recognition = PackageForegroundRecognition(
            lambda _: "com.tencent.tmgp.qjnn.fake"
        )
        self.assertIsNone(recognition.analyze(self.context(), self.argv()).box)

        recognition = PackageForegroundRecognition(lambda _: "com.tencent.tmgp.qjnn")
        self.assertIsNotNone(recognition.analyze(self.context(), self.argv()).box)

    def test_private_login_requires_known_package(self):
        desktop = PrivateLoginForegroundRecognition(lambda _: "app.lawnchair")
        qq = PrivateLoginForegroundRecognition(lambda _: "com.tencent.mobileqq")
        argv = SimpleNamespace(custom_recognition_param="")
        self.assertIsNone(desktop.analyze(self.context(), argv).box)
        self.assertIsNotNone(qq.analyze(self.context(), argv).box)


class StartupRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pipeline_path = ROOT / "assets" / "resource" / "pipeline" / "10_startup.json"
        cls.pipeline = json.loads(pipeline_path.read_text(encoding="utf-8"))

    def test_desktop_falls_through_to_existing_start_app(self):
        entry_next = self.pipeline["QJNN_StartAndEnterHome"]["next"]
        self.assertEqual(
            entry_next[-2:],
            ["QJNN_QjnnForeground", "QJNN_StartAppAndEnterHome"],
        )
        self.assertNotIn("QJNN_PrivateLoginWait", entry_next)
        self.assertEqual(
            self.pipeline["QJNN_StartAppAndEnterHome"]["action"],
            "StartApp",
        )
        self.assertEqual(
            self.pipeline["QJNN_StartAppAndEnterHome"]["next"],
            ["QJNN_WaitForGameState"],
        )

    def test_unknown_frames_do_not_fall_through_to_privacy_wait(self):
        for node_name in ("QJNN_WaitForGameState", "QJNN_StartAppAndEnterHome"):
            self.assertNotIn(
                "QJNN_PrivateLoginWait",
                self.pipeline[node_name]["next"],
                node_name,
            )

    def test_privacy_wait_has_explicit_predecessors(self):
        self.assertEqual(
            self.pipeline["QJNN_LoginScreenGate"]["next"],
            ["QJNN_LoginScreen"],
        )
        self.assertEqual(
            self.pipeline["QJNN_PrivateLoginForeground"]["next"],
            ["QJNN_PrivateLoginWait"],
        )

    def test_desktop_start_app_then_start_button_mock(self):
        end_to_end_path = (
            ROOT / "assets" / "resource" / "pipeline" / "40_start_and_signin.json"
        )
        end_to_end = json.loads(end_to_end_path.read_text(encoding="utf-8"))

        trace = ["QJNN_StartAndSignin"]
        entry_target = next(
            target
            for target in end_to_end["QJNN_StartAndSignin"]["next"]
            if isinstance(target, dict) and target.get("name") == "QJNN_StartAndEnterHome"
        )
        trace.append(entry_target["name"])

        # Desktop: all visual/foreground candidates miss, so the final existing
        # StartApp node is selected. No action is executed by this mock.
        trace.append(self.pipeline["QJNN_StartAndEnterHome"]["next"][-1])
        trace.extend(self.pipeline["QJNN_StartAppAndEnterHome"]["next"])

        # Next frame: the existing OCR/template-backed StartButton node hits.
        # Stop before executing its action.
        wait_candidates = self.pipeline["QJNN_WaitForGameState"]["next"]
        self.assertIn("QJNN_StartButton", wait_candidates)
        trace.append("QJNN_StartButton")

        self.assertEqual(
            trace,
            [
                "QJNN_StartAndSignin",
                "QJNN_StartAndEnterHome",
                "QJNN_StartAppAndEnterHome",
                "QJNN_WaitForGameState",
                "QJNN_StartButton",
            ],
        )
        self.assertNotIn("", trace)
        self.assertNotIn("QJNN_SafeStop", trace)
        self.assertNotIn("QJNN_PrivateLoginWait", trace)

    def test_start_button_returns_to_named_wait(self):
        self.assertEqual(
            self.pipeline["QJNN_StartButton"]["next"],
            ["QJNN_WaitForGameState"],
        )

    def test_home_stable_confirms_any_home_without_waiting_for_signin_text(self):
        gate=self.pipeline['QJNN_HomeScreenTemplateGate']
        stable=self.pipeline['QJNN_HomeStable']
        self.assertEqual(gate['custom_recognition'],'qjnn_home_any_page')
        self.assertEqual(gate['custom_recognition_param'],{'strict':True})
        self.assertEqual(gate['next'],['QJNN_HomeStable'])
        self.assertGreaterEqual(gate['post_delay'],400)
        self.assertEqual(stable['custom_recognition'],'qjnn_home_any_page')
        self.assertEqual(stable['custom_recognition_param'],{'strict':True})
        self.assertEqual(stable['action'],'DoNothing')
        self.assertNotIn('next',stable)
        self.assertNotIn('all_of',stable)

    def test_home_stable_timeout_stops_before_signin(self):
        timeout_node = self.pipeline["QJNN_HomeStableTimeout"]
        self.assertEqual(timeout_node["recognition"], "DirectHit")
        self.assertEqual(timeout_node["action"], "DoNothing")
        self.assertEqual(timeout_node["next"], ["QJNN_SafeStop"])
        self.assertEqual(
            timeout_node["focus"]["Node.Action.Starting"],
            "[Startup] Home base UI detected, but async home controls did not become ready",
        )

        trace = [
            "QJNN_EnterGameScreen",
            "QJNN_HomeScreen",
            "QJNN_HomeStableTimeout",
            "QJNN_SafeStop",
        ]
        self.assertNotIn("QJNN_HomeStable", trace)
        self.assertNotIn("QJNN_Signin", trace)
        self.assertNotIn("QJNN_SigninSkip", trace)

    def test_static_reference_check_rejects_invalid_targets(self):
        malformed = {
            "Node": {
                "next": ["", None, {}, "MissingNode"],
                "on_success": [],
            }
        }
        errors = []
        check_pipeline_references(
            malformed,
            {"Node"},
            ROOT / "assets" / "resource" / "pipeline" / "mock.json",
            errors,
        )
        self.assertEqual(len(errors), 5)


if __name__ == "__main__":
    unittest.main()
