[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$localAppData = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { $repoRoot }
$diagnosticDir = Join-Path $localAppData "VoxSub\diagnostics\source-run"
$null = New-Item -ItemType Directory -Force -Path $diagnosticDir
$logPath = Join-Path $diagnosticDir "source-run-$stamp.log"

function Write-RunLog {
    param([string]$Message)
    $Message | Tee-Object -FilePath $logPath -Append
}

function Invoke-LoggedStep {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )
    Write-RunLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Name"
    try {
        & $Action 2>&1 | Tee-Object -FilePath $logPath -Append
        if ($LASTEXITCODE -ne 0) {
            throw "命令退出码 $LASTEXITCODE"
        }
    } catch {
        Write-RunLog "[失败] $Name : $($_.Exception.Message)"
        throw
    }
}

function Get-PythonCandidate {
    $launchers = @(
        @{ Exe = "py"; Args = @("-3.11") },
        @{ Exe = "py"; Args = @("-3") },
        @{ Exe = "python"; Args = @() }
    )
    foreach ($launcher in $launchers) {
        $command = Get-Command $launcher.Exe -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        try {
            $versionText = (& $command.Source @($launcher.Args) -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null).Trim()
            $version = [version]::Parse("$versionText.0")
            if ($version -ge [version]::Parse("3.11.0")) {
                return @{ Exe = $command.Source; Args = @($launcher.Args); Version = $versionText }
            }
        } catch {
            continue
        }
    }
    return $null
}

function Invoke-Python {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Python,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $Python.Exe @($Python.Args) @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python 命令失败，退出码 $LASTEXITCODE"
    }
}

try {
    Write-RunLog "VoxSub 源码测试启动"
    Write-RunLog "项目目录: $repoRoot"
    Write-RunLog "诊断日志: $logPath"

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "未找到 Git。请先安装 Git for Windows 后重试。"
    }
    Invoke-LoggedStep "更新源码 (git pull --ff-only)" {
        git -C $repoRoot pull --ff-only
    }

    $python = Get-PythonCandidate
    if (-not $python) {
        throw "未找到 Python 3.11 或更高版本。请安装 Python 3.11+ 并勾选加入 PATH。"
    }
    Write-RunLog "Python: $($python.Exe) $($python.Version)"

    $venvDir = Join-Path $repoRoot ".venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"
    $venvReady = $false
    if (Test-Path -LiteralPath $venvPython) {
        try {
            $venvVersionText = (& $venvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null).Trim()
            $venvReady = [version]::Parse("$venvVersionText.0") -ge [version]::Parse("3.11.0")
        } catch {
            $venvReady = $false
        }
    }
    if (-not $venvReady) {
        if (Test-Path -LiteralPath $venvDir) {
            $backup = Join-Path $repoRoot ".venv.invalid-$stamp"
            Move-Item -LiteralPath $venvDir -Destination $backup
            Write-RunLog "旧虚拟环境不可用，已移到: $backup"
        }
        Invoke-LoggedStep "创建本机独立虚拟环境" {
            Invoke-Python $python @("-m", "venv", $venvDir)
        }
    }

    $lockFile = Join-Path $repoRoot "requirements.lock"
    if (-not (Test-Path -LiteralPath $lockFile)) {
        throw "找不到 requirements.lock，无法安全安装依赖。"
    }
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        Invoke-LoggedStep "同步锁定依赖 (uv)" {
            uv pip sync --python $venvPython $lockFile
        }
    } else {
        Write-RunLog "未找到 uv，改用虚拟环境 pip 安装 requirements.lock。"
        Invoke-LoggedStep "升级虚拟环境 pip" {
            & $venvPython -m pip install --upgrade pip
        }
        Invoke-LoggedStep "安装锁定依赖 (pip)" {
            & $venvPython -m pip install -r $lockFile
        }
    }

    Invoke-LoggedStep "验证关键 Python 依赖" {
        & $venvPython -c "import PySide6, onnxruntime, sherpa_onnx, sentry_sdk"
    }

    # Never inherit a foreign Python runtime configuration into the app.
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
    $env:VOXSUB_ENVIRONMENT = "testing"

    # DSN is optional and never stored in this repository.  An owner may place
    # it once in the user profile so friends do not need to type it manually.
    $dsnFile = Join-Path $localAppData "VoxSub\sentry_dsn.txt"
    if (Test-Path -LiteralPath $dsnFile) {
        $dsn = (Get-Content -LiteralPath $dsnFile -Raw).Trim()
        if ($dsn) { $env:VOXSUB_SENTRY_DSN = $dsn }
    }

    Invoke-LoggedStep "启动 VoxSub 测试版" {
        & $venvPython (Join-Path $repoRoot "run_app.py")
    }
    Write-RunLog "VoxSub 已退出。"
    exit 0
} catch {
    $friendly = $_.Exception.Message
    Write-RunLog ""
    Write-RunLog "[错误] $friendly"
    Write-RunLog "详细日志: $logPath"
    Write-Host ""
    Write-Host "VoxSub 测试版启动失败：$friendly" -ForegroundColor Red
    Write-Host "详细日志已保存：$logPath" -ForegroundColor Yellow
    exit 1
}
