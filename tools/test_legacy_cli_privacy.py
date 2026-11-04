"""Legacy CLI denial happens before device I/O; no real device or screenshots."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "agent")]


@pytest.fixture
def legacy_cli(monkeypatch):
    # Importing the real legacy recognition registry starts AgentServer mode.
    # It is unrelated to CLI dispatch and would pollute native integration tests.
    recognition = SimpleNamespace(
        ScreenDetection=object, ScreenState=object,
        TemplateMatchError=RuntimeError,
        detect_screen=Mock(side_effect=AssertionError("unguarded recognition")),
    )
    spec = spec_from_file_location("legacy_cli_under_test", ROOT / "agent/main.py")
    module = module_from_spec(spec)
    with patch.dict(sys.modules, {"qjnn_recognition": recognition}):
        spec.loader.exec_module(module)
    for name in ("run_start_pipeline", "run_capture", "capture_and_detect"):
        monkeypatch.setattr(module, name, Mock(side_effect=AssertionError(name)))
    return module


@pytest.mark.parametrize("foreground", [
    "com.tencent.mobileqq", "com.tencent.mm", None, "com.tencent.tmgp.qjnn",
])
@pytest.mark.parametrize("args", [[], ["--task", "start", "--select-device"]])
def test_legacy_start_rejects_before_any_foreground_capture(
    legacy_cli, monkeypatch, capsys, foreground, args,
):
    device = SimpleNamespace(package=foreground, screenshot=Mock(), click=Mock())
    factory = Mock(return_value=device)
    monkeypatch.setattr(legacy_cli, "create_controller", factory)
    monkeypatch.setattr(sys, "argv", ["agent/main.py", *args])
    assert legacy_cli.main() == 2
    factory.assert_not_called()
    device.screenshot.assert_not_called()
    device.click.assert_not_called()
    output = capsys.readouterr()
    assert output.out == ""
    assert "python tools/run_task.py QJNN_StartAndEnterHome" in output.err
    assert "com.tencent" not in output.err


@pytest.mark.parametrize("args", [
    ["--capture", "login"],
    ["--capture", "home"],
    ["--task", "daily", "--capture", "daily"],
])
def test_capture_cannot_bypass_disabled_start(legacy_cli, monkeypatch, args):
    factory = Mock(side_effect=AssertionError("device access"))
    monkeypatch.setattr(legacy_cli, "create_controller", factory)
    monkeypatch.setattr(sys, "argv", ["agent/main.py", *args])
    assert legacy_cli.main() == 2
    factory.assert_not_called()


def test_socket_dispatch_remains_host_integration_only(legacy_cli, monkeypatch):
    host = Mock(return_value=0)
    monkeypatch.setattr(legacy_cli, "run_agent", host)
    monkeypatch.setattr(sys, "argv", ["agent/main.py", "offline-socket"])
    assert legacy_cli.main() == 0
    host.assert_called_once_with("offline-socket")


def test_formal_cli_stopped_before_start_never_initializes_or_captures():
    from tools.run_task import run

    stop = Event()
    stop.set()
    with patch("tools.run_task.TaskRuntime", side_effect=AssertionError("runtime I/O")) as runtime:
        assert run("QJNN_StartAndEnterHome", force_select=False, init_only=False, stop_event=stop) == 130
    runtime.assert_not_called()
