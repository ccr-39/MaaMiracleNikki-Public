"""Recoverable whole-directory transaction. Never terminates any process."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Event
import time
import uuid

from app_paths import DATA_ROOT
from tools.windows_package import EXE, NAME, ensure_stopped, safe_path, sha, write_json
from updates.client import UpdateError
from updates.package import MANIFEST, PRESERVE, UPDATER, extract_archive, validate_package
from updates.version import Version, current_version


def move(source, target):
    try:
        source.rename(target)
    except OSError as exc:
        raise UpdateError(f"文件被占用或没有写入权限：{source}。请关闭占用文件的程序后重试；未强制结束任何进程。") from exc


class Transaction:
    def __init__(self, target, data_root, *, stopped=None, validate=validate_package, hook=lambda phase: None):
        raw = Path(target).absolute()
        safe_path(raw, raw.parent)
        self.target = raw.resolve()
        self.data_root = Path(data_root).resolve()
        if self.target.name != NAME or (self.target / "gui/main.py").exists() or (self.target / ".git").exists():
            raise UpdateError("更新目标必须是 MMN 发行目录，禁止替换源码")
        self.work = self.target.parent / (".mmn-update-" + NAME)
        safe_path(self.work, self.target.parent)
        if ((self.data_root.is_relative_to(self.target) and self.data_root != self.target / "userdata")
                or self.data_root.is_relative_to(self.work)):
            raise UpdateError("用户数据目录位于程序或更新目录内，请先移至独立目录")
        self.work.mkdir(exist_ok=True)
        self.journal = self.work / "transaction.json"
        self.stopped = stopped or (lambda: ensure_stopped(self.target))
        self.validate, self.hook = validate, hook

    @contextmanager
    def lock(self):
        import msvcrt
        lock_path = safe_path(self.work / "install.lock", self.work)
        with lock_path.open("a+b") as stream:
            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise UpdateError("另一个更新器正在运行，请稍后重试") from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

    def _stage(self, state):
        stage = safe_path(self.work / state["stage"], self.work)
        if stage.parent != self.work or not stage.name.startswith("transaction-"):
            raise UpdateError("更新事务路径无效")
        return stage

    def recover(self):
        if not self.journal.exists():
            return
        self.stopped()
        state = json.loads(self.journal.read_text(encoding="utf-8"))
        stage = self._stage(state)
        backup = stage / "backup"
        if state["committed"]:
            if not self.target.is_dir():
                raise UpdateError("更新后程序目录缺失；请保留事务和备份并联系开发者")
        elif backup.exists():
            if self.target.exists():
                move(self.target, stage / "failed-release")
            move(backup, self.target)
        elif not self.target.exists():
            raise UpdateError("原程序及回滚备份均缺失，请保留更新事务")
        self.journal.unlink()
        # Retain backups, failed candidates and journals until recovery succeeds.
        # Never delete unknown user files in an older installation.

    def install(self, archive, digest, version):
        self.recover()
        self.stopped()
        installed = json.loads((self.target / "_internal/assets/interface.json").read_text(encoding="utf-8"))["version"]
        if Version(version) <= Version(installed):
            raise UpdateError("已安装相同或更新版本，取消安装")
        if sha(archive) != digest:
            raise UpdateError("安装前 SHA256 校验失败")
        stage = Path(tempfile.mkdtemp(prefix="transaction-", dir=self.work))
        candidate = stage / "candidate"
        extract_archive(archive, candidate, version, Event())
        self.validate(candidate, version)
        # Legacy portable profiles remain byte-for-byte intact. New packages
        # cannot supply these paths. Normal profiles live outside the install.
        for name in PRESERVE:
            source = safe_path(self.target / name, self.target)
            if source.exists():
                for child in source.rglob("*") if source.is_dir() else ():
                    safe_path(child, self.target)
                destination = safe_path(candidate / name, candidate)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, destination)
                else:
                    shutil.copy2(source, destination)
        self.stopped()
        state = {"stage": stage.name, "version": version, "committed": False}
        write_json(self.journal, state)
        try:
            self.hook("before_backup")
            move(self.target, stage / "backup")
            self.hook("after_backup")
            move(candidate, self.target)
            self.hook("after_install")
            self.validate(self.target, version)
            self.hook("after_verify")
        except BaseException as exc:
            try:
                self.recover()
            except BaseException as recovery_error:
                raise UpdateError(f"更新失败，回滚被占用阻止。请关闭占用程序后重试更新器。保留记录：{self.journal}；{recovery_error}") from exc
            raise
        return stage

    def commit(self):
        state = json.loads(self.journal.read_text(encoding="utf-8"))
        state["committed"] = True
        write_json(self.journal, state)
        (self._stage(state) / "commit").touch()
        self.journal.unlink()


def wait_parent(pid, ready, timeout=300):
    """Hold the specific Windows process handle: no PID reuse or taskkill."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        # Retry after a previous attempt already observed the original parent.
        # ERROR_INVALID_PARAMETER means that PID no longer exists. Installation
        # still performs the independent, read-only running-instance check.
        if ctypes.get_last_error() == 87 and ready.is_file() and json.loads(
                ready.read_text(encoding="utf-8")).get("pid") == pid:
            return
        raise UpdateError("无法确认主进程状态；取消更新，原版本未改动")
    try:
        write_json(ready, {"ready": True, "pid": pid})
        result = kernel.WaitForSingleObject(handle, timeout * 1000)
        if result != 0:
            raise UpdateError("等待主程序安全退出超时，请关闭 MMN 后重试")
    finally:
        kernel.CloseHandle(handle)


def restart(target, data_root, probe=None):
    args = [str(target / EXE)]
    if probe is not None:
        args += ["--update-probe", str(probe)]
    return subprocess.Popen(args, cwd=data_root, close_fds=True)


def wait_health(process, stage, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise UpdateError("新版 GUI 启动失败，正在恢复旧版")
        ready = stage / "health.json"
        if ready.exists():
            value = json.loads(ready.read_text(encoding="utf-8"))
            expected = json.loads((stage / "probe.json").read_text(encoding="utf-8"))
            if value == {"token": expected["token"], "pid": process.pid}:
                return
            raise UpdateError("新版 GUI 健康回执无效")
        time.sleep(.1)
    raise UpdateError("新版 GUI 启动超时，正在恢复旧版")


def run_request(request_path):
    """Called only by our frozen, installed helper, never a downloaded script."""
    request_path = Path(request_path).resolve()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if request.get("schema") != 1 or type(request.get("pid")) is not int or request["pid"] <= 0:
        raise UpdateError("更新请求无效")
    archive = Path(request["archive"]).resolve()
    if archive.parent != request_path.parent:
        raise UpdateError("更新包必须位于独立下载目录")
    transaction = Transaction(request["target"], request["data_root"])
    with transaction.lock():
        wait_parent(request["pid"], request_path.parent / "ready.json")
        transaction.recover()
        if request.get("cancel") or (request_path.parent / "cancel").exists():
            return
        process, stage = None, None
        try:
            stage = transaction.install(archive, request["sha256"], request["version"])
            write_json(stage / "probe.json", {"token": uuid.uuid4().hex})
            process = restart(transaction.target, transaction.data_root, stage)
            wait_health(process, stage)
            transaction.commit()
            write_json(request_path.parent / "result.json", {"ok": True, "backup": str(stage / "backup")})
        except BaseException:
            if stage is not None and (stage / "commit").exists():
                # A log/cleanup error after commit must never launch a second GUI
                # or roll back a version that has already been released to use.
                raise
            if process is not None and stage is not None and transaction.journal.exists():
                (stage / "abort").touch()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    # Never forcibly stop a hung GUI. Its backup/journal remain
                    # until the user closes it and retries recovery.
                    raise UpdateError(f"新版未安全退出，请关闭该 MMN 窗口后重试。备份已保留：{transaction.journal}")
            transaction.recover()
            if transaction.target.is_dir():
                restart(transaction.target, transaction.data_root)
            raise


def launch_installer(archive, digest, version):
    if not getattr(sys, "frozen", False) or Path(sys.executable).name != EXE:
        raise UpdateError("Python 开发模式仅支持检查和下载，安装请使用发行版 EXE")
    target = Path(sys.executable).resolve().parent
    # Require our baseline manifest; a source checkout can never be the target.
    manifest = json.loads((target / MANIFEST).read_text(encoding="utf-8"))
    helper = target / UPDATER
    if manifest.get("version") != current_version() or manifest.get("files", {}).get(UPDATER) != sha(helper):
        raise UpdateError("本地独立更新器验证失败，请重新获取官方发行版")
    Transaction(target, DATA_ROOT)  # Validate target/data paths before leaving GUI.
    archive = Path(archive).resolve()
    # Each attempt has its own markers; a cancelled waiting helper must never
    # observe a later request and accidentally install it when this GUI exits.
    # The helper, its cwd, archive and result must survive the installation
    # directory rename, including when downloads live under local userdata.
    attempt = Path(tempfile.mkdtemp(prefix="mmn-installer-"))
    helper_copy = attempt / UPDATER
    shutil.copy2(helper, helper_copy)
    request_path = attempt / "request.json"
    # Keep a reference to the verified ZIP outside the helper's own directory.
    local_archive = attempt / archive.name
    try:
        os.link(archive, local_archive)
    except OSError:
        shutil.copy2(archive, local_archive)
    write_json(request_path, {"schema": 1, "pid": os.getpid(), "archive": str(local_archive), "sha256": digest,
                             "version": version, "target": str(target), "data_root": str(DATA_ROOT)})
    process = subprocess.Popen([str(helper_copy), "--request", str(request_path)], cwd=attempt,
                               close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return process, request_path


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    while True:
        try:
            if not getattr(sys, "frozen", False):
                raise UpdateError("独立更新器只能从打包发行版启动")
            run_request(args.request)
            return 0
        except BaseException as exc:
            try:
                write_json(args.request.parent / "result.json", {"ok": False, "error": str(exc)})
            finally:
                result = ctypes.windll.user32.MessageBoxW(None,
                    f"更新未完成：{exc}\n请关闭占用文件的程序或其他 MMN 窗口，然后点击“重试”。\n"
                    f"备份与事务记录已保留。更新请求：{args.request}", "MMN 软件更新", 0x115)
            if result != 4:  # IDRETRY; no taskkill, TerminateProcess or ADB commands.
                return 1
