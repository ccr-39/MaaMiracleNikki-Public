param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ProjectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$Target = Join-Path $ProjectRoot "assets\resource\model\ocr"
# Official PaddlePaddle ONNX revisions; see licenses/ocr/official-models-20261104.json.
# Evidence and upstream commit: THIRD_PARTY_NOTICES.md, OCR section.
$ExpectedHashes = [ordered]@{
    "det.onnx" = "d73e0058b7a8086bbd57f3d10b8bcd4ff95363f67e06e2762b5e814fe9c9410e"
    "rec.onnx" = "5435fd747c9e0efe15a96d0b378d5bd157e9492ed8fd80edf08f30d02fa24634"
    "keys.txt" = "b5f2bfe2bdd9448429e3e82b51c789775d9b42f2403d082b00662eb77e401c5d"
}

function Test-OcrDirectory([string]$Directory) {
    foreach ($Name in $ExpectedHashes.Keys) {
        $File = Join-Path $Directory $Name
        if (-not (Test-Path -LiteralPath $File -PathType Leaf)) { return $false }
        if ((Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash -ne $ExpectedHashes[$Name]) {
            return $false
        }
    }
    return $true
}

function Remove-InstallerDirectory([string]$Path, [string]$Container) {
    $Full = [System.IO.Path]::GetFullPath($Path)
    $Prefix = [System.IO.Path]::GetFullPath($Container).TrimEnd('\') + '\'
    if (-not $Full.StartsWith($Prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing cleanup outside installer directory: $Full"
    }
    if (Test-Path -LiteralPath $Full) { Remove-Item -LiteralPath $Full -Recurse -Force }
}

function Move-OcrDirectory([string]$Source, [string]$Destination) {
    # Every recursive move is confined to the target or this transaction.
    $TransactionPrefix = $TransactionRoot.TrimEnd('\') + '\'
    foreach ($Path in @($Source, $Destination)) {
        $Full = [System.IO.Path]::GetFullPath($Path)
        if ($Full -ne $Target -and -not $Full.StartsWith(
            $TransactionPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing move outside OCR transaction: $Full"
        }
    }
    if (Test-Path -LiteralPath $Destination) { throw "Move destination already exists: $Destination" }
    Move-Item -LiteralPath $Source -Destination $Destination -ErrorAction Stop
}

# Serialize installers, including separate clones, without leaving lock files.
$Mutex = [System.Threading.Mutex]::new($false, "Local\MaaMiracleNikki-OCR-Install")
$Locked = $false
$TempRoot = $null
$TransactionRoot = $null
$KeepTransaction = $false
$InstallStarted = $false
$InstallBase = Join-Path $ProjectRoot "install"
$TempBase = [System.IO.Path]::GetTempPath()
try {
    try { $Locked = $Mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $Locked = $true }
    if (-not $Locked) { throw "Another OCR installer is running; no files changed." }

    if ((Test-Path -LiteralPath $Target) -and
        ((Get-Item -LiteralPath $Target -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw "OCR target must not be a symbolic link or junction."
    }
    if ((Test-Path -LiteralPath $Target) -and -not (Test-Path -LiteralPath $Target -PathType Container)) {
        throw "OCR target must be a directory; no files changed."
    }
    if ((Test-OcrDirectory $Target) -and -not $Force) {
        Write-Host "OCR installation SHA-256 verified; download skipped."
        return
    }
    $Existing = @($ExpectedHashes.Keys | Where-Object {
        Test-Path -LiteralPath (Join-Path $Target $_)
    })
    if ($Existing.Count -gt 0 -and -not $Force) {
        throw "OCR installation is incomplete or has a SHA-256 mismatch. Use -Force to repair; no files changed."
    }
    if (Test-Path -LiteralPath $InstallBase) {
        $Recovery = @(Get-ChildItem -LiteralPath $InstallBase -Directory -Filter "ocr-*" | Where-Object {
            Test-Path -LiteralPath (Join-Path $_.FullName "previous")
        })
        if ($Recovery.Count -gt 0) {
            throw "Unresolved OCR recovery copy found: $($Recovery[0].FullName). Preserve and recover it before reinstalling."
        }
    }

    $TempRoot = Join-Path $TempBase ("maamiraclenikki-ocr-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $TempRoot -Force | Out-Null
    $Sources = @{
        "det.onnx" = "https://huggingface.co/PaddlePaddle/PP-OCRv6_small_det_onnx/resolve/28fe5895c24fd108c19eb3e8479f4ab385fbfc62/inference.onnx"
        "rec.onnx" = "https://huggingface.co/PaddlePaddle/PP-OCRv6_small_rec_onnx/resolve/b8f84f0b80c529de40b4fbb3544b84fa7233a513/inference.onnx"
        "keys.txt" = "https://raw.githubusercontent.com/MaaXYZ/MaaCommonAssets/dabcd4681ac990dc4361de26416d986abd80e4aa/OCR/ppocr_v6/small/keys.txt"
    }
    $VerifiedFiles = @{}
    foreach ($Name in $ExpectedHashes.Keys) {
        $Downloaded = Join-Path $TempRoot $Name
        Invoke-WebRequest -Uri $Sources[$Name] -OutFile $Downloaded -UseBasicParsing -TimeoutSec 120
        if ((Get-FileHash -LiteralPath $Downloaded -Algorithm SHA256).Hash -ne $ExpectedHashes[$Name]) {
            throw "SHA-256 mismatch for $Name; installation not changed."
        }
        $VerifiedFiles[$Name] = $Downloaded
    }

    # Same project volume; /install/ is Git-ignored, including recovery copies.
    $TransactionRoot = Join-Path $InstallBase ("ocr-" + [guid]::NewGuid().ToString("N"))
    $Candidate = Join-Path $TransactionRoot "candidate"
    $Backup = Join-Path $TransactionRoot "previous"
    $Failed = Join-Path $TransactionRoot "failed"
    New-Item -ItemType Directory -Path $Candidate -Force | Out-Null
    $HadTarget = Test-Path -LiteralPath $Target
    if ($HadTarget) {
        # Preserve README, hidden files and any other local directory contents.
        Get-ChildItem -LiteralPath $Target -Force | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $Candidate -Recurse -Force
        }
    }
    foreach ($Name in $ExpectedHashes.Keys) {
        Copy-Item -LiteralPath $VerifiedFiles[$Name] -Destination (Join-Path $Candidate $Name) -Force
    }
    if (-not (Test-OcrDirectory $Candidate)) { throw "Candidate verification failed; installation not changed." }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Target) -Force | Out-Null
    $InstallStarted = $true
    if ($HadTarget) { Move-OcrDirectory $Target $Backup }
    Move-OcrDirectory $Candidate $Target
    if (-not (Test-OcrDirectory $Target)) { throw "Installed OCR verification failed." }
    Write-Host "OCR models installed and SHA-256 verified in $Target"
}
catch {
    $InstallError = $_
    if ($InstallStarted) {
        try {
            if (Test-Path -LiteralPath $Backup) {
                if (Test-Path -LiteralPath $Target) { Move-OcrDirectory $Target $Failed }
                Move-OcrDirectory $Backup $Target
            }
            elseif (-not $HadTarget -and (Test-Path -LiteralPath $Target)) {
                Move-OcrDirectory $Target $Failed
            }
        }
        catch {
            $KeepTransaction = $true
            Write-Warning "Rollback failed. Preserve recovery directory: $TransactionRoot"
        }
    }
    throw $InstallError
}
finally {
    # Cleanup failure must not suppress rollback or leave the mutex held.
    try {
        if ($TempRoot) { Remove-InstallerDirectory $TempRoot $TempBase }
        if ($TransactionRoot -and -not $KeepTransaction) {
            Remove-InstallerDirectory $TransactionRoot $InstallBase
        }
    }
    finally {
        if ($Locked) { $Mutex.ReleaseMutex() }
        $Mutex.Dispose()
    }
}
