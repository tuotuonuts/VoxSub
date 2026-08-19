[CmdletBinding()]
param(
    [string]$ModelPath = $env:VOXSUB_NPU_TEST_MODEL,
    [string]$LlamaDir = $env:VOXSUB_LLAMA_DIR
)

$ErrorActionPreference = 'Stop'
$ProbeDir = Join-Path (Split-Path -Parent $PSScriptRoot) '.npu-probe'
New-Item -ItemType Directory -Path $ProbeDir -Force | Out-Null
$LogPath = Join-Path $ProbeDir 'probe.log'
$ServerOutPath = Join-Path $ProbeDir 'llama-server.stdout.log'
$ServerErrPath = Join-Path $ProbeDir 'llama-server.stderr.log'

function Write-Probe([string]$Message) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    $line | Tee-Object -FilePath $LogPath -Append
}

function Find-LlamaServer([string]$PreferredDir) {
    $roots = @()
    if ($PreferredDir) {
        $roots += $PreferredDir
    }
    $roots += (Join-Path $env:LOCALAPPDATA 'VoxSub\tools\llama')
    foreach ($root in $roots | Select-Object -Unique) {
        $candidate = Join-Path $root 'llama-server.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw '未找到带 OpenVINO 的 llama-server.exe。请先在该电脑安装 VoxSub，或在工作流输入 llama_dir。'
}

function Find-GgufModel([string]$PreferredPath) {
    if ($PreferredPath) {
        if (-not (Test-Path -LiteralPath $PreferredPath -PathType Leaf)) {
            throw "指定的 GGUF 模型不存在: $PreferredPath"
        }
        return (Resolve-Path -LiteralPath $PreferredPath).Path
    }
    $modelsRoot = Join-Path $env:LOCALAPPDATA 'VoxSub\models\llm'
    if (Test-Path -LiteralPath $modelsRoot -PathType Container) {
        $model = Get-ChildItem -LiteralPath $modelsRoot -Filter '*.gguf' -File -Recurse |
            Sort-Object LastWriteTimeUtc -Descending |
            Select-Object -First 1
        if ($model) {
            return $model.FullName
        }
    }
    throw '未找到 GGUF 模型。请在工作流的 model_path 输入该 Intel NPU 电脑上的绝对模型路径。'
}

function Get-FreePort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        return $listener.LocalEndpoint.Port
    }
    finally {
        $listener.Stop()
    }
}

$serverProcess = $null
$stdoutTask = $null
$stderrTask = $null
try {
    $npuDevices = @(Get-CimInstance Win32_PnPEntity |
        Where-Object { $_.Name -match '\bNPU\b|Neural Processing|AI Boost|Ryzen AI|Hexagon' } |
        Select-Object -ExpandProperty Name)
    if ($npuDevices.Count -eq 0) {
        throw 'Windows 没有检测到 NPU 设备，无法执行 Intel NPU 验证。'
    }
    Write-Probe "Detected NPU device(s): $($npuDevices -join '; ')"

    $server = Find-LlamaServer $LlamaDir
    $serverDir = Split-Path -Parent $server
    Write-Probe "llama-server: $server"
    foreach ($requiredFile in @('ggml-openvino.dll', 'openvino_intel_npu_plugin.dll')) {
        if (-not (Test-Path -LiteralPath (Join-Path $serverDir $requiredFile) -PathType Leaf)) {
            throw "OpenVINO NPU runtime file missing: $requiredFile"
        }
    }

    $deviceList = & $server --list-devices 2>&1
    $deviceExitCode = $LASTEXITCODE
    $deviceList | Set-Content -LiteralPath (Join-Path $ProbeDir 'llama-devices.log') -Encoding utf8
    Write-Probe "llama-server --list-devices exit=$deviceExitCode"
    $deviceList | ForEach-Object { Write-Probe "device: $_" }
    if ($deviceExitCode -ne 0) {
        throw 'llama-server --list-devices 失败。请查看 llama-devices.log。'
    }
    if (-not (($deviceList -join "`n") -match 'OPENVINO')) {
        throw 'llama-server 没有列出 OpenVINO 后端。请确认使用的是 VoxSub 随包 OpenVINO runtime。'
    }

    $model = Find-GgufModel $ModelPath
    $modelInfo = Get-Item -LiteralPath $model
    Write-Probe "GGUF model: $model ($([math]::Round($modelInfo.Length / 1GB, 2)) GB)"

    $port = Get-FreePort
    $args = @(
        '--device', 'OPENVINO0',
        '--model', $model,
        '--host', '127.0.0.1',
        '--port', "$port",
        '--ctx-size', '512',
        '--n-gpu-layers', '999',
        '--threads', '4',
        '--parallel', '1'
    )
    Write-Probe "Starting NPU server: --device OPENVINO0 --n-gpu-layers 999 --parallel 1"

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $server
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.Environment['GGML_OPENVINO_DEVICE'] = 'NPU'
    $startInfo.Environment['GGML_OPENVINO_ENABLE_FALLBACK'] = '0'
    foreach ($argument in $args) {
        [void]$startInfo.ArgumentList.Add($argument)
    }
    $serverProcess = [System.Diagnostics.Process]::new()
    $serverProcess.StartInfo = $startInfo
    if (-not $serverProcess.Start()) {
        throw 'llama-server 进程无法启动。'
    }
    $stdoutTask = $serverProcess.StandardOutput.ReadToEndAsync()
    $stderrTask = $serverProcess.StandardError.ReadToEndAsync()

    $ready = $false
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        if ($serverProcess.HasExited) {
            throw "llama-server 提前退出，退出码=$($serverProcess.ExitCode)"
        }
        try {
            $health = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -TimeoutSec 2 -UseBasicParsing
            if ($health.StatusCode -eq 200) {
                $ready = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 400
        }
    }
    if (-not $ready) {
        throw 'llama-server 在 90 秒内未就绪。'
    }
    Write-Probe 'llama-server health check passed.'

    $body = @{ messages = @(@{ role = 'user'; content = 'Reply with only OK.' }); max_tokens = 4; temperature = 0 } |
        ConvertTo-Json -Depth 5 -Compress
    $reply = Invoke-RestMethod -Uri "http://127.0.0.1:$port/v1/chat/completions" -Method Post `
        -ContentType 'application/json; charset=utf-8' -Body $body -TimeoutSec 45
    $content = [string]$reply.choices[0].message.content
    if ([string]::IsNullOrWhiteSpace($content)) {
        throw 'llama-server 已启动，但未返回有效聊天结果。'
    }
    Write-Probe "Inference reply: $content"
}
catch {
    Write-Probe "FAIL: $($_.Exception.Message)"
    throw
}
finally {
    if ($serverProcess) {
        if (-not $serverProcess.HasExited) {
            $serverProcess.Kill($true)
            $serverProcess.WaitForExit()
        }
        if ($stdoutTask) {
            $stdoutTask.GetAwaiter().GetResult() | Set-Content -LiteralPath $ServerOutPath -Encoding utf8
        }
        if ($stderrTask) {
            $stderrTask.GetAwaiter().GetResult() | Set-Content -LiteralPath $ServerErrPath -Encoding utf8
        }
        $combined = @(
            if (Test-Path -LiteralPath $ServerOutPath) { Get-Content -LiteralPath $ServerOutPath -Raw }
            if (Test-Path -LiteralPath $ServerErrPath) { Get-Content -LiteralPath $ServerErrPath -Raw }
        ) -join "`n"
        if ($combined -match '(?i)fallback to CPU|device NPU is not available') {
            Write-Probe 'FAIL: llama.cpp reported NPU fallback or unavailable NPU.'
            throw 'NPU 不可用或已回退到 CPU；请下载 intel-npu-probe 诊断附件。'
        }
        if ($combined -notmatch '(?is)openvino.{0,240}npu|npu.{0,240}openvino|using device.{0,80}npu') {
            Write-Probe 'FAIL: no explicit OpenVINO NPU execution marker was found.'
            throw '未在 llama-server 日志中找到 NPU 实际执行证据；请下载 intel-npu-probe 诊断附件。'
        }
        Write-Probe 'PASS: OpenVINO NPU execution marker found; no CPU fallback marker found.'
    }
}
