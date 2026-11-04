# Build/deploy via tools/build_windows.ps1 (private staging, checks and rollback).
from pathlib import Path
import hashlib
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH)
# Public packages support Windows 10/11 x64. UCRT is provided by Windows;
# MMN uses OpenCV image APIs and does not need its optional FFmpeg plugin.
def public_runtime_files(toc):
    return [entry for entry in toc
            if not Path(entry[0]).name.lower().startswith(("opencv_videoio_ffmpeg", "api-ms-win-"))
            and Path(entry[0]).name.lower() != "ucrtbase.dll"]

maa_data, maa_bins, maa_imports = collect_all("maa")
ocr_hashes = {
    "det.onnx": "d73e0058b7a8086bbd57f3d10b8bcd4ff95363f67e06e2762b5e814fe9c9410e",
    "rec.onnx": "5435fd747c9e0efe15a96d0b378d5bd157e9492ed8fd80edf08f30d02fa24634",
    "keys.txt": "b5f2bfe2bdd9448429e3e82b51c789775d9b42f2403d082b00662eb77e401c5d",
}
for name, expected in ocr_hashes.items():
    model = root / "assets/resource/model/ocr" / name
    if not model.is_file() or hashlib.sha256(model.read_bytes()).hexdigest() != expected:
        raise RuntimeError("Missing or outdated OCR asset; review tools/setup_ocr.ps1 (-Force only for an intentional migration): " + name)
# Agent source files preserve __file__ resource paths and dynamic bare imports.
datas = maa_data + [(str(root / "assets"), "assets")]
datas += [(str(p), "agent") for p in (root / "agent").glob("*.py")]
datas += [(str(root / "gui/icons"), "gui/icons")]
datas += [(str(root / name), ".") for name in ("LICENSE", "THIRD_PARTY_NOTICES.md")]
agent_imports = [p.stem for p in (root / "agent").glob("*.py") if p.stem != "main"]
a = Analysis([str(root / "packaging/launcher.py")], pathex=[str(root), str(root / "agent")],
             binaries=maa_bins, datas=datas, hiddenimports=maa_imports + agent_imports + ["tools.run_task"],
             excludes=["PySide6.QtTest", "tkinter", "matplotlib", "pytest", "PyInstaller"])
a.binaries = public_runtime_files(a.binaries)
a.datas = public_runtime_files(a.datas)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="MaaMiracleNikki", console=False, upx=False)
# The updater is stdlib-only, onefile and copied outside the installed onedir
# before launch, so it never holds a DLL or EXE inside the folder being moved.
updater_a = Analysis([str(root / "packaging/updater_entry.py")], pathex=[str(root)],
                     binaries=[], datas=[], hiddenimports=[],
                     excludes=["PySide6", "maa", "cv2", "numpy", "pytest", "PyInstaller"])
updater_a.binaries = public_runtime_files(updater_a.binaries)
updater_a.datas = public_runtime_files(updater_a.datas)
updater_pyz = PYZ(updater_a.pure)
updater_exe = EXE(updater_pyz, updater_a.scripts, updater_a.binaries, updater_a.datas,
                  name="MMNUpdater", console=False, upx=False)
# COLLECT copies the independently built helper alongside the main EXE.
coll = COLLECT(exe, updater_exe, a.binaries, a.datas,
               name="MaaMiracleNikki", upx=False)
