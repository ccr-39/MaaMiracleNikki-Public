"""Prepare a clean offline Windows ZIP and verified source attachments; never publish.

Run with the pinned packaging environment. The runtime may be freshly built or
an already offline-verified build: its embedded code must match this source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.windows_package import EXE, NAME, source_fingerprint, validate_layout, validate_sources
from updates.package import MANIFEST, inspect_archive, is_mutable, validate_package, write_manifest


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def check_public_files(package):
    for path in package.rglob('*'):
        if not path.is_file():
            continue
        name = path.relative_to(package).as_posix()
        if (is_mutable(name) or set(Path(name).parts) & {'__pycache__', 'tests', 'fixtures', '.git'}
                or path.name.startswith('test_') or path.suffix.lower() in {'.log', '.bak', '.tmp', '.pem', '.key', '.db', '.pyc'}):
            raise ValueError('Private/generated file in runtime: ' + name)
        if path.name.lower().startswith(('opencv_videoio_ffmpeg', 'api-ms-win-')) or path.name.lower() == 'ucrtbase.dll':
            raise ValueError('Excluded optional/legacy native library: ' + name)
        if path.suffix.lower() in {'.py', '.md', '.json', '.txt', '.ps1', '.svg'}:
            text = path.read_text(encoding='utf-8-sig', errors='replace')
            if re.search(r'gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{25,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|(?i:[A-Z]:[\\/]+Users[\\/]+ASUS)', text):
                raise ValueError('Credential or concrete user path: ' + name)


def prepare(runtime, source_archives, output):
    runtime, source_archives, output = map(lambda p: Path(p).resolve(), (runtime, source_archives, output))
    if output.exists() or output.is_relative_to(runtime) or output == ROOT:
        raise ValueError('Use a new output directory outside the runtime and source root')
    validate_layout(runtime)
    runtime_manifest = json.loads((runtime / MANIFEST).read_text(encoding='utf-8'))
    validate_package(runtime, runtime_manifest['version'])
    manifest = json.loads((ROOT / 'licenses/corresponding-source-manifest.json').read_text(encoding='utf-8'))
    archives = manifest['archives']
    if len(archives) != 13:
        raise ValueError('Expected the reviewed set of 13 corresponding-source archives')
    for entry in archives:
        name = entry['file']
        if Path(name).name != name:
            raise ValueError('Invalid archive name')
        path = source_archives / name
        if path.stat().st_size != entry['bytes'] or sha(path) != entry['sha256']:
            raise ValueError('Corresponding-source hash/size mismatch: ' + name)

    output.mkdir(parents=True)
    package = output / 'package' / NAME
    package.mkdir(parents=True)
    # Copy validated runtime bytes only, never copy userdata or a developer tree.
    for name in runtime_manifest['files']:
        if name in {EXE, 'MMNUpdater.exe'} or name.startswith('_internal/'):
            if name.startswith(('_internal/docs/', '_internal/licenses/')) or name == '_internal/mmn-release.json':
                continue
            copy_file(runtime / name, package / name)
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md', 'DISCLAIMER.md', 'CHANGELOG.md'):
        copy_file(ROOT / name, package / name)
    copy_file(ROOT / 'LICENSE', package / '_internal/LICENSE')
    copy_file(ROOT / 'THIRD_PARTY_NOTICES.md', package / '_internal/THIRD_PARTY_NOTICES.md')
    for folder in ('assets', 'gui/icons'):
        for path in (ROOT / folder).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts:
                copy_file(path, package / '_internal' / path.relative_to(ROOT))
    # PyInstaller also keeps agent sources for dynamic imports/__file__ paths.
    # Refresh their text (including checkout newline style); embedded modules
    # are still checked against the EXE below before accepting any changed code.
    for path in (ROOT / 'agent').glob('*.py'):
        copy_file(path, package / '_internal/agent' / path.name)
    for folder in ('licenses', 'docs'):
        shutil.copytree(ROOT / folder, package / folder)
    copy_file(ROOT / 'gui/README.md', package / 'gui/README.md')
    copy_file(ROOT / 'config/gui.example.json', package / 'docs/gui.example.json')
    copy_file(ROOT / 'assets/resource/model/ocr/README.md', package / 'assets/resource/model/ocr/README.md')
    for name, link in [('licenses/README.md', '../../licenses/README.md'),
                       ('docs/ASSET_NOTICE.md', '../../docs/ASSET_NOTICE.md'),
                       ('docs/PUBLIC_RELEASE_STATUS.md', '../../docs/PUBLIC_RELEASE_STATUS.md'),
                       ('docs/DISTRIBUTION_REMEDIATION_20261104.md', '../../docs/DISTRIBUTION_REMEDIATION_20261104.md')]:
        target = package / '_internal' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('# 随包说明\n\n请查看[完整说明](' + link + ')。\n', encoding='utf-8')
    for name in ('ocr/official-models-20261104.json', 'corresponding-source-manifest.json'):
        copy_file(ROOT / 'licenses' / name, package / '_internal/licenses' / name)
    readme = (ROOT / 'packaging/public-readme.md').read_text(encoding='utf-8').replace('](../', '](')
    readme = readme.replace('\n\n', '\n\n## 下载与安装\n\n', 1)
    project_readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    readme += '\n## 功能\n' + project_readme.split('## 功能\n', 1)[1].split('## MuMu / ADB', 1)[0]
    # Keep the shipped quickstart/defaults/FAQ in step with the source README.
    quickstart = '## MuMu / ADB' + project_readme.split('## MuMu / ADB', 1)[1].split('## 源码运行与构建', 1)[0]
    readme += '\n' + quickstart.replace('](config/gui.example.json)', '](docs/gui.example.json)')
    (package / 'README.md').write_text(readme, encoding='utf-8')
    (package / 'PUBLICATION_BLOCKED.txt').write_text(
        'NOT PUBLISHED. Redistribution permission for 22 game templates remains unconfirmed.\n'
        'See docs/ASSET_NOTICE.md and docs/PUBLIC_RELEASE_STATUS.md.\n', encoding='utf-8')

    # Validate all app modules against the actual EXE/PYZ without running tasks.
    validate_layout(package)
    validate_sources(package, ROOT)
    ocr = package / '_internal/assets/resource/model/ocr'
    installer = (ROOT / 'tools/setup_ocr.ps1').read_text(encoding='utf-8')
    for name in ('det.onnx', 'rec.onnx', 'keys.txt'):
        digest = sha(ocr / name)
        if digest not in installer:
            raise ValueError('Model does not match fixed installer: ' + name)
    inputs = source_fingerprint(ROOT)
    for name in ('det.onnx', 'rec.onnx', 'keys.txt'):
        inputs['assets/resource/model/ocr/' + name] = sha(ocr / name)
    (package / '_internal/mmn-release.json').write_text(json.dumps({
        'runtime_inputs': inputs, 'exe_sha256': sha(package / EXE), 'reused_from': None,
        'packaging_note': 'Embedded code/source and runtime assets validated; documentation refreshed; no device tasks executed.'}, indent=2), encoding='utf-8')
    check_public_files(package)
    version = json.loads((ROOT / 'assets/interface.json').read_text(encoding='utf-8'))['version']
    write_manifest(package, version)
    validate_package(package, version)
    destination = output / 'corresponding-source'
    destination.mkdir()
    for entry in archives:
        copy_file(source_archives / entry['file'], destination / entry['file'])
    copy_file(ROOT / 'licenses/corresponding-source-manifest.json', destination / 'manifest.json')
    (destination / 'README.md').write_text(
        '# 对应源码材料\n\n本目录的 13 份归档需与 Windows 包同次提供。版本、URL、SHA256 和补丁位置见 manifest.json。'
        'MaaDeps/vcpkg 含 ports、patches 和构建材料。原始许可证随程序保留；本地准备不等于已向公众交付。\n', encoding='utf-8')
    archive = output / (NAME + '-Windows-x64.zip')
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in sorted(package.rglob('*')):
            if path.is_file():
                bundle.write(path, NAME + '/' + path.relative_to(package).as_posix())
    inspect_archive(archive, version)
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            relative = entry.filename.removeprefix(NAME + '/')
            if hashlib.sha256(bundle.read(entry)).hexdigest() != sha(package / relative):
                raise ValueError('ZIP CRC/content mismatch: ' + relative)
    archive.with_suffix('.zip.sha256').write_text(sha(archive) + '  ' + archive.name + '\n', encoding='ascii')
    result = {'version': version, 'zip': archive.name, 'zip_bytes': archive.stat().st_size,
        'zip_sha256': sha(archive), 'zip_files': sum(p.is_file() for p in package.rglob('*')),
        'exe_sha256': sha(package / EXE), 'source_archives': len(archives),
        'source_archive_bytes': sum(entry['bytes'] for entry in archives),
        'source_embedded_code_match': True, 'models_match_installer': True,
        'zip_crc_manifest_hashes': True, 'published': False,
        'publication_blocker': '22 game templates lack confirmed redistribution permission'}
    (output / 'DISTRIBUTION_RESULT.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--source-archives', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.runtime, args.source_archives, args.output), indent=2))
