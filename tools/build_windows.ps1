param([string]$Python = "python", [switch]$InstallDependencies)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BuildArguments = @("$ProjectRoot/tools/windows_package.py")
if ($InstallDependencies) { $BuildArguments += "--install-dependencies" }
& $Python @BuildArguments
if ($LASTEXITCODE -ne 0) { throw "Packaging failed; see the diagnostic above. The previous release/rollback backup is retained." }
Write-Host (Join-Path $ProjectRoot "dist/MaaMiracleNikki/MaaMiracleNikki.exe")
