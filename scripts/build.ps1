






param([switch]$SkipTests, [switch]$SkipPyInstaller)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Version = "0.9.0-beta"
$LlamaVersion = "b10470"  # 2026-08-18 official latest; pinned for reproducible builds
Set-Location $Root

function Run-Checked([string]$Label, [scriptblock]$Block) {
    Write-Host "[build] $Label ..." -ForegroundColor Cyan
    & $Block
    if ($LASTEXITCODE -ne 0) { throw "$Label failed (exit $LASTEXITCODE)" }
}


if (-not $SkipTests) {
    # Use a unique D-drive folder inside the workspace. OCR cache policy tests
    # intentionally reject drive C, while uniqueness still avoids OneDrive
    # retaining a handle from a previous pytest run.
    $TestBaseTemp = Join-Path $Root (
        ".pytest-build-" + [guid]::NewGuid().ToString("N"))
    try {
        Run-Checked "pytest" {
            & ".venv\Scripts\python.exe" -m pytest tests/ -q --basetemp $TestBaseTemp
        }
    } finally {
        $ResolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
        $ResolvedTestBase = [IO.Path]::GetFullPath($TestBaseTemp)
        if (-not $ResolvedTestBase.StartsWith(
                $ResolvedRoot + ".pytest-build-",
                [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean unexpected pytest path: $ResolvedTestBase"
        }
        if (Test-Path -LiteralPath $ResolvedTestBase) {
            Remove-Item -LiteralPath $ResolvedTestBase -Recurse -Force
        }
    }
}







Set-Location $Root
Write-Host "[build] root = $Root" -ForegroundColor Cyan
$Dist = Join-Path $Root "dist\VoxSub"
Write-Host "[build] dist = $Dist" -ForegroundColor Cyan

$ReleaseDir = Join-Path $Root "..\Release"
Write-Host "[build] release = $ReleaseDir" -ForegroundColor Cyan
if (-not (Test-Path $ReleaseDir)) { New-Item -ItemType Directory -Path $ReleaseDir | Out-Null }
$Work = Join-Path $env:TEMP "VoxSub_pybuild"
$Icon = Join-Path $Root "assets\icon.ico"
$SpecDir = Join-Path $Root "build"
if (-not $SkipPyInstaller) {
    if (Test-Path $Dist) { Remove-Item $Dist -Recurse -Force }
    if (Test-Path $Work) { Remove-Item $Work -Recurse -Force }
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
            "--collect-all", "pyaudiowpatch",
            "--collect-all", "recap",
            "--collect-all", "comtypes",
            "--collect-all", "psutil",
            "--collect-all", "onnxruntime",
            "--collect-all", "rapidocr",
            "--hidden-import", "rapidocr.main",
            "--collect-all", "cv2",
            "--collect-all", "qfluentwidgets",
            "--hidden-import", "voxsub.pipeline",
            "--hidden-import", "voxsub.diagnostics",
            "--hidden-import", "voxsub.process_audio",
            "--hidden-import", "voxsub.model_catalog",
            "--hidden-import", "voxsub.ocr",
            "--hidden-import", "voxsub.translate.factory",
            "--hidden-import", "voxsub.ui.model_hub_window",
            "--hidden-import", "voxsub.ui.ocr_workspace",
            "run_app.py"
        )
    }
} elseif (-not (Test-Path (Join-Path $Dist "VoxSub.exe"))) {
    throw "-SkipPyInstaller requested but existing dist output is missing"
}

# Every recognizer, including Marketplace ASR models, needs Silero VAD.  Keep
# this small shared dependency inside the installer instead of requiring a
# hidden first-run download that leaves a new machine unable to start.
$BootstrapVad = Join-Path $Root "assets\bootstrap_models\vad\silero_vad_v5.onnx"
$BootstrapVadSha256 = "6B99CBFD39246B6706F98EC13C7C50C6B299181F2474FA05CBC8046ACC274396"
if (-not (Test-Path $BootstrapVad)) {
    throw "bundled VAD asset missing: $BootstrapVad"
}
if ((Get-FileHash -LiteralPath $BootstrapVad -Algorithm SHA256).Hash -ne $BootstrapVadSha256) {
    throw "bundled VAD asset SHA256 mismatch"
}
$BootstrapVadDest = Join-Path $Dist "models_base\vad"
New-Item -ItemType Directory -Path $BootstrapVadDest -Force | Out-Null
Copy-Item -LiteralPath $BootstrapVad -Destination (Join-Path $BootstrapVadDest "silero_vad_v5.onnx") -Force
Write-Host "[build] bundled base VAD -> $BootstrapVadDest" -ForegroundColor Green

# GGUF runtime matrix.  End users receive all three small backends so runtime
# selection can follow discrete GPU -> Intel NPU -> integrated GPU -> CPU
# without downloading executables after installation.
$LlamaAssets = @(
    @{
        Name = "cpu";
        File = "llama-$LlamaVersion-bin-win-cpu-x64.zip";
        Sha256 = "A31F1F317813AE7E044BE183E0A20B90E78A80C0E97EE11A8B32A014ECCD5043"
    },
    @{
        Name = "vulkan";
        File = "llama-$LlamaVersion-bin-win-vulkan-x64.zip";
        Sha256 = "2E89637B30E0E2F90D4ED486118E8642F60625B1DBEBB9BA3A30BC4100306FC9"
    }
)
$LlamaCache = Join-Path $env:TEMP "VoxSub_llama_$LlamaVersion"
$LlamaDest = Join-Path $Dist "tools\llama"
New-Item -ItemType Directory -Path $LlamaCache -Force | Out-Null
New-Item -ItemType Directory -Path $LlamaDest -Force | Out-Null
foreach ($Asset in $LlamaAssets) {
    $Zip = Join-Path $LlamaCache $Asset.File
    $Url = "https://github.com/ggml-org/llama.cpp/releases/download/$LlamaVersion/$($Asset.File)"
    if ((-not (Test-Path $Zip)) -or
        ((Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash -ne $Asset.Sha256)) {
        Invoke-WebRequest -Uri $Url -OutFile $Zip -UseBasicParsing
    }
    if ((Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash -ne $Asset.Sha256) {
        throw "llama.cpp runtime SHA256 mismatch: $($Asset.File)"
    }
    $Extract = Join-Path $LlamaCache $Asset.Name
    if (Test-Path $Extract) { Remove-Item -LiteralPath $Extract -Recurse -Force }
    Expand-Archive -LiteralPath $Zip -DestinationPath $Extract -Force
    $BackendDest = Join-Path $LlamaDest $Asset.Name
    if (Test-Path $BackendDest) { Remove-Item -LiteralPath $BackendDest -Recurse -Force }
    New-Item -ItemType Directory -Path $BackendDest -Force | Out-Null
    Copy-Item -Path (Join-Path $Extract "*") -Destination $BackendDest -Recurse -Force
}

# The official OpenVINO archive still contains a private NPUW compile option
# that is incompatible with the Intel NPU driver used by our hardware runner.
# Use the source-built no-NPUW runtime that passed the real NPU probe.  The
# override is useful for CI and for a locally cached verified runtime; a clean
# build falls back to the reproducible source builder.
$NpuRuntimeDir = $env:VOXSUB_NPU_RUNTIME_DIR
if (-not $NpuRuntimeDir) {
    $NpuRuntimeDir = Join-Path $env:TEMP "VoxSub_npu_runtime_$LlamaVersion"
}
$NpuRequired = @(
    "llama-server.exe",
    "ggml-openvino.dll",
    "openvino_intel_npu_plugin.dll",
    "runtime-dependencies.txt"
)
$NpuReady = (@($NpuRequired | Where-Object {
    Test-Path -LiteralPath (Join-Path $NpuRuntimeDir $_) -PathType Leaf
}).Count -eq $NpuRequired.Count)
if (-not $NpuReady) {
    Run-Checked "build no-NPUW OpenVINO runtime" {
        & (Join-Path $Root "scripts\build_npu_runtime.ps1") `
            -OutputDir $NpuRuntimeDir `
            -CacheRoot (Join-Path $env:TEMP "VoxSub_npu_source_$LlamaVersion")
    }
}
$NpuReady = (@($NpuRequired | Where-Object {
    Test-Path -LiteralPath (Join-Path $NpuRuntimeDir $_) -PathType Leaf
}).Count -eq $NpuRequired.Count)
if (-not $NpuReady) {
    throw "NPU-compatible OpenVINO runtime is incomplete: $NpuRuntimeDir"
}
$NpuDest = Join-Path $LlamaDest "openvino"
if (Test-Path -LiteralPath $NpuDest) { Remove-Item -LiteralPath $NpuDest -Recurse -Force }
New-Item -ItemType Directory -Path $NpuDest -Force | Out-Null
Copy-Item -Path (Join-Path $NpuRuntimeDir "*") -Destination $NpuDest -Recurse -Force
Write-Host "[build] bundled llama.cpp $LlamaVersion CPU/Vulkan/no-NPUW-OpenVINO -> $LlamaDest" -ForegroundColor Green

# C 模式必须开箱即用：将 ffmpeg 作为独立工具随 onedir 分发，并附许可证。
$FfmpegPath = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
if (-not $FfmpegPath) {
    $FfmpegCandidates = @(
        "C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        (Join-Path $env:LOCALAPPDATA "VoxSub\tools\ffmpeg.exe")
    )
    $FfmpegPath = $FfmpegCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $FfmpegPath) { throw "ffmpeg not found; cannot build video import support" }
$ToolsDir = Join-Path $Dist "tools"
New-Item -ItemType Directory -Path $ToolsDir -Force | Out-Null
Copy-Item -LiteralPath $FfmpegPath -Destination (Join-Path $ToolsDir "ffmpeg.exe") -Force
$FfmpegRoot = Split-Path -Parent (Split-Path -Parent $FfmpegPath)
$FfmpegLicense = Join-Path $FfmpegRoot "LICENSE"
if (Test-Path $FfmpegLicense) {
    Copy-Item -LiteralPath $FfmpegLicense -Destination (Join-Path $ToolsDir "FFMPEG_LICENSE.txt") -Force
}
Write-Host "[build] bundled ffmpeg -> $ToolsDir" -ForegroundColor Green




function Find-DevCert {
    Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
        Where-Object { $_.Subject -like "*VoxSub*" -and $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date) } |
        Sort-Object NotAfter -Descending |
        Select-Object -First 1
}
function Sign-Artifact([string]$Path, $Certificate) {
    if (-not $Certificate) {
        Write-Host "[sign] SKIPPED: VoxSub code-signing certificate not found" -ForegroundColor Yellow
        return
    }
    if (-not (Test-Path -LiteralPath $Path)) { throw "要签名的文件不存在: $Path" }

    Write-Host "[sign] signing $(Split-Path -Leaf $Path) from CurrentUser certificate store ..." -ForegroundColor Cyan
    $Signature = Set-AuthenticodeSignature `
        -FilePath $Path `
        -Certificate $Certificate `
        -HashAlgorithm SHA256 `
        -TimestampServer "http://timestamp.digicert.com"
    if (-not $Signature.SignerCertificate) { throw "签名失败: $Path ($($Signature.StatusMessage))" }

    Write-Host "[sign] signer=$($Signature.SignerCertificate.Subject)" -ForegroundColor Green
    Write-Host "[sign] status=$($Signature.Status) (self-signed certificate may report NotTrusted/UnknownError)"
}
$Exe = Join-Path $Dist "VoxSub.exe"
Write-Host "[build] packaged OCR smoke ..." -ForegroundColor Cyan
# VoxSub is built as a Windows GUI executable, so invoking it with `&` does not
# reliably populate $LASTEXITCODE. Read the real process exit code explicitly;
# otherwise a successful smoke test is mistaken for `exit $null` and the
# installer stage is skipped.
$OcrSmoke = Start-Process -FilePath $Exe -ArgumentList "--ocr-smoke" `
    -WindowStyle Hidden -Wait -PassThru
if ($OcrSmoke.ExitCode -ne 0) {
    throw "packaged OCR smoke failed (exit $($OcrSmoke.ExitCode))"
}
$Cert = Find-DevCert
Sign-Artifact -Path $Exe -Certificate $Cert


$SizeMB = [math]::Round(((Get-ChildItem $Dist -Recurse | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "[build] OK -> $Dist ($SizeMB MB)" -ForegroundColor Green
$r = Get-AuthenticodeSignature -FilePath $Exe
if ($r.SignerCertificate) {
    Write-Host "[build] signed: $($r.SignerCertificate.Subject)"
} else {
    Write-Host "[build] NOT signed (create/import a CurrentUser code-signing certificate)"
}


$Iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $Iscc) {
    $IsccCandidates = @(
        "C:\Program Files\Inno Setup 7\ISCC.exe",
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    )
    $Iscc = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Iscc) {
    Write-Host "[build] InnoSetup not found: installer .exe skipped."
    Write-Host "         Install: https://jrsoftware.org/isdl.php (then rerun with installer step)"
} else {
    Run-Checked "Inno Setup installer" {
        & $Iscc (Join-Path $Root "scripts\installer.iss")
    }
    $Setup = Join-Path $ReleaseDir "VoxSub-Setup-$Version.exe"
    if (-not (Test-Path $Setup)) { throw "installer output missing: $Setup" }
    Sign-Artifact -Path $Setup -Certificate $Cert
    $SetupHash = (Get-FileHash -LiteralPath $Setup -Algorithm SHA256).Hash
    $HashPath = "$Setup.sha256"
    Set-Content -LiteralPath $HashPath -Value "$SetupHash  $(Split-Path -Leaf $Setup)" -Encoding ASCII
    Write-Host "[build] installer -> $Setup" -ForegroundColor Green
    Write-Host "[build] sha256   -> $HashPath" -ForegroundColor Green
}
