"""Create the public ZIP and SHA256 sidecar locally; never publish a Release."""
import argparse
import json
import re
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.windows_package import NAME, sha, safe_path
from updates.package import MANIFEST, validate_package
from updates.version import Version


def bundle(release, output, *, test_build=False, build_id=None):
    release, output = Path(release).resolve(), Path(output).resolve()
    version = json.loads((release / MANIFEST).read_text(encoding="utf-8"))["version"]
    if Version(version).pre and not test_build:
        raise ValueError("只能导出稳定版本，请先设置 assets/interface.json 的版本并构建")
    validate_package(release, version)
    if output.is_relative_to(release):
        raise ValueError("导出目录必须位于发行目录之外")
    output.mkdir(parents=True, exist_ok=True)
    if test_build and (not isinstance(build_id, str) or not re.fullmatch(r"[0-9a-f]{12,40}", build_id)):
        raise ValueError("测试构建必须提供 commit 标识")
    label = f"test-{version}-{build_id[:12]}" if test_build else version
    path = output / f"{NAME}-{label}-windows-x64.zip"
    # Exclusive creation avoids accidentally overwriting an artifact in use.
    with zipfile.ZipFile(path, "x", zipfile.ZIP_DEFLATED) as archive:
        files = json.loads((release / MANIFEST).read_text(encoding="utf-8"))["files"]
        for name in [MANIFEST, *files]:
            archive.write(safe_path(release / name, release), NAME + "/" + name)
    path.with_suffix(".zip.sha256").write_text(f"{sha(path)}  {path.name}\n", encoding="ascii")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test-build", action="store_true")
    parser.add_argument("--build-id")
    args = parser.parse_args()
    print(bundle(args.release, args.output, test_build=args.test_build, build_id=args.build_id))
