[CmdletBinding()]
param(
    [string]$ModelPath = $env:VOXSUB_NPU_TEST_MODEL,
    [string]$LlamaDir = $env:VOXSUB_LLAMA_DIR,
    [switch]$DriverCheckOnly
)

$ErrorActionPreference = 'Stop'
$ProbeDir = Join-Path (Split-Path -Parent $PSScriptRoot) '.npu-probe'
New-Item -ItemType Directory -Path $ProbeDir -Force | Out-Null
$LogPath = Join-Path $ProbeDir 'probe.log'
$ServerOutPath = Join-Path $ProbeDir 'llama-server.stdout.log'
$ServerErrPath = Join-Path $ProbeDir 'llama-server.stderr.log'
$InferenceResponsePath = Join-Path $ProbeDir 'inference-response.json'
$MinimumIntelNpuDriver = [version]'32.0.100.4778'
$IntelNpuDriverUrl = 'https://www.intel.com/content/www/us/en/download/794734/intel-npu-driver-windows.html'

function Write-Probe([string]$Message) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Write-Output $line
    $line | Out-File -LiteralPath $LogPath -Encoding utf8 -Append
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
    $assetModelsRoot = Join-Path (Split-Path -Parent $PSScriptRoot) '.npu-assets\models'
    $pathFile = Join-Path $assetModelsRoot 'model-path.txt'
    if (Test-Path -LiteralPath $pathFile -PathType Leaf) {
        $recordedPath = (Get-Content -LiteralPath $pathFile -Raw).Trim()
        if ($recordedPath -and (Test-Path -LiteralPath $recordedPath -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $recordedPath).Path
        }
    }
    $modelsRoot = Join-Path $env:LOCALAPPDATA 'VoxSub\models\llm'
    $modelsRoots = @(
        $assetModelsRoot,
        $modelsRoot,
        (Join-Path $env:LOCALAPPDATA 'VoxSub\models')
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
$inferenceSucceeded = $false
try {
    $npuDevices = @(Get-CimInstance Win32_PnPEntity |
        Where-Object { $_.Name -match '\bNPU\b|Neural Processing|AI Boost|Ryzen AI|Hexagon' } |
        Select-Object -ExpandProperty Name)
    if ($npuDevices.Count -eq 0) {
        throw 'Windows did not detect an NPU device.'
    }
    Write-Probe "Detected NPU device(s): $($npuDevices -join '; ')"
    $processorNames = @(Get-CimInstance Win32_Processor | Select-Object -ExpandProperty Name -Unique)
    Write-Probe "Processor(s): $($processorNames -join '; ')"
    $npuDrivers = @(Get-CimInstance Win32_PnPSignedDriver |
        Where-Object { $_.DeviceName -match '\bNPU\b|Neural Processing|AI Boost' } |
        Select-Object DeviceName, DriverVersion)
    Write-Probe "NPU driver(s): $((@($npuDrivers | ForEach-Object { "$($_.DeviceName) $($_.DriverVersion)" })) -join '; ')"
    $parsedDrivers = @($npuDrivers | ForEach-Object {
        try {
            [pscustomobject]@{ Name = $_.DeviceName; Version = [version]$_.DriverVersion }
        }
        catch {
            Write-Probe "Ignoring unreadable NPU driver version: $($_.DeviceName) $($_.DriverVersion)"
        }
    })
    if ($parsedDrivers.Count -eq 0) {
        throw "The Intel NPU driver version could not be read. Reinstall the latest driver: $IntelNpuDriverUrl"
    }
    $newestNpuDriver = $parsedDrivers | Sort-Object Version -Descending | Select-Object -First 1
    if ($newestNpuDriver.Version -lt $MinimumIntelNpuDriver) {
        throw "Intel NPU driver $($newestNpuDriver.Version) is too old for OpenVINO 2026.2. Minimum: $MinimumIntelNpuDriver. Update to 32.0.100.4841 or newer, restart Windows, then retry: $IntelNpuDriverUrl"
    }
    Write-Probe "Intel NPU driver compatibility check passed: $($newestNpuDriver.Version) >= $MinimumIntelNpuDriver"
    if ($DriverCheckOnly) {
        Write-Probe 'PASS: Intel NPU driver preflight completed.'
        return
    }

    $server = Find-LlamaServer $LlamaDir
    $serverDir = Split-Path -Parent $server
    Write-Probe "llama-server: $server"
    $env:PATH = "$serverDir;$env:PATH"
    foreach ($requiredFile in @('ggml-openvino.dll', 'openvino_intel_npu_plugin.dll')) {
        if (-not (Test-Path -LiteralPath (Join-Path $serverDir $requiredFile) -PathType Leaf)) {
            throw "OpenVINO NPU runtime file missing: $requiredFile"
        }
    }

    # Do not call --list-devices here. On some Intel NPU driver/runtime
    # combinations that diagnostic path crashes before the explicit device
    # selection is applied. The real server launch below is the authoritative
    # test and captures its stdout/stderr for diagnosis.
    Write-Probe 'Skipping --list-devices; testing explicit OPENVINO0/NPU launch.'

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
        '--parallel', '1',
        '--verbose'
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
    $startInfo.EnvironmentVariables['GGML_OPENVINO_STATEFUL_EXECUTION'] = '0'
    $startInfo.EnvironmentVariables['GGML_OPENVINO_PROFILING'] = '1'
    $startInfo.EnvironmentVariables['OV_NPU_LOG_LEVEL'] = 'LOG_INFO'
    $serverProcess = New-Object System.Diagnostics.Process
    $serverProcess.StartInfo = $startInfo
    if (-not $serverProcess.Start()) {
        throw 'llama-server process could not start.'
    }
    $stdoutTask = $serverProcess.StandardOutput.ReadToEndAsync()
    $stderrTask = $serverProcess.StandardError.ReadToEndAsync()

    $ready = $false
    $startupTimeoutSeconds = 600
    $deadline = (Get-Date).AddSeconds($startupTimeoutSeconds)
    $lastHeartbeat = Get-Date
    while ((Get-Date) -lt $deadline) {
        if ($serverProcess.HasExited) {
            throw "llama-server exited early with code $($serverProcess.ExitCode)"
        }
        if (((Get-Date) - $lastHeartbeat).TotalSeconds -ge 30) {
            $elapsed = [Math]::Floor(((Get-Date) - $deadline.AddSeconds(-$startupTimeoutSeconds)).TotalSeconds)
            Write-Probe "Still waiting for llama-server health after ${elapsed}s (first NPU compile may be slow)."
            $lastHeartbeat = Get-Date
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
        throw "llama-server did not become ready within $startupTimeoutSeconds seconds."
    }
    Write-Probe 'llama-server health check passed.'

    $body = @{
        messages = @(@{ role = 'user'; content = 'Reply with only OK.' })
        max_tokens = 16
        temperature = 0
        chat_template_kwargs = @{ enable_thinking = $false }
    } | ConvertTo-Json -Depth 6 -Compress
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/v1/chat/completions" -Method Post `
        -ContentType 'application/json; charset=utf-8' -Body $body -TimeoutSec 60 -UseBasicParsing
    $response.Content | Set-Content -LiteralPath $InferenceResponsePath -Encoding utf8
    $reply = $response.Content | ConvertFrom-Json
    $message = $reply.choices[0].message
    $content = [string]$message.content
    $reasoning = [string]$message.reasoning_content
    $completionTokens = [int]$reply.usage.completion_tokens
    if ([string]::IsNullOrWhiteSpace($content) -and
        [string]::IsNullOrWhiteSpace($reasoning) -and $completionTokens -le 0) {
        throw 'llama-server started but returned no generated tokens.'
    }
    Write-Probe "Inference completed: content='$content' reasoning='$reasoning' completion_tokens=$completionTokens"
    $inferenceSucceeded = $true
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
        $fallbackDetected = $combined -match '(?i)fallback to CPU|device NPU is not available'
        # Require NPU as a standalone device/plugin name. A broad substring
        # search incorrectly matched tensor names such as ``input_scale``.
        $npuMarkerDetected = $combined -match (
            '(?im)^.*(?:OpenVINO:\s+using device\s+NPU|' +
            '\[[^\]\r\n]*\bNPU\b[^\]\r\n]*\]|Intel\s+NPU\s+Plugin|' +
            '\bNPU\s+(?:compiler|driver|device|platform)\b).*$'
        )
        if ($inferenceSucceeded) {
            if ($fallbackDetected) {
                Write-Probe 'FAIL: llama.cpp reported NPU fallback or unavailable NPU.'
                throw 'NPU is unavailable or fell back to CPU. Download intel-npu-probe diagnostics.'
            }
            if (-not $npuMarkerDetected) {
                Write-Probe 'FAIL: no explicit OpenVINO NPU execution marker was found.'
                throw 'No proof of OpenVINO NPU execution was found in llama-server logs.'
            }
            Write-Probe 'PASS: health check and inference succeeded on OpenVINO NPU; no CPU fallback marker found.'
        } elseif ($fallbackDetected) {
            Write-Probe 'DIAGNOSTIC: llama.cpp reported NPU fallback or unavailable NPU before inference completed.'
        } elseif (-not $npuMarkerDetected) {
            Write-Probe 'DIAGNOSTIC: no explicit OpenVINO NPU execution marker was found before inference failed.'
        }
    }
}
