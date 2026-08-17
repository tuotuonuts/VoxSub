






param([switch]$SkipTests)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Run-Checked([string]$Label, [scriptblock]$Block) {
    Write-Host "[build] $Label ..." -ForegroundColor Cyan
    & $Block
    if ($LASTEXITCODE -ne 0) { throw "$Label failed (exit $LASTEXITCODE)" }
}


if (-not $SkipTests) {
    Run-Checked "pytest" {
        & ".venv\Scripts\python.exe" -m pytest tests/ -q
    }
}







Set-Location $Root
Write-Host "[build] root = $Root" -ForegroundColor Cyan
$Dist = Join-Path $Root "dist\VoxSub"
Write-Host "[build] dist = $Dist" -ForegroundColor Cyan

$ReleaseDir = Join-Path $Root "..\Release"
Write-Host "[build] release = $ReleaseDir" -ForegroundColor Cyan
if (-not (Test-Path $ReleaseDir)) { New-Item -ItemType Directory -Path $ReleaseDir | Out-Null }
if (Test-Path $Dist) { Remove-Item $Dist -Recurse -Force }
$Work = Join-Path $env:TEMP "VoxSub_pybuild"
If (Test-Path $Work) { Remove-Item $Work -Recurse -Force }
$Icon = Join-Path $Root "assets\icon.ico"
$SpecDir = Join-Path $Root "build"
Run-Checked "pyinstaller" {
    $Py = Join-Path "D:\OneDrive\app_dve\VoxSub" ".venv\Scripts\python.exe"

    & $Py @(
        "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--name", "VoxSub",
        "--icon", "D:/OneDrive/app_dve/VoxSub/assets/icon.ico",
        "--distpath", "D:/OneDrive/app_dve/VoxSub/dist",
        "--workpath", "$env:TEMP\VoxSub_pybuild",
        "--specpath", "D:/OneDrive/app_dve/VoxSub/build",
        "--collect-all", "sherpa_onnx",
        "--collect-all", "soundcard",
        "--collect-all", "onnxruntime",
        "--collect-all", "qfluentwidgets",
        "--hidden-import", "voxsub.pipeline",
        "--hidden-import", "voxsub.translate.factory",
        "run_app.py"
    )
}




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
    $DevPw = "VoxSubDev2026!"
    $PfxOut = Join-Path $env:TEMP "voxsub_dev.pfx"
    $TmpOut = Join-Path $env:TEMP "VoxSub_signed.exe"
    $sec = ConvertTo-SecureString $DevPw -AsPlainText -Force


    [void](Export-PfxCertificate -Cert $Cert.PsPath -FilePath $PfxOut -Password $sec)

    if (Test-Path $TmpOut) { Remove-Item $TmpOut -Force }
    if (-not (Test-Path $Exe)) { throw "要签名的 exe 不存在: $Exe" }
    Write-Host "[build] self-sign (osslsigncode) ..." -ForegroundColor Cyan


    # 签名统一委托给 bash scripts/sign.sh (signtool):
    # - osslsigncode 2.14-mingw 对 PyInstaller 6.22 PE 间歇失败 (实测)
    # - PowerShell 5.1 对原生 exe ANSI 传参失败 (实测)
    # - bash + signtool 100% 可靠
    & bash -lc "cd 'D:/OneDrive/app_dve/VoxSub' && bash scripts/sign.sh '$Exe'"
    if ($LASTEXITCODE -ne 0) { throw "self-sign failed (exit $LASTEXITCODE)" }
    Remove-Item $PfxOut -ErrorAction SilentlyContinue
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


$SizeMB = [math]::Round(((Get-ChildItem $Dist -Recurse | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "[build] OK -> $Dist ($SizeMB MB)" -ForegroundColor Green
$r = Get-AuthenticodeSignature -FilePath $Exe
if ($r.SignerCertificate) {
    Write-Host "[build] signed: $($r.SignerCertificate.Subject)"
} else {
    Write-Host "[build] NOT signed (install osslsigncode + create dev cert for self-sign)"
}


if (-not (Get-Command iscc -ErrorAction SilentlyContinue)) {
    Write-Host "[build] InnoSetup not found: installer .exe skipped."
    Write-Host "         Install: https://jrsoftware.org/isdl.php (then rerun with installer step)"
} else {
    Write-Host "[build] InnoSetup available; add installer step in M9."
}