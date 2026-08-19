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
    $roots += (Join-Path (Split-Path -Parent $PSScriptRoot) '.npu-assets\openvino')
    $roots += (Join-Path $env:LOCALAPPDATA 'VoxSub\tools\llama')
    foreach ($root in $roots | Select-Object -Unique) {
        if (Test-Path -LiteralPath $root -PathType Container) {
            $candidate = Get-ChildItem -LiteralPath $root -Filter 'llama-server.exe' -File -Recurse |
                Select-Object -First 1
            if ($candidate) {
                return $candidate.FullName
            }
        }
    }
    throw 'OpenVINO llama-server.exe not found. Install VoxSub or provide llama_dir.'
}

function Find-GgufModel([string]$PreferredPath) {
    if ($PreferredPath) {
        if (-not (Test-Path -LiteralPath $PreferredPath -PathType Leaf)) {
            throw "GGUF model does not exist: $PreferredPath"
        }
        return (Resolve-Path -LiteralPath $PreferredPath).Path
    }
    $modelsRoot = Join-Path $env:LOCALAPPDATA 'VoxSub\models\llm'
    $modelsRoots = @(
        (Join-Path (Split-Path -Parent $PSScriptRoot) '.npu-assets\models'),
        $modelsRoot
    )
    foreach ($root in $modelsRoots | Select-Object -Unique) {
      if (Test-Path -LiteralPath $root -PathType Container) {
        $model = Get-ChildItem -LiteralPath $root -Filter '*.gguf' -File -Recurse |
            Sort-Object LastWriteTimeUtc -Descending |
            Select-Object -First 1
        if ($model) {
            return $model.FullName
        }
      }
    }
    throw 'No GGUF model found. Provide the absolute model_path on the Intel NPU computer.'
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

function Quote-WindowsArgument([string]$Value) {
    if ($Value -notmatch '[\s\"]') {
        return $Value
    }
    return '"' + $Value.Replace('"', '\"') + '"'
}

$serverProcess = $null
$stdoutTask = $null
$stderrTask = $null
try {
    $npuDevices = @(Get-CimInstance Win32_PnPEntity |
        Where-Object { $_.Name -match '\bNPU\b|Neural Processing|AI Boost|Ryzen AI|Hexagon' } |
        Select-Object -ExpandProperty Name)
    if ($npuDevices.Count -eq 0) {
        throw 'Windows did not detect an NPU device.'
    }
    Write-Probe "Detected NPU device(s): $($npuDevices -join '; ')"

    $server = Find-LlamaServer $LlamaDir
    $serverDir = Split-Path -Parent $server
    Write-Probe "llama-server: $server"
    $env:PATH = "$serverDir;$env:PATH"
    foreach ($requiredFile in @('ggml-openvino.dll', 'openvino_intel_npu_plugin.dll')) {
        if (-not (Test-Path -LiteralPath (Join-Path $serverDir $requiredFile) -PathType Leaf)) {
            throw "OpenVINO NPU runtime file missing: $requiredFile"
        }
    }

    # Device enumeration can initialize the NPU plugin with its default device
    # before llama-server receives an explicit --device. Keep this diagnostic
    # probe on OpenVINO CPU; the real server below is still forced to NPU.
    $previousOpenvinoDevice = $env:GGML_OPENVINO_DEVICE
    $env:GGML_OPENVINO_DEVICE = 'CPU'
    $deviceList = & $server --list-devices 2>&1
    $deviceExitCode = $LASTEXITCODE
    if ($null -eq $previousOpenvinoDevice) {
        Remove-Item Env:GGML_OPENVINO_DEVICE -ErrorAction SilentlyContinue
    } else {
        $env:GGML_OPENVINO_DEVICE = $previousOpenvinoDevice
    }
    $deviceList | Set-Content -LiteralPath (Join-Path $ProbeDir 'llama-devices.log') -Encoding utf8
    Write-Probe "llama-server --list-devices exit=$deviceExitCode"
    $deviceList | ForEach-Object { Write-Probe "device: $_" }
    if ($deviceExitCode -ne 0) {
        throw 'llama-server --list-devices failed. See llama-devices.log.'
    }
    if (-not (($deviceList -join "`n") -match 'OPENVINO')) {
        throw 'llama-server did not list an OpenVINO backend.'
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
    # Use .Arguments/.EnvironmentVariables for Windows PowerShell 5.1;
    # ArgumentList/Environment are only available on newer .NET runtimes.
    $startInfo.Arguments = (($args | ForEach-Object {
        Quote-WindowsArgument ([string]$_)
    }) -join ' ')
    $startInfo.EnvironmentVariables['GGML_OPENVINO_DEVICE'] = 'NPU'
    $startInfo.EnvironmentVariables['GGML_OPENVINO_ENABLE_FALLBACK'] = '0'
    $serverProcess = New-Object System.Diagnostics.Process
    $serverProcess.StartInfo = $startInfo
    if (-not $serverProcess.Start()) {
        throw 'llama-server process could not start.'
    }
    $stdoutTask = $serverProcess.StandardOutput.ReadToEndAsync()
    $stderrTask = $serverProcess.StandardError.ReadToEndAsync()

    $ready = $false
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        if ($serverProcess.HasExited) {
            throw "llama-server exited early with code $($serverProcess.ExitCode)"
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
        throw 'llama-server did not become ready within 90 seconds.'
    }
    Write-Probe 'llama-server health check passed.'

    $body = @{ messages = @(@{ role = 'user'; content = 'Reply with only OK.' }); max_tokens = 4; temperature = 0 } |
        ConvertTo-Json -Depth 5 -Compress
    $reply = Invoke-RestMethod -Uri "http://127.0.0.1:$port/v1/chat/completions" -Method Post `
        -ContentType 'application/json; charset=utf-8' -Body $body -TimeoutSec 45
    $content = [string]$reply.choices[0].message.content
    if ([string]::IsNullOrWhiteSpace($content)) {
        throw 'llama-server started but returned no valid chat result.'
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
            $serverProcess.Kill()
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
            throw 'NPU is unavailable or fell back to CPU. Download intel-npu-probe diagnostics.'
        }
        if ($combined -notmatch '(?is)openvino.{0,240}npu|npu.{0,240}openvino|using device.{0,80}npu') {
            Write-Probe 'FAIL: no explicit OpenVINO NPU execution marker was found.'
            throw 'No proof of OpenVINO NPU execution was found in llama-server logs.'
        }
        Write-Probe 'PASS: OpenVINO NPU execution marker found; no CPU fallback marker found.'
    }
}
