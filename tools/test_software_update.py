"""Only fake GitHub metadata, inert fixture EXEs and temporary installations.

No HTTP, ADB, emulator, PyInstaller builds, or execution of release binaries.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from threading import Event
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request
import zipfile

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from updates import client, installer, package
from updates.client import Cancelled, UpdateClient, UpdateError, parse_release
from updates.version import Version, current_version
from tools.release_bundle import bundle
from tools.windows_package import EXE, NAME, sha


def make_release(path, version="1.0.0"):
    required = [EXE, package.UPDATER, "_internal/base_library.zip", "_internal/python313.dll",
                "_internal/PySide6/Qt6Core.dll", "_internal/PySide6/Qt6Gui.dll", "_internal/PySide6/Qt6Widgets.dll",
                "_internal/PySide6/plugins/platforms/qwindows.dll", "_internal/assets/resource/pipeline/test.json"]
    required += ["_internal/assets/resource/model/ocr/" + n for n in ("det.onnx", "rec.onnx", "keys.txt")]
    required += ["_internal/maa/bin/" + n for n in ("MaaFramework.dll", "MaaToolkit.dll", "MaaAgentClient.dll",
        "MaaAgentServer.dll", "MaaAdbControlUnit.dll", "MaaCustomControlUnit.dll", "MaaUtils.dll",
        "fastdeploy_ppocr_maa.dll", "onnxruntime_maa.dll", "opencv_world4_maa.dll", "DirectML.dll")]
    for name in required:
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"INERT TEST FIXTURE - NEVER EXECUTE " + version.encode())
    (path / "_internal/assets/interface.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    package.write_manifest(path, version)
    return path


@pytest.fixture
def release_zip(tmp_path):
    release = make_release(tmp_path / "source" / NAME)
    return bundle(release, tmp_path / "artifacts")


def metadata(path, version="1.0.0"):
    name = path.name
    return {"tag_name": "v" + version, "draft": False, "prerelease": False, "body": "中文更新说明\n修复问题",
            "assets": [{"name": n, "size": size,
                        "browser_download_url": client.PREFIX + "v" + version + "/" + n}
                       for n, size in [(name, path.stat().st_size), (name + ".sha256", 140)]]}


def test_canonical_public_release_zip_and_checksum(tmp_path):
    path = tmp_path / "MaaMiracleNikki-Windows-x64.zip"
    path.write_bytes(b"offline metadata fixture")
    release = parse_release(metadata(path))
    assert release.package.name == path.name
    assert release.checksum.name == path.name + ".sha256"
    assert "/MaaMiracleNikki-Public/" in release.package.url


class FakeTransport:
    def __init__(self, path, data=None):
        self.calls = []
        self.responses = {client.API: json.dumps(data or metadata(path)).encode(),
                          client.PREFIX + "v1.0.0/" + path.name: path.read_bytes(),
                          client.PREFIX + "v1.0.0/" + path.name + ".sha256":
                              f"{sha(path)}  {path.name}\n".encode()}

    def open(self, url):
        self.calls.append(url)
        value = self.responses[url]
        if isinstance(value, BaseException):
            raise value
        return io.BytesIO(value)


def test_semver_and_single_version_source():
    values = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2",
              "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.2.0", "1.10.0", "2.0.0"]
    assert sorted(map(Version, reversed(values))) == list(map(Version, values))
    assert Version("v1.0.0+abc") == Version("1.0.0+def")
    assert current_version() == json.loads((ROOT / "assets/interface.json").read_text(encoding="utf-8"))["version"]


@pytest.mark.parametrize("value", ["1.0", "01.2.3", "1.2.3-01", "1.2.3-a..b", "1.2.3+", "1.2.3/../../"])
def test_invalid_semver(value):
    with pytest.raises(ValueError):
        Version(value)


def test_latest_and_no_update(release_zip):
    transport = FakeTransport(release_zip)
    release = UpdateClient(transport).latest()
    assert Version(release.version) > Version(current_version())
    assert "中文" in release.notes
    old = metadata(release_zip, "0.0.1")
    old["assets"] = []
    transport.responses[client.API] = json.dumps(old).encode()
    assert Version(UpdateClient(transport).latest().version) < Version(current_version())


@pytest.mark.parametrize("flag", ["draft", "prerelease"])
def test_reject_unstable(release_zip, flag):
    data = metadata(release_zip)
    data[flag] = True
    with pytest.raises(UpdateError):
        parse_release(data)


def test_network_failure_and_no_token(release_zip):
    transport = FakeTransport(release_zip)
    transport.responses[client.API] = TimeoutError()
    with pytest.raises(UpdateError, match="超时"):
        UpdateClient(transport, retries=0).latest()
    opener = Mock()
    transport = client.Transport()
    transport.opener = opener
    transport.open(client.API)
    request = opener.open.call_args.args[0]
    assert not request.has_header("Authorization")
    assert opener.open.call_args.kwargs["timeout"] == 10


@pytest.mark.parametrize("url", ["http://github.com/ccr-39/MaaMiracleNikki/releases/download/a/b",
    "https://example.com/file", "https://github.com/other/repo/releases/download/v1/a.zip",
    "https://github.com@evil.com/file", "file:///tmp/x"])
def test_untrusted_downloads_and_redirects_are_rejected(url):
    with pytest.raises(UpdateError):
        client.Transport().open(url)
    with pytest.raises(UpdateError):
        client.RestrictedRedirect().redirect_request(Request(client.PREFIX + "v1/a.zip"), None, 302, "", {}, url)


def test_download_validated_offline(release_zip, tmp_path):
    transport = FakeTransport(release_zip)
    release = UpdateClient(transport).latest()
    progress = []
    path, digest = UpdateClient(transport).download(release, lambda done, total: progress.append((done, total)), directory=tmp_path)
    assert sha(path) == digest == sha(release_zip)
    assert progress[0][0] == 0 and progress[-1][0] == progress[-1][1]
    assert path.parent != release_zip.parent


def test_integrity_failure_cleans_partial_download(release_zip, tmp_path):
    transport = FakeTransport(release_zip)
    release = UpdateClient(transport).latest()
    transport.responses[release.checksum.url] = f"{'0' * 64}  {release.package.name}".encode()
    with pytest.raises(UpdateError, match="SHA256"):
        UpdateClient(transport).download(release, directory=tmp_path)
    assert not list(tmp_path.glob("mmn-download-*"))


def test_retry_interrupted_stream_and_cancel(release_zip, tmp_path):
    transport = FakeTransport(release_zip)
    release = UpdateClient(transport).latest()
    original = transport.open
    attempts = []
    def once(url):
        if url == release.package.url:
            attempts.append(url)
            if len(attempts) == 1:
                return io.BytesIO(release_zip.read_bytes()[:20])
        return original(url)
    transport.open = once
    path, digest = UpdateClient(transport, retries=1).download(release, directory=tmp_path)
    assert len(attempts) == 2 and sha(path) == digest
    shutil.rmtree(path.parent)
    cancel = Event()
    def progress(done, total):
        if done:
            cancel.set()
    with pytest.raises(Cancelled):
        UpdateClient(transport).download(release, progress, cancel, tmp_path)
    assert not list(tmp_path.glob("mmn-download-*"))


def test_reject_digest_mismatch_and_missing_assets(release_zip, tmp_path):
    data = metadata(release_zip)
    data["assets"][0]["digest"] = "sha256:" + "0" * 64
    release = parse_release(data)
    with pytest.raises(UpdateError, match="摘要"):
        UpdateClient(FakeTransport(release_zip)).download(release, directory=tmp_path)
    data["assets"] = []
    with pytest.raises(UpdateError, match="缺少"):
        UpdateClient(FakeTransport(release_zip)).download(parse_release(data), directory=tmp_path)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/escape", "MaaMiracleNikki/../escape",
    "MaaMiracleNikki/a\\b", "MaaMiracleNikki/CON.txt", "MaaMiracleNikki/file:stream",
    "MaaMiracleNikki/a. /b", "MaaMiracleNikki//b", "Other/file"])
def test_archive_traversal_rejected_before_extract(tmp_path, name):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, b"bad")
    with pytest.raises(UpdateError):
        package.extract_archive(path, tmp_path / "out", "1.0.0", Event())
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "escape").exists()


def test_archive_link_duplicate_and_manifest_mismatch(release_zip, tmp_path):
    path = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo(NAME + "/link")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(info, "../outside")
    with pytest.raises(UpdateError, match="链接"):
        package.inspect_archive(path, "1.0.0")
    with pytest.raises(UpdateError, match="版本"):
        package.inspect_archive(release_zip, "2.0.0")
    with zipfile.ZipFile(release_zip, "a") as archive:
        archive.writestr(NAME + "/maamiraclenikki.EXE", b"duplicate")
    with pytest.raises(UpdateError, match="重复"):
        package.inspect_archive(release_zip, "1.0.0")


def transaction(tmp_path, **kwargs):
    target = make_release(tmp_path / "installed" / NAME, "0.1.0")
    return installer.Transaction(target, tmp_path / "profile", stopped=lambda: None, **kwargs)


def installed_version(target):
    return json.loads((target / "_internal/assets/interface.json").read_text())["version"]


def test_success_full_directory_replacement_and_preservation(release_zip, tmp_path):
    t = transaction(tmp_path)
    (t.target / "old-dependency.dll").write_text("old")
    for name in ("config/gui.json", "logs/log.txt", "state/last-queue.json", "_internal/config/device.json"):
        p = t.target / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"USER DATA \x00")
    t.data_root.mkdir()
    (t.data_root / "profile.txt").write_text("keep")
    with t.lock():
        stage = t.install(release_zip, sha(release_zip), "1.0.0")
        t.commit()
    assert installed_version(t.target) == "1.0.0"
    assert installed_version(stage / "backup") == "0.1.0"
    assert not (t.target / "old-dependency.dll").exists()
    assert (stage / "backup/old-dependency.dll").read_text() == "old"
    for name in ("config/gui.json", "logs/log.txt", "state/last-queue.json", "_internal/config/device.json"):
        assert (t.target / name).read_bytes() == b"USER DATA \x00"
    assert (t.data_root / "profile.txt").read_text() == "keep"
    assert not t.journal.exists()


@pytest.mark.parametrize("phase", ["before_backup", "after_backup", "after_install", "after_verify"])
def test_rollback_at_every_boundary(release_zip, tmp_path, phase):
    def hook(current):
        if current == phase:
            raise PermissionError("test file occupied")
    t = transaction(tmp_path, hook=hook)
    (t.target / "config").mkdir()
    (t.target / "config/gui.json").write_bytes(b"profile")
    with pytest.raises(PermissionError):
        t.install(release_zip, sha(release_zip), "1.0.0")
    assert installed_version(t.target) == "0.1.0"
    assert (t.target / "config/gui.json").read_bytes() == b"profile"
    assert not t.journal.exists()


def test_rollback_failure_retains_recoverable_journal(release_zip, tmp_path):
    t = transaction(tmp_path)
    real_move = installer.move
    def fail(source, target):
        if source.name in ("candidate", "backup"):
            raise UpdateError("simulated lock")
        real_move(source, target)
    with patch.object(installer, "move", side_effect=fail), pytest.raises(UpdateError, match="回滚"):
        t.install(release_zip, sha(release_zip), "1.0.0")
    assert t.journal.exists()
    t.recover()
    assert installed_version(t.target) == "0.1.0" and not t.journal.exists()


def test_real_windows_lock_is_detected_without_stopping_test_child(release_zip, tmp_path):
    t = transaction(tmp_path)
    child = subprocess.Popen([sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
                             cwd=t.target, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        assert child.stdout.readline().strip() == b"ready"
        t.stopped = lambda: installer.ensure_stopped(t.target)
        with patch("tools.windows_package.running_instances", return_value=[]), pytest.raises(RuntimeError, match="occupied"):
            t.install(release_zip, sha(release_zip), "1.0.0")
        assert child.poll() is None and installed_version(t.target) == "0.1.0"
    finally:
        child.stdin.close()
        child.wait(timeout=5)
        child.stdout.close()


def test_reject_tampered_package_or_downgrade(release_zip, tmp_path):
    t = transaction(tmp_path)
    with pytest.raises(UpdateError, match="SHA256"):
        t.install(release_zip, "0" * 64, "1.0.0")
    with pytest.raises(UpdateError, match="相同"):
        t.install(release_zip, sha(release_zip), "0.1.0")
    assert installed_version(t.target) == "0.1.0"


def test_source_mode_never_launches_or_modifies_install(tmp_path):
    with patch.object(installer.subprocess, "Popen") as launch, pytest.raises(UpdateError, match="开发模式"):
        installer.launch_installer(tmp_path / "test.zip", "0" * 64, "1.0.0")
    launch.assert_not_called()
    with pytest.raises(UpdateError, match="源码"):
        installer.Transaction(ROOT, tmp_path / "data")


def test_data_inside_install_is_rejected(tmp_path):
    target = make_release(tmp_path / NAME)
    with pytest.raises(UpdateError, match="用户数据"):
        installer.Transaction(target, target / "config")


@pytest.mark.parametrize("fail", [False, True])
def test_portable_update_preserves_data_and_rolls_back(release_zip, tmp_path, fail):
    target = make_release(tmp_path / "installed" / NAME, "0.1.0")
    profile = target / "userdata/config/gui.json"
    profile.parent.mkdir(parents=True)
    profile.write_bytes(b'{"competition_votes": 8}')
    def hook(phase):
        if fail and phase == "after_install":
            raise PermissionError("portable rollback injection")
    t = installer.Transaction(target, target / "userdata", stopped=lambda: None, hook=hook)
    if fail:
        with pytest.raises(PermissionError):
            t.install(release_zip, sha(release_zip), "1.0.0")
        assert installed_version(target) == "0.1.0"
    else:
        stage = t.install(release_zip, sha(release_zip), "1.0.0")
        t.commit()
        assert (stage / "backup/userdata/config/gui.json").read_bytes() == profile.read_bytes()
    assert profile.read_bytes() == b'{"competition_votes": 8}'


def test_run_request_waits_before_mutation_and_restarts(release_zip, tmp_path):
    t = transaction(tmp_path)
    copied = tmp_path / release_zip.name
    shutil.copy2(release_zip, copied)
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"schema": 1, "pid": 1234, "archive": str(copied), "sha256": sha(copied),
        "version": "1.0.0", "target": str(t.target), "data_root": str(t.data_root)}))
    observed = []
    def wait(*args):
        observed.append("wait")
        assert installed_version(t.target) == "0.1.0"
    def restart(target, data, probe=None):
        observed.append("restart")
        assert installed_version(target) == "1.0.0"
    with patch.object(installer, "wait_parent", side_effect=wait), patch.object(installer, "restart", side_effect=restart), \
            patch.object(installer, "ensure_stopped"), patch.object(installer, "wait_health"):
        installer.run_request(request)
    assert observed == ["wait", "restart"]
    assert json.loads((tmp_path / "result.json").read_text())["ok"]


def test_restart_failure_restores_old_release(release_zip, tmp_path):
    t = transaction(tmp_path)
    copied = tmp_path / release_zip.name
    shutil.copy2(release_zip, copied)
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"schema": 1, "pid": 123, "archive": str(copied), "sha256": sha(copied),
        "version": "1.0.0", "target": str(t.target), "data_root": str(t.data_root)}))
    with patch.object(installer, "wait_parent"), patch.object(installer, "ensure_stopped"), \
            patch.object(installer, "restart", side_effect=[OSError("launch failed"), Mock()]) as restart:
        with pytest.raises(OSError, match="launch failed"):
            installer.run_request(request)
    assert restart.call_count == 2 and installed_version(t.target) == "0.1.0"


def test_health_receipt_checks_token_and_pid(tmp_path):
    process = Mock(pid=42)
    process.poll.return_value = None
    (tmp_path / "probe.json").write_text('{"token":"fixture"}')
    (tmp_path / "health.json").write_text('{"token":"fixture", "pid":42}')
    installer.wait_health(process, tmp_path, timeout=1)
    (tmp_path / "health.json").write_text('{"token":"wrong", "pid":42}')
    with pytest.raises(UpdateError, match="回执"):
        installer.wait_health(process, tmp_path, timeout=1)
    (tmp_path / "health.json").unlink()
    with pytest.raises(UpdateError, match="超时"):
        installer.wait_health(process, tmp_path, timeout=.01)
    process.poll.return_value = 1
    with pytest.raises(UpdateError, match="启动失败"):
        installer.wait_health(process, tmp_path)


def test_gui_initialization_failure_rolls_back_before_tasks(release_zip, tmp_path):
    t = transaction(tmp_path)
    copied = tmp_path / release_zip.name
    shutil.copy2(release_zip, copied)
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"schema": 1, "pid": 123, "archive": str(copied), "sha256": sha(copied),
        "version": "1.0.0", "target": str(t.target), "data_root": str(t.data_root)}))
    child = Mock()
    with patch.object(installer, "wait_parent"), patch.object(installer, "ensure_stopped"), \
            patch.object(installer, "restart", return_value=child) as launch, \
            patch.object(installer, "wait_health", side_effect=UpdateError("新版启动失败")):
        with pytest.raises(UpdateError, match="启动失败"):
            installer.run_request(request)
    child.wait.assert_called_once_with(timeout=10)
    assert launch.call_count == 2 and installed_version(t.target) == "0.1.0"
    assert list(t.work.glob("transaction-*/abort"))


def test_frozen_launch_uses_local_verified_helper_and_unique_attempts(release_zip, tmp_path):
    target = make_release(tmp_path / "installed" / NAME, "0.1.0")
    profile = tmp_path / "profile"
    profile.mkdir()
    with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(target / EXE)), \
            patch.object(installer, "current_version", return_value="0.1.0"), patch.object(installer, "DATA_ROOT", profile), \
            patch.object(installer.subprocess, "Popen") as launch:
        _, request1 = installer.launch_installer(release_zip, sha(release_zip), "1.0.0")
        (request1.parent / "cancel").touch()
        _, request2 = installer.launch_installer(release_zip, sha(release_zip), "1.0.0")
        assert request1 != request2 and not (request2.parent / "cancel").exists()
        payload = json.loads(request2.read_text(encoding="utf-8"))
        assert payload["target"] == str(target.resolve()) and payload["data_root"] == str(profile)
        assert sha(request2.parent / package.UPDATER) == sha(target / package.UPDATER)
        assert sha(Path(payload["archive"])) == sha(release_zip)
        assert Path(launch.call_args.args[0][0]).parent == request2.parent
        (target / package.UPDATER).write_bytes(b"tampered")
        with pytest.raises(UpdateError, match="更新器验证"):
            installer.launch_installer(release_zip, sha(release_zip), "1.0.0")
        assert launch.call_count == 2  # All fixture executables remain inert.
