# VoxSub build script (ASCII only comments!)
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build.ps1          # full build
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build.ps1 -SkipTests
# Steps: unit tests -> PyInstaller (onedir, windowed) -> self-sign -> dist summary
# Prereqs: python venv with pyinstaller; ffmpeg optional (runtime only);
#          InnoSetup optional (installer exe, detect at the end).
param([switch]$SkipTests)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Run-Checked([string]$Label, [scriptblock]$Block) {
    Write-Host "[build] $Label ..." -ForegroundColor Cyan
    & $Block
    if ($LASTEXITCODE -ne 0) { throw "$Label failed (exit $LASTEXITCODE)" }
}

# 1) tests
if (-not $SkipTests) {
    Run-Checked "pytest" {
        & ".venv\Scripts\python.exe" -m pytest tests/ -q
    }
}

# 2) PyInstaller onedir build (windowed GUI app)
$Dist = Join-Path $Root "dist\VoxSub"
if (Test-Path $Dist) { Remove-Item $Dist -Recurse -Force }
Run-Checked "pyinstaller" {
    & ".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed `
        --name VoxSub `
        --icon "assets\icon.ico" `
        --add-data "assets;assets" `
        --collect-all sherpa_onnx `
        --collect-all soundcard `
        --collect-all onnxruntime `
        --collect-all qfluentwidgets `
        --hidden-import voxsub.pipeline `
        --hidden-import voxsub.translate.factory `
        run_app.py
}

# 3) self-sign the exe (formal OV cert replaces this in release pipeline)
$Exe = Join-Path $Dist "VoxSub.exe"
Run-Checked "self-sign" {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\sign.ps1" sign $Exe
}

# 4) summary
$SizeMB = [math]::Round(((Get-ChildItem $Dist -Recurse | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "[build] OK -> $Dist ($SizeMB MB)" -ForegroundColor Green
[void](& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\sign.ps1" verify $Exe)

# 5) InnoSetup detection
if (-not (Get-Command iscc -ErrorAction SilentlyContinue)) {
    Write-Host "[build] InnoSetup not found: installer .exe skipped."
    Write-Host "         Install: https://jrsoftware.org/isdl.php (then rerun with installer step)"
} else {
    Write-Host "[build] InnoSetup available; add installer step in M9."
}