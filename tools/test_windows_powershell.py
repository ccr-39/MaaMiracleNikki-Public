"""Offline regression for pwsh -> Python -> Windows PowerShell module paths."""
import hashlib
import os
import shutil
import subprocess

import pytest

from tools.windows_powershell import windows_powershell_env


@pytest.mark.parametrize("key", ["PSModulePath", "PSMODULEPATH", "psmodulepath"])
def test_child_environment_preserves_settings_without_inherited_module_paths(monkeypatch, key):
    monkeypatch.setenv(key, "incompatible-pwsh-modules")
    monkeypatch.setenv("MMN_DATA_DIR", "isolated-runtime-data")
    before = dict(os.environ)
    child = windows_powershell_env()
    assert child == {k: v for k, v in before.items() if k.upper() != "PSMODULEPATH"}
    assert dict(os.environ) == before
    assert child["MMN_DATA_DIR"] == "isolated-runtime-data"


def test_windows_powershell_rebuilds_module_paths_and_hashes_real_file(tmp_path, monkeypatch):
    shell = shutil.which("powershell.exe")
    if not shell:
        pytest.skip("Windows PowerShell is required; no network fallback")
    # Simulate a module whose manifest is discoverable in WinPS but whose
    # implementation requires PS7 (as opposed to a manifest WinPS can skip).
    # No machine modules are changed.
    module = tmp_path / "pwsh-modules" / "Microsoft.PowerShell.Utility"
    module.mkdir(parents=True)
    (module / "Microsoft.PowerShell.Utility.psd1").write_text(
        "@{ModuleVersion='7.0.0'; PowerShellVersion='3.0'; "
        "RootModule='Utility.psm1'; FunctionsToExport=@('Get-FileHash')}", encoding="ascii")
    (module / "Utility.psm1").write_text("throw 'Incompatible module must not execute'", encoding="ascii")
    monkeypatch.setenv("PSModulePath", str(module.parent))
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"offline SHA256 compatibility fixture")
    script = tmp_path / "check.ps1"
    script.write_text(
        '$ErrorActionPreference = "Stop"\n'
        f"[Console]::WriteLine((Get-FileHash -LiteralPath '{str(payload).replace(chr(39), chr(39)*2)}' "
        "-Algorithm SHA256).Hash)\n"
        # These cmdlets are also needed by OCR extraction / package guards.
        "Get-Command Expand-Archive, Get-CimInstance, ConvertTo-Json -ErrorAction Stop | Out-Null\n",
        encoding="utf-8-sig")
    command = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    broken = subprocess.run(command, capture_output=True, timeout=30)
    assert broken.returncode != 0
    assert b"Get-FileHash" in broken.stdout + broken.stderr
    fixed = subprocess.run(command, capture_output=True, env=windows_powershell_env(), timeout=30)
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    assert fixed.stdout.strip().decode("ascii").lower() == hashlib.sha256(payload.read_bytes()).hexdigest()
