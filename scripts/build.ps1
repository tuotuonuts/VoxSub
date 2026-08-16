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

# 3) self-sign the exe via osslsigncode (PowerShell Set-AuthenticodeSignature
#    fails with UnknownError for self-signed certs on this machine; osslsigncode
#    is the reliable path. Formal OV cert reuses this pipeline.)
function Find-DevCert {
    Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
        Where-Object { $_.Subject -like "*VoxSub*" } | Select-Object -First 1
}
function Find-SignTool {
    $cands = @($env:OSSLSIGNCODE,
               (Join-Path $env:LOCALAPPDATA "VoxSub\tools\osslsigncode.exe"))
    foreach ($c in $cands) { if ($c -and (Test-Path $c)) { return $c } }
    return $null
}
$Exe = Join-Path $Dist "VoxSub.exe"
$SignTool = Find-SignTool
$Cert = Find-DevCert
if ($SignTool -and $Cert) {
    $DevPw = "VoxSubDev2026!"          # dev-only self-signed password
    $PfxOut = Join-Path $env:TEMP "voxsub_dev.pfx"
    $TmpOut = Join-Path $env:TEMP "VoxSub_signed.exe"
    $sec = ConvertTo-SecureString $DevPw -AsPlainText -Force
    [void](Export-PfxCertificate -Cert $Cert.Cert.PsPath -FilePath $PfxOut -Password $sec)
    Run-Checked "self-sign (osslsigncode)" {
        & $SignTool sign -pkcs12 $PfxOut -pass $DevPw -h sha256 `
            -t http://timestamp.digicert.com -in $Exe -out $TmpOut
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[sign] timestamp failed, retry without timestamp"
            & $SignTool sign -pkcs12 $PfxOut -pass $DevPw -h sha256 -in $Exe -out $TmpOut
        }
        Remove-Item $PfxOut -ErrorAction SilentlyContinue
    }
    Move-Item -Force $TmpOut $Exe
    $r = Get-AuthenticodeSignature -FilePath $Exe
    if ($r.SignerCertificate) {
        Write-Host "[sign] done, signer: $($r.SignerCertificate.Subject)" -ForegroundColor Green
        Write-Host "[sign] status=$($r.Status) (NotTrusted/UnknownError expected for self-signed; OV cert will give Valid)"
    } else {
        Write-Host "[sign] WARNING: no signer readable" -ForegroundColor Yellow
    }
} else {
    Write-Host "[sign] SKIPPED: osslsigncode or dev cert missing" -ForegroundColor Yellow
}

# 4) summary
$SizeMB = [math]::Round(((Get-ChildItem $Dist -Recurse | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "[build] OK -> $Dist ($SizeMB MB)" -ForegroundColor Green
$r = Get-AuthenticodeSignature -FilePath $Exe
if ($r.SignerCertificate) {
    Write-Host "[build] signed: $($r.SignerCertificate.Subject)"
} else {
    Write-Host "[build] NOT signed (install osslsigncode + create dev cert for self-sign)"
}

# 5) InnoSetup detection
if (-not (Get-Command iscc -ErrorAction SilentlyContinue)) {
    Write-Host "[build] InnoSetup not found: installer .exe skipped."
    Write-Host "         Install: https://jrsoftware.org/isdl.php (then rerun with installer step)"
} else {
    Write-Host "[build] InnoSetup available; add installer step in M9."
}