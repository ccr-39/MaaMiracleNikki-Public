param(
    [string]$Exe = (Join-Path (Split-Path -Parent $PSScriptRoot) "dist/MaaMiracleNikki/MaaMiracleNikki.exe"),
    [string]$ReportPath = ""
)
$ErrorActionPreference = "Stop"
$Exe = (Resolve-Path -LiteralPath $Exe).Path
$PreviousData = $env:MMN_DATA_DIR
$CheckRoot = Join-Path ([IO.Path]::GetTempPath()) ("MMN-offline-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $CheckRoot | Out-Null
$Passed = $false
try {
    # Verify a clean copy so even an existing portable profile stays untouched.
    $ProgramCopy = Join-Path $CheckRoot "program"
    New-Item -ItemType Directory -Path $ProgramCopy | Out-Null
    Copy-Item -LiteralPath $Exe -Destination $ProgramCopy
    Copy-Item -LiteralPath (Join-Path (Split-Path $Exe) "_internal") -Destination $ProgramCopy -Recurse
    $Profile = Join-Path $ProgramCopy "userdata"
    $Process = Start-Process -FilePath (Join-Path $ProgramCopy "MaaMiracleNikki.exe") -ArgumentList "--offline-check" -WorkingDirectory $CheckRoot -WindowStyle Hidden -PassThru
    if (-not $Process.WaitForExit(60000)) { throw "Offline check timed out; inspect process and logs" }
    if ($Process.ExitCode -ne 0) { throw "Offline check failed; logs: $Profile/logs" }
    $Report = Get-Content -Raw -LiteralPath "$Profile/logs/offline-check.json" | ConvertFrom-Json
    if (-not $Report.ok -or $Report.tasks -ne 14 -or -not $Report.gui_created) { throw "Invalid offline report" }
    if ($ReportPath) {
        $Report | ConvertTo-Json | Set-Content -LiteralPath $ReportPath -Encoding UTF8
    }
    $Report | ConvertTo-Json
    $Passed = $true
} finally {
    $env:MMN_DATA_DIR = $PreviousData
    if ($Passed) {
        $ResolvedCheck = [IO.Path]::GetFullPath($CheckRoot)
        $TempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
        if (-not $ResolvedCheck.StartsWith($TempBase, [StringComparison]::OrdinalIgnoreCase) -or
            -not ([IO.Path]::GetFileName($ResolvedCheck).StartsWith("MMN-offline-"))) {
            throw "Unsafe verification cleanup path: $ResolvedCheck"
        }
        Remove-Item -LiteralPath $ResolvedCheck -Recurse -Force
    } else {
        Write-Warning "Verification diagnostics retained: $CheckRoot"
    }
}
