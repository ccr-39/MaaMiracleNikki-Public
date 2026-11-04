from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = ROOT / "tools"
AGENT_DIR = ROOT / "agent"
for path in (TOOLS_DIR, AGENT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from run_task import (  # noqa: E402
    RESOURCE_DIR,
    RUNTIME_CUSTOM_ACTION_NAMES,
    RUNTIME_CUSTOM_RECOGNITION_NAMES,
    pipeline_custom_action_names,
    pipeline_custom_recognition_names,
    register_local_extensions,
    validate_custom_action_runtime_coverage,
    validate_custom_recognition_runtime_coverage,
)
from test_ocr import load_image  # noqa: E402
from offline_test_support import require_file


CLAIMED_SAMPLE = ROOT / "images" / "debug" / "pipeline" / "signin_failure_004.png"
FRIEND_NO_TARGET_SAMPLE = (
    ROOT / "images" / "screenshots" / "friend" / "friend_send_heart_no_target_real_001.png"
)


class RegistryProbe:
    def __init__(self):
        self.recognition_names: set[str] = set()
        self.action_names: set[str] = set()

    def register_custom_recognition(self, name, instance):
        self.recognition_names.add(name)
        return instance is not None

    def register_custom_action(self, name, instance):
        self.action_names.add(name)
        return instance is not None


class CustomRecognitionRegistrationTests(unittest.TestCase):
    def test_pipeline_references_are_covered_by_formal_runtime_registry(self):
        referenced, declared = validate_custom_recognition_runtime_coverage()
        self.assertEqual(referenced, pipeline_custom_recognition_names())
        self.assertFalse(referenced - declared)

    def test_formal_registration_function_registers_every_declared_name(self):
        probe = RegistryProbe()
        self.assertTrue(register_local_extensions(probe))
        self.assertEqual(
            probe.recognition_names,
            set(RUNTIME_CUSTOM_RECOGNITION_NAMES),
        )
        self.assertEqual(probe.action_names, set(RUNTIME_CUSTOM_ACTION_NAMES))

    def test_custom_action_pipeline_references_are_covered(self):
        referenced, declared = validate_custom_action_runtime_coverage()
        self.assertEqual(referenced, pipeline_custom_action_names())
        self.assertFalse(referenced - declared)

    def test_missing_runtime_name_fails_before_task_submission(self):
        with patch(
            "run_task.pipeline_custom_recognition_names",
            return_value=frozenset({"qjnn_missing_test_recognition"}),
        ):
            with self.assertRaisesRegex(RuntimeError, "qjnn_missing_test_recognition"):
                validate_custom_recognition_runtime_coverage()

    def test_missing_custom_action_fails_before_task_submission(self):
        with patch(
            "run_task.pipeline_custom_action_names",
            return_value=frozenset({"qjnn_missing_test_action"}),
        ):
            with self.assertRaisesRegex(RuntimeError, "qjnn_missing_test_action"):
                validate_custom_action_runtime_coverage()

    def test_maa_runtime_resolves_signin_custom_recognition(self):
        require_file(CLAIMED_SAMPLE)
        if not CLAIMED_SAMPLE.is_file():
            self.skipTest(
                "Private device capture images/debug/pipeline/signin_failure_004.png "
                "is unavailable; images/debug/ is intentionally excluded from Git."
            )

        from maa.library import Library
        from maa.pipeline import JCustomRecognition, JRecognitionType
        from maa.resource import Resource
        from maa.tasker import Tasker

        resource = Resource()
        load_job = resource.post_bundle(RESOURCE_DIR).wait()
        self.assertTrue(load_job.succeeded)
        self.assertTrue(resource.loaded)
        self.assertTrue(register_local_extensions(resource))

        tasker = Tasker()
        tasker._resource_holder = resource
        bound = bool(
            Library.framework().MaaTaskerBindResource(
                tasker._handle,
                resource._handle,
            )
        )
        self.assertTrue(bound)
        self.assertTrue(tasker.inited)

        result = tasker.post_recognition(
            JRecognitionType.Custom,
            JCustomRecognition(
                custom_recognition="qjnn_signin_state",
                custom_recognition_param={"state": "CLAIMED"},
            ),
            load_image(CLAIMED_SAMPLE),
        ).get(wait=True)
        self.assertIsNotNone(result)
        self.assertTrue(result.status.succeeded)
        self.assertTrue(result.nodes)
        recognition = result.nodes[0].recognition
        self.assertIsNotNone(recognition)
        self.assertTrue(recognition.hit)
        self.assertIsNotNone(recognition.best_result)

    def test_maa_runtime_resolves_friend_result_custom_recognition(self):
        require_file(FRIEND_NO_TARGET_SAMPLE)
        from maa.library import Library
        from maa.pipeline import JCustomRecognition, JRecognitionType
        from maa.resource import Resource
        from maa.tasker import Tasker

        resource = Resource()
        load_job = resource.post_bundle(RESOURCE_DIR).wait()
        self.assertTrue(load_job.succeeded)
        self.assertTrue(register_local_extensions(resource))

        tasker = Tasker()
        tasker._resource_holder = resource
        self.assertTrue(
            bool(
                Library.framework().MaaTaskerBindResource(
                    tasker._handle,
                    resource._handle,
                )
            )
        )
        result = tasker.post_recognition(
            JRecognitionType.Custom,
            JCustomRecognition(
                custom_recognition="qjnn_friend_send_heart_result",
                custom_recognition_param={"kind": "NO_TARGET"},
            ),
            load_image(FRIEND_NO_TARGET_SAMPLE),
        ).get(wait=True)
        self.assertIsNotNone(result)
        self.assertTrue(result.status.succeeded)
        self.assertTrue(result.nodes)
        recognition = result.nodes[0].recognition
        self.assertIsNotNone(recognition)
        self.assertTrue(recognition.hit)
        self.assertEqual(recognition.best_result.detail["kind"], "NO_TARGET")


if __name__ == "__main__":
    unittest.main()
