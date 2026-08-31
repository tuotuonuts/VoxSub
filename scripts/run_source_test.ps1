[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    [Console]::OutputEncoding = $OutputEncoding
} catch {
    # Keep startup compatible with older Windows PowerShell hosts.
}
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$localAppData = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { $repoRoot }
$diagnosticDir = Join-Path $localAppData "VoxSub\diagnostics\source-run"
$null = New-Item -ItemType Directory -Force -Path $diagnosticDir
$logPath = Join-Path $diagnosticDir "source-run-$stamp.log"
$bootstrapRoot = Join-Path $localAppData "VoxSub\bootstrap"
$uvVersion = "0.12.5"
$gitVersion = "2.55.0.5"
$isArm64 = $env:PROCESSOR_ARCHITECTURE -eq "ARM64"
if ($isArm64) {
    $uvAsset = "uv-aarch64-pc-windows-msvc.zip"
    $uvSha256 = "724279317FEE6E5FA8AD1908E4EBA2BBE764EF1ECE5B3F4597927B62B1FE562A"
    $gitAsset = "MinGit-$gitVersion-arm64.zip"
    $gitSha256 = "05843F9D6E60306C3AB886799E2C67200CAAB921571F10512DF3493049179DDB"
} else {
    $uvAsset = "uv-x86_64-pc-windows-msvc.zip"
    $uvSha256 = "4C4D49D8738847D9B71BA319E49A5688C93EAC0FE6204B1DF24E98528DDDF39A"
    $gitAsset = "MinGit-$gitVersion-64-bit.zip"
    $gitSha256 = "56D7B226B7693196CFC71FEF26568F536C4A021AB6C37FF2DB4287BED908E96E"
}
$uvUrl = "https://github.com/astral-sh/uv/releases/download/$uvVersion/$uvAsset"
$gitUrl = "https://github.com/git-for-windows/git/releases/download/v2.55.0.windows.5/$gitAsset"
$script:gitExe = $null
$script:uvExe = $null

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

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

function Get-VerifiedDownload {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Sha256
    )
    $parent = Split-Path -Parent $Destination
    $null = New-Item -ItemType Directory -Force -Path $parent
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        if ((Get-FileSha256 $Destination) -eq $Sha256.ToUpperInvariant()) {
            return $Destination
        }
        Remove-Item -LiteralPath $Destination -Force
    }
    $partial = "$Destination.part"
    if (Test-Path -LiteralPath $partial) {
        Remove-Item -LiteralPath $partial -Force
    }
    Write-RunLog "下载引导工具: $([System.IO.Path]::GetFileName($Destination))"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $Url -OutFile $partial -UseBasicParsing
        $actual = Get-FileSha256 $partial
        if ($actual -ne $Sha256.ToUpperInvariant()) {
            throw "下载文件 SHA256 校验失败 (expected=$($Sha256.Substring(0, 12)) actual=$($actual.Substring(0, 12)))"
        }
        Move-Item -LiteralPath $partial -Destination $Destination -Force
        return $Destination
    } catch {
        Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
        throw
    }
}

function Get-PortableCommand {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("git", "uv")][string]$Name
    )
    $existing = Get-Command $Name -ErrorAction SilentlyContinue
    if ($existing) {
        return $existing.Source
    }
    if ($Name -eq "uv") {
        $root = Join-Path $bootstrapRoot "uv"
        $exe = Join-Path $root "uv.exe"
        $archive = Join-Path $bootstrapRoot $uvAsset
        if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
            Get-VerifiedDownload $uvUrl $archive $uvSha256 | Out-Null
            $staging = Join-Path $bootstrapRoot ("uv-extract-" + [guid]::NewGuid().ToString("N"))
            try {
                Expand-Archive -LiteralPath $archive -DestinationPath $staging -Force
                $found = Get-ChildItem -LiteralPath $staging -Filter "uv.exe" -File -Recurse |
                    Select-Object -First 1
                if (-not $found) { throw "uv 压缩包中没有 uv.exe。" }
                New-Item -ItemType Directory -Force -Path $root | Out-Null
                Copy-Item -LiteralPath $found.FullName -Destination $exe -Force
            } finally {
                if (Test-Path -LiteralPath $staging) {
                    Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
                }
            }
        }
        $script:uvExe = $exe
        return $exe
    }
    $root = Join-Path $bootstrapRoot "git"
    $exe = Join-Path $root "cmd\git.exe"
    $archive = Join-Path $bootstrapRoot $gitAsset
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        Get-VerifiedDownload $gitUrl $archive $gitSha256 | Out-Null
        $staging = Join-Path $bootstrapRoot ("git-extract-" + [guid]::NewGuid().ToString("N"))
        try {
            Expand-Archive -LiteralPath $archive -DestinationPath $staging -Force
            $found = Get-ChildItem -LiteralPath $staging -Filter "git.exe" -File -Recurse |
                Where-Object { $_.Directory.Name -eq "cmd" } |
                Select-Object -First 1
            if (-not $found) { throw "MinGit 压缩包中没有 cmd\\git.exe。" }
            $foundRoot = $found.Directory.Parent
            New-Item -ItemType Directory -Force -Path $root | Out-Null
            Copy-Item -Path (Join-Path $foundRoot.FullName "*") -Destination $root -Recurse -Force
        } finally {
            if (Test-Path -LiteralPath $staging) {
                Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        throw "便携 Git 安装后仍找不到 git.exe。"
    }
    $env:Path = "$(Split-Path -Parent $exe);$(Join-Path $root 'mingw64\bin');$env:Path"
    $script:gitExe = $exe
    return $exe
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
            $probe = @(& $command.Source @($launcher.Args) -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}'); print(sys.executable)" 2>$null)
            $versionText = ([string]$probe[0]).Trim()
            $version = [version]::Parse("$versionText.0")
            if ($version -ge [version]::Parse("3.11.0")) {
                $pythonPath = if ($probe.Count -gt 1) { ([string]$probe[1]).Trim() } else { "" }
                return @{ Exe = $command.Source; Args = @($launcher.Args); Path = $pythonPath; Version = $versionText }
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

function Invoke-Uv {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    & $script:uvExe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "uv 命令失败，退出码 $LASTEXITCODE"
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
    Invoke-LoggedStep "创建本机独立虚拟环境" {
        if ($script:uvExe) {
            $request = if ($python -and $python.Path) { $python.Path } else { "3.11" }
            $venvArgs = @("venv", "--python", $request)
            if (-not $python) { $venvArgs += "--managed-python" }
            $venvArgs += $venvDir
            Invoke-Uv $venvArgs
        } else {
            Invoke-Python $python @("-m", "venv", $venvDir)
        }
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

function Get-TrackedWorkingTreeChanges {
    <#
    Report only tracked source changes. Runtime data is intentionally outside the
    repository, and ignored folders such as .venv must not block routine updates.
    #>
    $status = @(& $script:gitExe -c "safe.directory=$repoRoot" -C $repoRoot status --porcelain --untracked-files=no)
    if ($LASTEXITCODE -ne 0) {
        throw "无法检查 Git 工作区状态，退出码 $LASTEXITCODE。"
    }
    return @(
        $status |
            Where-Object { $_ } |
            ForEach-Object {
                $line = [string]$_
                if ($line.Length -gt 3) { $line.Substring(3) } else { $line }
            }
    )
}

try {
    Write-RunLog "VoxSub 源码测试启动"
    Write-RunLog "项目目录: $repoRoot"
    Write-RunLog "诊断日志: $logPath"

    $script:gitExe = Get-PortableCommand "git"
    Write-RunLog "Git: $script:gitExe"
    $python = Get-PythonCandidate
    if ($python) {
        Write-RunLog "系统 Python: $($python.Exe) $($python.Version)"
    } else {
        Write-RunLog "未找到系统 Python 3.11+，将使用 uv 管理的 Python 3.11。"
    }
    try {
        $script:uvExe = Get-PortableCommand "uv"
        Write-RunLog "uv: $script:uvExe"
    } catch {
        if (-not $python) {
            throw "未找到 Python 3.11+，且无法自动下载 uv 管理工具：$($_.Exception.Message)"
        }
        Write-RunLog "[警告] uv 自动准备失败，将使用系统 Python 和 pip：$($_.Exception.Message)"
        $script:uvExe = $null
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

    $localChanges = @(Get-TrackedWorkingTreeChanges)
    if ($localChanges.Count -gt 0) {
        Write-RunLog "检测到 $($localChanges.Count) 个未提交的 Git 跟踪文件，为避免覆盖，未执行 git pull。"
        foreach ($path in $localChanges) {
            Write-RunLog "本地修改: $path"
        }
        Write-Host "检测到未提交的 VoxSub 源码修改，已安全停止更新。" -ForegroundColor Yellow
        Write-Host "请先提交或暂存这些代码修改，再重新双击本脚本。" -ForegroundColor Yellow
        Write-Host "模型、配置、日志和 .venv 不会触发此检查。详细文件列表已写入诊断日志。" -ForegroundColor Yellow
        Write-RunLog "未执行 git pull：请先提交或暂存本地源码修改。"
        exit 4
    }

    Invoke-LoggedStep "更新源码 (git pull --ff-only)" {
        & $script:gitExe -c "safe.directory=$repoRoot" -C $repoRoot pull --ff-only
    }

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
        Move-VenvAside $venvDir
        New-LocalVenv
    }

    $lockFile = Join-Path $repoRoot "requirements.lock"
    if (-not (Test-Path -LiteralPath $lockFile)) {
        throw "找不到 requirements.lock，无法安全安装依赖。"
    }
    if ($script:uvExe) {
        try {
            Invoke-LoggedStep "同步锁定依赖 (uv)" {
                & $script:uvExe pip sync --python $venvPython $lockFile
            }
        } catch {
            Write-RunLog "[警告] 现有虚拟环境同步失败，将备份并重建本机环境。"
            Move-VenvAside $venvDir
            New-LocalVenv
            $venvPython = Join-Path $venvDir "Scripts\python.exe"
            Invoke-LoggedStep "重建后同步锁定依赖 (uv)" {
                & $script:uvExe pip sync --python $venvPython $lockFile
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

    # Installer builds bundle the llama.cpp runtimes. Source checkouts need
    # the same CPU/Vulkan/OpenVINO runtime set once, without rebuilding or
    # repacking the app. OpenVINO is downloaded to LocalAppData and is never
    # copied into Git. If the network is unavailable, keep the CPU/Vulkan
    # fallback usable and record the exact reason in the diagnostic log.
    try {
        Invoke-LoggedStep "补齐本地质量翻译运行时 (CPU/Vulkan/OpenVINO)" {
            & (Join-Path $repoRoot "scripts\sync_llama_runtime.ps1")
            if ($LASTEXITCODE -ne 0) {
                throw "llama.cpp 运行时同步失败，退出码 $LASTEXITCODE"
            }
        }
    } catch {
        Write-RunLog "[警告] 质量翻译运行时未准备完成：$($_.Exception.Message)"
        Write-Host "质量翻译运行时暂未完整就绪；基础功能仍可启动，网络恢复后重新运行本脚本即可补齐。" -ForegroundColor Yellow
    }

    # Never inherit a foreign Python runtime configuration into the app.
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
    $env:VOXSUB_ENVIRONMENT = "testing"

    # A source checkout may have a verified no-NPUW OpenVINO runtime produced
    # by build.ps1 in TEMP. Prefer it over the downloaded bootstrap runtime;
    # never copy either runtime into Git.
    if (-not $env:VOXSUB_NPU_RUNTIME_DIR) {
        $npuCandidate = Join-Path $env:TEMP "VoxSub_npu_runtime_b10470"
        if (Test-Path -LiteralPath (Join-Path $npuCandidate "llama-server.exe") -PathType Leaf) {
            $env:VOXSUB_NPU_RUNTIME_DIR = $npuCandidate
            Write-RunLog "发现本机 OpenVINO NPU 运行时: $npuCandidate"
        }
    }

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
