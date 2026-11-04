"""Strict Windows ZIP layout and inventory, shared by publishing and installing."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import stat
import zipfile

from tools.windows_package import EXE, NAME, safe_path, sha, validate_layout, write_json
from updates.client import UpdateError, check_cancel
from updates.version import Version

MANIFEST = "mmn-update.json"
UPDATER = "MMNUpdater.exe"
MUTABLE = ("userdata", "config", "cache", "logs", "state", "images", "gui.json", "runtime.json", "device.json")
PRESERVE = (*MUTABLE, *("_internal/" + name for name in MUTABLE))
MAX_UNPACKED = 6 * 1024**3


def relative_name(value):
    if not isinstance(value, str) or not value or "\\" in value or len(value) > 230:
        raise UpdateError("压缩包路径无效")
    parts = value.split("/")
    for part in parts:
        if (not part or part in (".", "..") or part[-1] in " ." or
                any(ord(c) < 32 or c in ':<>"|?*' for c in part) or
                re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?", part)):
            raise UpdateError("压缩包含不安全的 Windows 路径")
    return value


def is_mutable(name):
    return any(name.lower() == p or name.lower().startswith(p + "/") for p in PRESERVE)


def manifest_data(data, version):
    if (not isinstance(data, dict) or data.get("schema") != 1 or data.get("product") != NAME or
            data.get("platform") != "windows-x64" or data.get("version") != version):
        raise UpdateError("发行清单的产品、版本或平台不匹配")
    Version(version)
    files = data.get("files")
    if not isinstance(files, dict) or not files or len(files) > 50000:
        raise UpdateError("发行文件清单无效")
    folded = set()
    for name, digest in files.items():
        relative_name(name)
        if name.lower() in folded or name == MANIFEST or is_mutable(name) or not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
            raise UpdateError("发行清单含重复、不安全或可变用户文件")
        folded.add(name.lower())
    if not {EXE, UPDATER, "_internal/assets/interface.json"}.issubset(files):
        raise UpdateError("发行清单缺少入口或独立更新器")
    return files


def inspect_archive(path, version):
    with zipfile.ZipFile(path) as archive:
        entries, folded, total = {}, set(), 0
        for info in archive.infolist():
            name = relative_name(info.filename.rstrip("/") if info.is_dir() else info.filename)
            mode = info.external_attr >> 16
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR) or info.flag_bits & 1:
                raise UpdateError("压缩包含链接、特殊文件或加密文件")
            if name == NAME and info.is_dir():
                continue
            if not name.startswith(NAME + "/") or name.lower() in folded:
                raise UpdateError("压缩包根目录或大小写重复路径无效")
            folded.add(name.lower())
            if info.is_dir():
                continue
            total += info.file_size
            if total > MAX_UNPACKED or len(entries) >= 50000:
                raise UpdateError("更新包解压大小或文件数超限")
            entries[name[len(NAME) + 1:]] = info
        entry = entries.get(MANIFEST)
        if not entry or entry.file_size > 8 * 1024**2:
            raise UpdateError("更新包缺少发行清单")
        files = manifest_data(json.loads(archive.read(entry)), version)
        if set(entries) != set(files) | {MANIFEST}:
            raise UpdateError("压缩包与发行文件清单不一致")
        # Detect a file being used as a parent before extracting anything.
        names = {n.lower() for n in entries}
        if any(str(p).replace("\\", "/").lower() in names for n in entries for p in Path(n).parents if str(p) != "."):
            raise UpdateError("压缩包路径存在文件与目录冲突")
        return files


def extract_archive(path, destination, version, cancel):
    files = inspect_archive(path, version)
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(path) as archive:
        for name in [MANIFEST, *files]:
            check_cancel(cancel)
            target = safe_path(destination / name, destination)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(NAME + "/" + name) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, 256 * 1024)
    validate_package(destination, version)


def validate_package(path, version):
    files = manifest_data(json.loads((path / MANIFEST).read_text(encoding="utf-8")), version)
    actual = set()
    for item in path.rglob("*"):
        safe_path(item, path)
        if item.is_file():
            name = item.relative_to(path).as_posix()
            if name == MANIFEST or is_mutable(name):
                continue
            actual.add(name)
            if name not in files or sha(item) != files[name]:
                raise UpdateError(f"发行文件校验失败：{name}")
    if actual != set(files):
        raise UpdateError("发行文件不完整")
    interface = json.loads((path / "_internal/assets/interface.json").read_text(encoding="utf-8"))
    if interface.get("version") != version:
        raise UpdateError("应用版本与 Release 版本不一致")
    validate_layout(path)


def write_manifest(path, version):
    files = {}
    for item in path.rglob("*"):
        safe_path(item, path)
        if item.is_file() and item != path / MANIFEST:
            name = item.relative_to(path).as_posix()
            if is_mutable(name):
                raise UpdateError("发行目录包含用户数据，拒绝发布")
            files[name] = sha(item)
    value = {"schema": 1, "product": NAME, "platform": "windows-x64", "version": version, "files": files}
    manifest_data(value, version)
    write_json(path / MANIFEST, value)
