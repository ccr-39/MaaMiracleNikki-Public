"""Explicit offline packaging check: no controller, ADB or task execution."""
import importlib
import json
from app_paths import RESOURCE_ROOT, DATA_ROOT

def verify(window):
    from gui.task_registry import TASKS, validate_registry
    validate_registry()
    assert len(TASKS) == 14
    for name in ("det.onnx", "rec.onnx", "keys.txt"):
        assert (RESOURCE_ROOT / "assets/resource/model/ocr" / name).is_file(), name
    for path in (RESOURCE_ROOT / "assets/resource/pipeline").glob("*.json"):
        json.loads(path.read_text(encoding="utf-8"))
    from tools.run_task import validate_custom_action_runtime_coverage, validate_custom_recognition_runtime_coverage
    validate_custom_action_runtime_coverage()
    validate_custom_recognition_runtime_coverage()
    # Load every agent module, including dynamically imported runtime callbacks.
    import sys
    sys.path.insert(0, str(RESOURCE_ROOT / "agent"))
    for path in (RESOURCE_ROOT / "agent").glob("*.py"):
        if path.stem not in {"main", "qjnn_action", "qjnn_recognition"}:
            importlib.import_module(path.stem)
    from maa.resource import Resource
    from maa.tasker import Tasker
    Tasker.set_log_dir(DATA_ROOT / "logs/framework")
    resource = Resource()
    assert resource.post_bundle(RESOURCE_ROOT / "assets/resource").wait().succeeded
    # Exercise the new OCR-only login boundary in the frozen EXE as well as
    # source tests: a blank in-memory frame, no controller and no device.
    import numpy as np
    from qjnn_login import LoginTextReader, login_screen_evidence
    from qjnn_privacy import TEMPLATE_ROOT
    assert TEMPLATE_ROOT == RESOURCE_ROOT / "assets/resource/image"
    assert Tasker.set_save_on_error(False)
    assert login_screen_evidence(np.zeros((1280, 720, 3), dtype=np.uint8), LoginTextReader()) is False
    report = {"ok": True, "tasks": len(TASKS), "resource_root": str(RESOURCE_ROOT), "data_root": str(DATA_ROOT), "gui_created": window.isVisible(), "login_ocr_ready": True}
    (DATA_ROOT / "logs/offline-check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
