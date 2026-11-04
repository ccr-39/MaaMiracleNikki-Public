"""Windows onedir build, validation and recoverable fixed-directory deployment.

Only dist/MaaMiracleNikki is a public release. Work is staged under build;
build/windows-package-backup holds one previous release for rollback.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import marshal
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import uuid

NAME = "MaaMiracleNikki"
EXE = NAME + ".exe"
INFO = "mmn-release.json"
BACKUP_FILES = "mmn-backup-files.json"
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.windows_powershell import windows_powershell_env


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def safe_path(path, parent):
    """All move/delete destinations must remain strictly inside their owner."""
    path, parent = Path(path).absolute(), Path(parent).resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(parent) or resolved == parent:
        raise RuntimeError(f"Unsafe package path outside {parent}: {path}")
    # Do not follow reparse points even when their destination is inside parent.
    for part in (path, *path.parents):
        if part == parent:
            break
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise RuntimeError(f"Reparse point in package path: {part}")
    return path


def remove_tree(path, parent):
    path = safe_path(path, parent)
    if path.exists():
        for child in path.rglob("*"):
            safe_path(child, path)
        shutil.rmtree(path)


def input_files(root):
    files = [root / "app_paths.py", root / "signin_options.py", root / "tools/run_task.py", root / "tools/windows_package.py", root / "packaging/launcher.py",
             root / "tools/windows_powershell.py",
             root / "packaging/updater_entry.py",
             root / "LICENSE", root / "THIRD_PARTY_NOTICES.md"]
    for folder in ("gui", "agent", "updates"):
        files += [p for p in (root / folder).rglob("*.py") if "__pycache__" not in p.parts]
    for folder in ("assets", "gui/icons"):
        files += [p for p in (root / folder).rglob("*") if p.is_file()]
    return sorted(set(files))


def source_fingerprint(root):
    return {p.relative_to(root).as_posix(): sha(p) for p in input_files(root)}


def release_files(release):
    return {p.relative_to(release).as_posix(): sha(p) for p in release.rglob("*")
            if p.is_file() and p != release / "_internal" / BACKUP_FILES}


def check_retirable_backup(backup):
    ledger = backup / "_internal" / BACKUP_FILES
    record = json.loads(ledger.read_text(encoding="utf-8")) if ledger.exists() else {}
    if record.get("unclassified") or record.get("files") != release_files(backup):
        raise RuntimeError(f"Previous backup has unknown/modified files; retained for inspection: {backup}")
    # Never rotate out a backup that contains misplaced mutable user data.
    for name in release_files(backup):
        if name.split("/", 1)[0].lower() in {"userdata", "config", "cache", "logs", "state", "images"}:
            raise RuntimeError(f"User data found in backup; retained for inspection: {backup}")


def normalized_code(code):
    return code.replace(co_filename="<source>", co_consts=tuple(
        normalized_code(v) if isinstance(v, types.CodeType) else v for v in code.co_consts))


def validate_sources(release, root):
    """Prove a reused/built bundle contains current runtime code and resources."""
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(release / EXE))
    pyz = archive.open_embedded_archive(next(n for n in archive.toc if n.endswith(".pyz")))
    updater = CArchiveReader(str(release / "MMNUpdater.exe"))
    updater_pyz = updater.open_embedded_archive(next(n for n in updater.toc if n.endswith(".pyz")))
    for source in input_files(root):
        relative = source.relative_to(root)
        if relative == Path("packaging/launcher.py"):
            bundled = marshal.loads(archive.extract("launcher"))
        elif relative == Path("packaging/updater_entry.py"):
            bundled = marshal.loads(updater.extract("updater_entry"))
        elif relative.suffix == ".py":
            if relative.parts[0] == "agent":
                if source.stem == "main":
                    # Standalone Agent entry is shipped as source, not GUI code.
                    if (release / "_internal" / relative).read_bytes() != source.read_bytes():
                        raise RuntimeError(f"Stale agent source: {relative}")
                    continue
                module = source.stem
            else:
                module = ".".join(relative.with_suffix("").parts).removesuffix(".__init__")
            if module not in pyz.toc:
                raise RuntimeError(f"Missing bundled module: {module}")
            bundled = pyz.extract(module)
        else:
            if sha(release / "_internal" / relative) != sha(source):
                raise RuntimeError(f"Stale bundled resource: {relative}")
            continue
        if normalized_code(bundled) != normalized_code(compile(source.read_bytes(), str(source), "exec", dont_inherit=True)):
            raise RuntimeError(f"Stale bundled code: {relative}")
        if relative.parts[0] == "updates" or relative in (
                Path("app_paths.py"), Path("tools/windows_package.py"), Path("tools/windows_powershell.py")):
            module = ".".join(relative.with_suffix("").parts).removesuffix(".__init__")
            if module not in updater_pyz.toc or normalized_code(updater_pyz.extract(module)) != normalized_code(
                    compile(source.read_bytes(), str(source), "exec", dont_inherit=True)):
                raise RuntimeError(f"Stale updater code: {relative}")
        if relative.parts[0] == "agent" and (release / "_internal" / relative).read_bytes() != source.read_bytes():
            raise RuntimeError(f"Stale agent source: {relative}")


def validate_layout(release):
    required = [EXE, "_internal/base_library.zip", "_internal/assets/interface.json",
                "_internal/PySide6/Qt6Core.dll", "_internal/PySide6/Qt6Gui.dll",
                "_internal/PySide6/Qt6Widgets.dll", "_internal/PySide6/plugins/platforms/qwindows.dll"]
    required += ["_internal/assets/resource/model/ocr/" + n for n in ("det.onnx", "rec.onnx", "keys.txt")]
    required += ["_internal/maa/bin/" + n for n in (
        "MaaFramework.dll", "MaaToolkit.dll", "MaaAgentClient.dll", "MaaAgentServer.dll",
        "MaaAdbControlUnit.dll", "MaaCustomControlUnit.dll", "MaaUtils.dll",
        "fastdeploy_ppocr_maa.dll", "onnxruntime_maa.dll", "opencv_world4_maa.dll", "DirectML.dll")]
    missing = [p for p in required if not (release / p).is_file() or not (release / p).stat().st_size]
    if missing or not list((release / "_internal").glob("python3*.dll")):
        raise RuntimeError(f"Incomplete onedir release: {missing}")
    if not list((release / "_internal/assets/resource/pipeline").glob("*.json")):
        raise RuntimeError("Missing Pipeline JSON resources")


def running_instances():
    command = ("[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); "
               "Get-CimInstance Win32_Process -Filter \"name = 'MaaMiracleNikki.exe'\" | "
               "Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress")
    result = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                            check=True, capture_output=True, encoding="utf-8",
                            env=windows_powershell_env(),
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    data = json.loads(result.stdout) if result.stdout.strip() else []
    return data if isinstance(data, list) else [data]


def ensure_stopped(release_dir=ROOT / "dist" / NAME):
    instances = running_instances()
    if instances:
        raise RuntimeError("Please close MaaMiracleNikki before building/replacing; no process was stopped: "
                           + json.dumps(instances, ensure_ascii=False))
    if Path(release_dir).exists():
        # A surviving ADB server/terminal can lock the directory through its cwd
        # even when no MMN process exists. Check DELETE access without deleting.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(release_dir), 0x10000, 7, None, 3, 0x02000000, None)
        if handle == wintypes.HANDLE(-1).value:
            error = ctypes.get_last_error()
            raise RuntimeError(f"Release directory is occupied or not writable (Windows {error}): {release_dir}. "
                               "Please close the app, ADB server or terminal holding it; no process was stopped.")
        kernel.CloseHandle(handle)


def offline_verify(root, release, report):
    validate_layout(release)
    result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                             str(root / "tools/verify_windows.ps1"), "-Exe", str(release / EXE),
                             "-ReportPath", str(report)], cwd=root, check=False,
                            env=windows_powershell_env(),
                            capture_output=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"Offline verification failed ({release}): {result.stdout}\n{result.stderr}")
    return json.loads(report.read_text(encoding="utf-8-sig"))


class Deployment:
    def __init__(self, root, verify, stopped=ensure_stopped, hook=lambda _: None):
        self.root = Path(root).resolve()
        self.build = self.root / "build"
        self.target = self.root / "dist" / NAME
        self.backup = self.build / "windows-package-backup" / NAME
        self.journal = self.build / "windows-package-transaction.json"
        self.verify, self.stopped, self.hook = verify, stopped, hook

    def stage(self):
        self.build.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=".windows-package-", dir=self.build))

    def _move(self, source, destination):
        safe_path(source, self.root)
        safe_path(destination, self.root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            source.rename(destination)
        except OSError as exc:
            raise OSError(f"Cannot rename {source} -> {destination}. Close programs, ADB servers or terminals "
                          f"holding this directory; no process was terminated. {exc}") from exc

    def _stage_from(self, state):
        stage = safe_path(Path(state["stage"]), self.build)
        if stage.parent != self.build or not stage.name.startswith(".windows-package-"):
            raise RuntimeError(f"Invalid transaction stage: {stage}")
        return stage

    def recover(self):
        if not self.journal.exists():
            return
        self.stopped()
        state = json.loads(self.journal.read_text(encoding="utf-8"))
        stage = self._stage_from(state)
        candidate = stage / "dist" / NAME
        prior = stage / "prior-backup"
        if not state.get("validated"):
            info = self.target / "_internal" / INFO
            installed_id = json.loads(info.read_text(encoding="utf-8")).get("transaction_id") if info.exists() else None
            if installed_id == state["id"]:
                self._move(self.target, stage / "failed-release")
            if state["had_target"] and not self.target.exists():
                if not self.backup.exists():
                    raise RuntimeError("Cannot recover old release: backup missing; transaction retained")
                self._move(self.backup, self.target)
                (self.target / "_internal" / BACKUP_FILES).unlink(missing_ok=True)
            if prior.exists():
                if self.backup.exists():
                    raise RuntimeError("Cannot restore earlier backup; transaction retained")
                self._move(prior, self.backup)
            if state["had_target"]:
                self.verify(self.target, self.build / "windows-package-rollback-check.json")
        elif not self.target.exists():
            raise RuntimeError("Validated release missing; transaction retained for inspection")
        # Keep the journal until every required recovery step has completed.
        remove_tree(stage, self.build)
        self.journal.unlink()

    def promote(self, stage):
        self.stopped()
        stage = self._stage_from({"stage": str(stage)})
        candidate = stage / "dist" / NAME
        self.verify(candidate, self.build / "windows-package-stage-check.json")
        self.stopped()  # The old EXE may have been opened during the build.
        if self.backup.exists():
            check_retirable_backup(self.backup)
        state = {"id": uuid.uuid4().hex, "stage": str(stage), "had_target": self.target.exists(), "validated": False}
        info_path = candidate / "_internal" / INFO
        info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.exists() else {}
        info["transaction_id"] = state["id"]
        write_json(info_path, info)
        if (candidate / "mmn-update.json").exists():
            from updates.package import write_manifest
            write_manifest(candidate, info.get("version") or json.loads(
                (candidate / "_internal/assets/interface.json").read_text(encoding="utf-8"))["version"])
        # Preserve installation-local data in both the complete old backup and
        # the replacement. Verification itself uses a separate program copy.
        from updates.package import PRESERVE
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
        write_json(self.journal, state)
        try:
            if state["had_target"] and self.backup.exists():
                self._move(self.backup, stage / "prior-backup")
            self.hook("before_backup")
            if self.target.exists():
                self._move(self.target, self.backup)
                files = release_files(self.backup)
                unclassified = sorted(set(files) - set(release_files(candidate)) - {"_internal/" + INFO})
                write_json(self.backup / "_internal" / BACKUP_FILES,
                           {"files": files, "unclassified": unclassified})
            self.hook("after_backup")
            self._move(candidate, self.target)
            self.hook("after_install")
            self.verify(self.target, self.build / "windows-package-final-check.json")
            self.hook("after_verify")
            state["validated"] = True
            write_json(self.journal, state)
        except BaseException:
            # Permission errors, locked files and Ctrl-C all use the same rollback.
            # If rollback itself fails, retain journal, stage and backup intact.
            try:
                self.recover()
            except BaseException as recovery_error:
                raise RuntimeError(f"Deployment failed and rollback needs attention; close the app and rerun. "
                                   f"Journal: {self.journal}; {recovery_error}") from recovery_error
            raise
        self.recover()  # validated transaction: only clean this round's stage


@contextmanager
def build_lock(build):
    import msvcrt
    build.mkdir(parents=True, exist_ok=True)
    with (build / "windows-package.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("Another packaging operation is running") from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def build_candidate(root, stage, install_dependencies=False):
    if install_dependencies:
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(root / "packaging/requirements.txt")], check=True)
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--distpath", str(stage / "dist"), "--workpath", str(stage / "work"), str(root / "MaaMiracleNikki.spec")]
    log = root / "build/windows-package-build.log"
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(command, cwd=root, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"PyInstaller build failed; previous release retained. See {log}")


def prepare_update_manifest(candidate):
    from updates.package import write_manifest
    version = json.loads((candidate / "_internal/assets/interface.json").read_text(encoding="utf-8"))["version"]
    write_manifest(candidate, version)


def run(root=ROOT, *, reuse=None, install_dependencies=False, builder=build_candidate,
        verify=None, stopped=ensure_stopped):
    root = Path(root).resolve()
    verify = verify or (lambda release, report: offline_verify(root, release, report))
    deployment = Deployment(root, verify, stopped)
    with build_lock(deployment.build):
        stopped()
        deployment.recover()
        before = source_fingerprint(root)
        stage = deployment.stage()
        try:
            if reuse is not None:
                reuse = Path(reuse).resolve()
                validate_layout(reuse)
                validate_sources(reuse, root)
                shutil.copytree(reuse, stage / "dist" / NAME)
            else:
                builder(root, stage, install_dependencies)
            candidate = stage / "dist" / NAME
            validate_layout(candidate)
            validate_sources(candidate, root)
            if before != source_fingerprint(root):
                raise RuntimeError("Source/resources changed during build; previous release retained")
            write_json(candidate / "_internal" / INFO,
                       {"runtime_inputs": before, "exe_sha256": sha(candidate / EXE),
                        "reused_from": str(reuse) if reuse else None})
            prepare_update_manifest(candidate)
            deployment.promote(stage)
            report = {"ok": True, "exe": str(deployment.target / EXE), "exe_sha256": sha(deployment.target / EXE),
                      "backup": str(deployment.backup), "reused_from": str(reuse) if reuse else None}
            write_json(root / "build/windows-package-report.json", report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return report
        finally:
            # A failed/incomplete rollback owns its stage until next recovery.
            if stage.exists() and not deployment.journal.exists():
                remove_tree(stage, deployment.build)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-dependencies", action="store_true")
    parser.add_argument("--reuse", type=Path, help="Validate and adopt an existing full release without recompiling")
    args = parser.parse_args()
    try:
        run(reuse=args.reuse, install_dependencies=args.install_dependencies)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Packaging failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
