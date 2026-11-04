"""Exercise the real filesystem transaction in temp roots, never real releases."""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import windows_package as pkg


def release(path, version):
    (path / "_internal").mkdir(parents=True)
    (path / pkg.EXE).write_text(version)
    (path / "_internal/dependency.dll").write_text(version)
    return path


def version(path):
    return (path / pkg.EXE).read_text()


def verifier(path, report):
    assert version(path) == (path / "_internal/dependency.dll").read_text()
    pkg.write_json(report, {"ok": True})


def engine(tmp_path, **kwargs):
    return pkg.Deployment(tmp_path, kwargs.pop("verify", verifier), stopped=kwargs.pop("stopped", lambda: None), **kwargs)


def candidate(deployment, name="new"):
    stage = deployment.stage()
    release(stage / "dist" / pkg.NAME, name)
    return stage


def test_repeat_updates_one_fixed_directory_with_complete_dependencies(tmp_path):
    d = engine(tmp_path)
    release(d.target, "old")
    d.promote(candidate(d, "v1"))
    assert version(d.target) == "v1" and version(d.backup) == "old"
    d.promote(candidate(d, "v2"))
    assert version(d.target) == "v2" and version(d.backup) == "v1"
    assert list((tmp_path / "dist").iterdir()) == [d.target]
    assert not list(d.build.glob(".windows-package-*"))
    assert not d.journal.exists()


@pytest.mark.parametrize("fail", [False, True])
def test_portable_user_data_survives_deployment_and_rollback(tmp_path, fail):
    def hook(phase):
        if fail and phase == "after_install":
            raise PermissionError("offline rollback injection")
    d = engine(tmp_path, hook=hook)
    release(d.target, "old")
    user = d.target / "userdata/config/gui.json"
    user.parent.mkdir(parents=True)
    user.write_bytes(b'{"competition_votes": 7}')
    if fail:
        with pytest.raises(PermissionError):
            d.promote(candidate(d))
        assert version(d.target) == "old"
    else:
        d.promote(candidate(d))
        assert (d.backup / "userdata/config/gui.json").read_bytes() == user.read_bytes()
    assert user.read_bytes() == b'{"competition_votes": 7}'


@pytest.mark.parametrize("phase", ["before_backup", "after_backup", "after_install", "after_verify"])
def test_failure_at_each_rename_boundary_restores_old_and_prior_backup(tmp_path, phase):
    def fail(value):
        if value == phase:
            raise PermissionError("simulated Windows file lock/permission failure")

    d = engine(tmp_path)
    release(d.target, "old")
    d.promote(candidate(d, "current"))
    d.hook = fail
    with pytest.raises(PermissionError):
        d.promote(candidate(d, "bad"))
    assert version(d.target) == "current" and version(d.backup) == "old"
    assert not d.journal.exists()
    assert not list(d.build.glob(".windows-package-*"))


def test_final_verification_failure_automatically_rolls_back(tmp_path):
    def verify(path, report):
        verifier(path, report)
        if path == d.target and version(path) == "new":
            raise RuntimeError("simulated GUI/native load failure after install")

    d = engine(tmp_path, verify=verify)
    release(d.target, "old")
    with pytest.raises(RuntimeError, match="native load failure"):
        d.promote(candidate(d))
    assert version(d.target) == "old"
    assert not d.journal.exists()


def test_actual_candidate_rename_failure_restores_backup(tmp_path):
    d = engine(tmp_path)
    release(d.target, "old")
    stage = candidate(d)
    move = d._move

    def fail_move(source, destination):
        if source == stage / "dist" / pkg.NAME:
            raise PermissionError("candidate rename denied")
        move(source, destination)

    d._move = fail_move
    with pytest.raises(PermissionError):
        d.promote(stage)
    assert version(d.target) == "old"


def test_running_application_is_reported_without_killing_or_modifying(tmp_path):
    stopped = Mock(side_effect=RuntimeError("Please close MaaMiracleNikki"))
    d = engine(tmp_path, stopped=stopped)
    release(d.target, "old")
    with pytest.raises(RuntimeError, match="Please close"):
        d.promote(candidate(d))
    assert version(d.target) == "old" and not d.backup.exists()


def test_process_started_during_build_blocks_replacement(tmp_path):
    stopped = Mock(side_effect=[None, RuntimeError("Please close MaaMiracleNikki")])
    d = engine(tmp_path, stopped=stopped)
    release(d.target, "old")
    with pytest.raises(RuntimeError, match="Please close"):
        d.promote(candidate(d))
    assert version(d.target) == "old"


def test_interrupted_transaction_is_recovered_on_next_invocation(tmp_path):
    d = engine(tmp_path)
    release(d.target, "old")
    stage = candidate(d)
    state = {"id": "abrupt-interruption", "stage": str(stage), "had_target": True, "validated": False}
    pkg.write_json(stage / "dist" / pkg.NAME / "_internal" / pkg.INFO, {"transaction_id": state["id"]})
    pkg.write_json(d.journal, state)
    d._move(d.target, d.backup)
    d._move(stage / "dist" / pkg.NAME, d.target)
    d.recover()
    assert version(d.target) == "old"
    assert not d.journal.exists() and not stage.exists()


def test_failed_rollback_keeps_journal_backup_and_stage_for_retry(tmp_path):
    def fail(phase):
        if phase == "after_install":
            raise RuntimeError("post-install failure")

    d = engine(tmp_path, hook=fail)
    release(d.target, "old")
    stage = candidate(d)
    move = d._move

    def block_rollback(source, destination):
        if source == d.target and destination.name == "failed-release":
            raise PermissionError("EXE was opened during rollback")
        return move(source, destination)

    d._move = block_rollback
    with pytest.raises(RuntimeError, match="rollback needs attention"):
        d.promote(stage)
    assert stage.exists() and d.journal.exists() and version(d.backup) == "old"
    d._move = move
    d.recover()
    assert version(d.target) == "old" and not stage.exists()


def test_unrecognized_or_modified_backup_is_not_deleted(tmp_path):
    d = engine(tmp_path)
    release(d.target, "old")
    d.promote(candidate(d, "current"))
    note = d.backup / "important.txt"
    note.write_text("unknown user file")
    with pytest.raises(RuntimeError, match="unknown/modified"):
        d.promote(candidate(d, "next"))
    assert version(d.target) == "current" and note.read_text() == "unknown user file"


def test_unknown_files_present_before_first_backup_are_also_preserved(tmp_path):
    d = engine(tmp_path)
    release(d.target, "old")
    (d.target / "personal-notes.txt").write_text("keep")
    d.promote(candidate(d, "current"))
    with pytest.raises(RuntimeError, match="unknown/modified"):
        d.promote(candidate(d, "next"))
    assert (d.backup / "personal-notes.txt").read_text() == "keep"


def test_destructive_paths_outside_workspace_or_owner_are_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="Unsafe"):
        pkg.remove_tree(tmp_path, tmp_path)
    with pytest.raises(RuntimeError, match="Unsafe"):
        pkg.remove_tree(tmp_path.parent / "unrelated-project", tmp_path)
    d = engine(tmp_path)
    pkg.write_json(d.journal, {"stage": str(tmp_path / "source"), "id": "x"})
    with pytest.raises(RuntimeError, match="Unsafe package path|Invalid transaction"):
        d.recover()


def test_user_configuration_logs_state_and_cache_are_untouched(tmp_path):
    user = tmp_path / "LocalAppData/MaaMiracleNikki"
    for name in ("config/gui.json", "logs/run.log", "state/last-queue.json", "cache/data", "config/migration-backups/original.json"):
        path = user / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("user-owned data")
    before = pkg.release_files(user)
    d = engine(tmp_path / "project")
    release(d.target, "old")
    d.promote(candidate(d))
    assert pkg.release_files(user) == before


def test_build_failure_keeps_old_release_and_cleans_only_own_stage(tmp_path):
    target = release(tmp_path / "dist" / pkg.NAME, "old")
    unrelated = tmp_path / "build/other-project-data"
    unrelated.mkdir(parents=True)
    (unrelated / "keep").write_text("keep")

    def fail_build(root, stage, install):
        (stage / "partial-build").write_text("incomplete")
        raise RuntimeError("PyInstaller failed")

    with patch.object(pkg, "source_fingerprint", return_value={}):
        with pytest.raises(RuntimeError, match="PyInstaller failed"):
            pkg.run(tmp_path, builder=fail_build, verify=verifier, stopped=lambda: None)
    assert version(target) == "old" and (unrelated / "keep").exists()
    assert not list((tmp_path / "build").glob(".windows-package-*"))


def test_default_build_pipeline_and_install_option_are_preserved(tmp_path):
    calls = []

    def build(root, stage, install):
        calls.append(install)
        release(stage / "dist" / pkg.NAME, str(len(calls)))

    with patch.object(pkg, "source_fingerprint", return_value={}), patch.object(pkg, "validate_layout"), patch.object(pkg, "validate_sources"), patch.object(pkg, "prepare_update_manifest"):
        for install in (False, True):
            pkg.run(tmp_path, builder=build, install_dependencies=install, verify=verifier, stopped=lambda: None)
    assert calls == [False, True]
    assert version(tmp_path / "dist" / pkg.NAME) == "2"


def test_pyinstaller_arguments_always_use_private_stage_not_live_dist(tmp_path):
    stage = tmp_path / "build/.windows-package-test"
    (tmp_path / "build").mkdir()
    with patch.object(pkg.subprocess, "run", return_value=Mock(returncode=0)) as command:
        pkg.build_candidate(tmp_path, stage, True)
    assert command.call_args_list[0].args[0][:3] == [sys.executable, "-m", "pip"]
    args = command.call_args_list[1].args[0]
    assert args[args.index("--distpath") + 1] == str(stage / "dist")
    assert args[args.index("--workpath") + 1] == str(stage / "work")
    assert args[-1] == str(tmp_path / "MaaMiracleNikki.spec")


def test_concurrent_packaging_is_refused(tmp_path):
    with pkg.build_lock(tmp_path / "build"):
        with pytest.raises(RuntimeError, match="Another packaging"):
            with pkg.build_lock(tmp_path / "build"):
                pytest.fail("must not enter concurrent operation")


def test_launcher_script_forwards_compatible_arguments(tmp_path):
    script = Path(pkg.ROOT / "tools/build_windows.ps1")
    # A fake Python executable records wrapper arguments, without building.
    fake = tmp_path / "fake-python.cmd"
    output = tmp_path / "arguments.txt"
    fake.write_text('@echo off\r\necho %* > "' + str(output) + '"\r\nexit /b 0\r\n')
    result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                             "-Python", str(fake), "-InstallDependencies"], capture_output=True,
                            env=pkg.windows_powershell_env(), timeout=30)
    assert result.returncode == 0
    args = output.read_text()
    assert "windows_package.py" in args and "--install-dependencies" in args


def test_frozen_launcher_uses_temp_as_child_process_working_directory(tmp_path):
    data = tmp_path / "persistent-user-data"
    data.mkdir()
    program = '''
import runpy, sys, types
from pathlib import Path
data, launcher = map(Path, sys.argv[1:])
gui = types.ModuleType("gui.main")
def main():
    import tempfile
    assert Path.cwd() == Path(tempfile.gettempdir())
    return 0
gui.main = main
sys.modules["gui.main"] = gui
paths = types.ModuleType("app_paths")
paths.DATA_ROOT = data
sys.modules["app_paths"] = paths
runpy.run_path(str(launcher), run_name="__main__")
'''
    subprocess.run([sys.executable, "-c", program, str(data), str(pkg.ROOT / "packaging/launcher.py")],
                   cwd=tmp_path, check=True)


def test_running_process_guard_does_not_attempt_termination(tmp_path):
    with patch.object(pkg, "running_instances", return_value=[{"ProcessId": 123}]), patch.object(pkg.subprocess, "run") as execute:
        with pytest.raises(RuntimeError, match="Please close MaaMiracleNikki"):
            pkg.ensure_stopped(tmp_path)
        execute.assert_not_called()


def test_actual_windows_working_directory_lock_is_detected_read_only(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
                               cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        assert process.stdout.readline().strip() == b"ready"
        with patch.object(pkg, "running_instances", return_value=[]):
            with pytest.raises(RuntimeError, match="occupied or not writable"):
                pkg.ensure_stopped(tmp_path)
    finally:
        process.stdin.close()  # Graceful exit of our test child, never a user process.
        process.wait(timeout=5)
        process.stdout.close()
    with patch.object(pkg, "running_instances", return_value=[]):
        pkg.ensure_stopped(tmp_path)
