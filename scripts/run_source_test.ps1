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
    $previousErrorPreference = $ErrorActionPreference
    try {
        # Native tools such as uv commonly write progress to stderr even on
        # success. Normalize both streams into the log and judge success only
        # by the native process exit code.
        $ErrorActionPreference = "Continue"
        & $Action 2>&1 |
            ForEach-Object {
                if ($_ -is [System.Management.Automation.ErrorRecord]) {
                    $_.Exception.Message
                } else {
                    [string]$_
                }
            } |
            Tee-Object -FilePath $logPath -Append
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            throw "命令退出码 $exitCode"
        }
    } catch {
        Write-RunLog "[失败] $Name : $($_.Exception.Message)"
        throw
    } finally {
        $ErrorActionPreference = $previousErrorPreference
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

function Move-VenvAside {
    param([Parameter(Mandatory = $true)][string]$VenvDir)

    if (-not (Test-Path -LiteralPath $VenvDir)) {
        return
    }
    $backup = Join-Path $repoRoot ".venv.invalid-$stamp"
    if (Test-Path -LiteralPath $backup) {
        $backup = Join-Path $repoRoot (".venv.invalid-" + (Get-Date -Format "yyyyMMdd-HHmmssfff"))
    }
    try {
        Move-Item -LiteralPath $VenvDir -Destination $backup -ErrorAction Stop
    } catch {
        throw "无法替换旧虚拟环境，可能仍被 Python、IDE 或同步软件占用。请关闭相关程序后重试。原始错误：$($_.Exception.Message)"
    }
    Write-RunLog "旧虚拟环境已移到备份目录: $backup"
}

function New-LocalVenv {
    param([switch]$WithoutPip)

    $arguments = @("-m", "venv")
    if ($WithoutPip) {
        $arguments += "--without-pip"
    }
    $arguments += $venvDir
    Invoke-LoggedStep "创建本机独立虚拟环境" {
        Invoke-Python $python $arguments
    }
}

function Get-VoxSubProcesses {
    <# Return only processes that can keep this checkout or its runtime busy. #>
    $repoMarker = $repoRoot.ToLowerInvariant()
    try {
        $processes = Get-CimInstance -ClassName Win32_Process -ErrorAction Stop
    } catch {
        throw "无法检查正在运行的 VoxSub 进程：$($_.Exception.Message)"
    }
    foreach ($process in $processes) {
        $name = ([string]$process.Name).ToLowerInvariant()
        $commandLine = ([string]$process.CommandLine).ToLowerInvariant()
        $isVoxSub = $name -eq "voxsub.exe"
        $isPython = $name -in @("python.exe", "pythonw.exe") -and (
            $commandLine.Contains($repoMarker) -or
            $commandLine.Contains("run_app.py") -or
            $commandLine.Contains("voxsub.ui.app")
        )
        $isLlama = $name -eq "llama-server.exe"
        if ($isVoxSub -or $isPython -or $isLlama) {
            [pscustomobject]@{
                Id = [int]$process.ProcessId
                Name = [string]$process.Name
                CommandLine = ([string]$process.CommandLine).Trim()
            }
        }
    }
}

function Wait-VoxSubProcessesExit {
    param([int]$TimeoutSeconds = 30)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $remaining = @(Get-VoxSubProcesses)
        if ($remaining.Count -eq 0) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    $remaining = @(Get-VoxSubProcesses)
    foreach ($process in $remaining) {
        Write-RunLog "仍在运行: $($process.Name) (PID $($process.Id)) $($process.CommandLine)"
    }
    return $false
}

try {
    Write-RunLog "VoxSub 源码测试启动"
    Write-RunLog "项目目录: $repoRoot"
    Write-RunLog "诊断日志: $logPath"

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "未找到 Git。请先安装 Git for Windows 后重试。"
    }

    $running = @(Get-VoxSubProcesses)
    if ($running.Count -gt 0) {
        foreach ($process in $running) {
            Write-RunLog "启动前检测到进程: $($process.Name) (PID $($process.Id)) $($process.CommandLine)"
        }
        Write-Host "检测到 VoxSub 或其相关子进程仍在运行。" -ForegroundColor Yellow
        Write-Host "请先右键系统托盘图标，选择“退出应用”，然后重新双击本脚本。" -ForegroundColor Yellow
        Write-RunLog "未执行 git pull：请先从托盘菜单选择“退出应用”。"
        exit 2
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
    $uv = Get-Command uv -ErrorAction SilentlyContinue
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
        Move-VenvAside $venvDir
        if ($uv) {
            New-LocalVenv -WithoutPip
        } else {
            New-LocalVenv
        }
    }

    $lockFile = Join-Path $repoRoot "requirements.lock"
    if (-not (Test-Path -LiteralPath $lockFile)) {
        throw "找不到 requirements.lock，无法安全安装依赖。"
    }
    if ($uv) {
        try {
            Invoke-LoggedStep "同步锁定依赖 (uv)" {
                uv pip sync --python $venvPython $lockFile
            }
        } catch {
            Write-RunLog "[警告] 现有虚拟环境同步失败，将备份并重建本机环境。"
            Move-VenvAside $venvDir
            New-LocalVenv -WithoutPip
            $venvPython = Join-Path $venvDir "Scripts\python.exe"
            Invoke-LoggedStep "重建后同步锁定依赖 (uv)" {
                uv pip sync --python $venvPython $lockFile
            }
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
    Write-RunLog "VoxSub 主程序已退出，等待相关子进程结束。"
    if (-not (Wait-VoxSubProcessesExit)) {
        Write-Host "主程序已退出，但仍有 VoxSub 相关子进程未结束。" -ForegroundColor Yellow
        Write-Host "请使用托盘菜单中的“退出应用”完成退出；脚本不会强制结束进程。" -ForegroundColor Yellow
        Write-RunLog "等待子进程超时；未强制结束任何进程。"
        exit 3
    }
    Write-RunLog "VoxSub 主程序及相关子进程已结束。"
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
