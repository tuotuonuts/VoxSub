[CmdletBinding()]
param(
    [string]$ModelPath = $env:VOXSUB_NPU_TEST_MODEL,
    [string]$LlamaDir = $env:VOXSUB_LLAMA_DIR,
    [string]$OutputDir = $env:VOXSUB_NPU_PROBE_DIR,
    [switch]$DriverCheckOnly,
    [ValidateSet('OPENVINO0', 'NPU')]
    [string]$DeviceArgument = 'OPENVINO0',
    [ValidateSet('NPU', 'GPU', 'CPU')]
    [string]$OpenVinoDevice = 'NPU',
    [switch]$SkipOpenVinoDevice,
    [switch]$SkipHardwarePreflight,
    [ValidateRange(30, 3600)]
    [int]$StartupTimeoutSeconds = 600,
    [ValidateRange(5, 600)]
    [int]$InferenceTimeoutSeconds = 60
)

$ErrorActionPreference = 'Stop'
$ProbeDir = if ($OutputDir) {
    [System.IO.Path]::GetFullPath($OutputDir)
} else {
    Join-Path (Split-Path -Parent $PSScriptRoot) '.npu-probe'
}
New-Item -ItemType Directory -Path $ProbeDir -Force | Out-Null
$LogPath = Join-Path $ProbeDir 'probe.log'
$ServerOutPath = Join-Path $ProbeDir 'llama-server.stdout.log'
$ServerErrPath = Join-Path $ProbeDir 'llama-server.stderr.log'
$InferenceResponsePath = Join-Path $ProbeDir 'inference-response.json'
$ProbeSummaryPath = Join-Path $ProbeDir 'probe-summary.json'
$ProbeSchemaVersion = 2
$RunId = [guid]::NewGuid().ToString('N')
$StartedAt = (Get-Date).ToString('o')
# A probe directory is reusable, but its result files must always describe this
# invocation. Keep the directory itself so callers can choose a stable path.
foreach ($artifact in @($LogPath, $ServerOutPath, $ServerErrPath,
        $InferenceResponsePath, $ProbeSummaryPath)) {
    if (Test-Path -LiteralPath $artifact -PathType Leaf) {
        Remove-Item -LiteralPath $artifact -Force -ErrorAction SilentlyContinue
    }
}
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

function Get-FailureReasonCode([string]$Message, [string]$CombinedOutput, [int]$ExitCode) {
    $text = "$Message`n$CombinedOutput"
    if ($ExitCode -eq -1073741819 -or $ExitCode -eq 3221225477) {
        return 'process_access_violation'
    }
    if ($text -match '(?i)NPU_COMPILER_DYNAMIC_QUANTIZATION|NPU_USE_NPUW|NPUW_[A-Z_]+|not supported for current configuration') {
        return 'unsupported_openvino_option'
    }
    if ($text -match '(?i)\u62d2\u7edd\u8bbf\u95ee|access is denied|permission denied') {
        return 'npu_device_inventory_access_denied'
    }
    if ($text -match '(?i)inventory|enumeration|HRESULT|pnputil exit code') {
        return 'npu_device_inventory_error'
    }
    if ($text -match '(?i)Windows did not detect an NPU|no matching NPU|NPU device.*(?:not found|not detected)|\u672a\u68c0\u6d4b\u5230 NPU') {
        return 'npu_device_not_detected'
    }
    if ($text -match '(?i)fallback to CPU|device NPU is not available|NPU.*(?:unavailable|fallback)') {
        return 'cpu_fallback_or_npu_unavailable'
    }
    if ($text -match '(?i)DLL|module could not be found|加载动态链接库|找不到指定的模块') {
        return 'runtime_dll_missing'
    }
    if ($text -match '(?i)driver|\u9a71\u52a8') {
        return 'driver_or_device_error'
    }
    if ($text -match '(?i)health|ready|timed out|\u8d85\u65f6') {
        return 'server_startup_or_health_timeout'
    }
    if ($text -match '(?i)inference|completion|生成') {
        return 'inference_error'
    }
    return 'probe_failed'
}

function Get-SafeExitCode([object]$Value) {
    if ($null -eq $Value) { return 0 }
    return [int]$Value
}

function Test-AccessDeniedText([string]$Text) {
    if ([string]::IsNullOrEmpty($Text)) { return $false }
    return [regex]::IsMatch($Text, '(?i)\u62d2\u7edd\u8bbf\u95ee|access is denied|permission denied')
}

function Register-NpuInventoryError([string]$Source, [object]$ErrorRecord) {
    $exception = $ErrorRecord.Exception
    $message = [string]$exception.Message
    $hresult = [int]$exception.HResult
    if ($hresult -eq -2147024891 -or
        (Test-AccessDeniedText $message)) {
        $script:npuInventoryAccessDenied = $true
    }
    $script:npuInventoryErrors += "${Source} (HRESULT ${hresult}): ${message}"
    Write-Probe "${Source} NPU enumeration failed (HRESULT=${hresult}): ${message}" | Out-Null
}

function Get-NpuDevices {
    $rx = '\bNPU\b|Neural Processing|AI Boost|Ryzen AI|Hexagon'
    $script:npuInventoryCommands += 'WMI: Win32_PnPEntity'
    try {
        $items = @(Get-CimInstance Win32_PnPEntity -ErrorAction Stop |
            Where-Object { $_.Name -match $rx } |
            Select-Object -ExpandProperty Name)
        if ($items.Count -gt 0) { return $items }
        Write-Probe 'WMI returned no NPU devices; trying Get-PnpDevice.' | Out-Null
    }
    catch {
        Register-NpuInventoryError 'WMI' $_
    }
    $script:npuInventoryCommands += 'PowerShell PnP: Get-PnpDevice -PresentOnly'
    try {
        $items = @(Get-PnpDevice -PresentOnly -ErrorAction Stop |
            Where-Object { $_.FriendlyName -match $rx -or $_.Name -match $rx } |
            ForEach-Object { if ($_.FriendlyName) { $_.FriendlyName } else { $_.Name } })
        if ($items.Count -gt 0) { return $items }
        Write-Probe 'Get-PnpDevice returned no NPU devices; trying pnputil.' | Out-Null
    }
    catch {
        Register-NpuInventoryError 'Get-PnpDevice' $_
    }
    $script:npuInventoryCommands += 'pnputil: /enum-devices /connected'
    try {
        $pnputilOutput = @(& pnputil.exe /enum-devices /connected 2>&1)
        $script:npuInventoryPnpUtilExitCode = Get-SafeExitCode $LASTEXITCODE
        $pnputilText = ($pnputilOutput | ForEach-Object { [string]$_ }) -join "`n"
        $pnputilDiagnosticLines = @($pnputilOutput |
            ForEach-Object { [string]$_ } |
            Where-Object {
                $_ -match '(?i)error|fail|denied|access|permission|\bNPU\b|AI Boost|Neural Processing|not found|no devices'
            } |
            Select-Object -First 30)
        if ($pnputilDiagnosticLines.Count -gt 0) {
            Write-Probe "pnputil diagnostic output: $($pnputilDiagnosticLines -join ' | ')" | Out-Null
        }
        if ($script:npuInventoryPnpUtilExitCode -ne 0) {
            if (Test-AccessDeniedText $pnputilText) {
                $script:npuInventoryAccessDenied = $true
            }
            $script:npuInventoryErrors += "pnputil exit code $($script:npuInventoryPnpUtilExitCode): $pnputilText"
        }
        $items = @($pnputilOutput |
            ForEach-Object { [string]$_ } |
            Where-Object { $_ -match $rx } |
            ForEach-Object { ($_ -split ':', 2)[-1].Trim() } |
            Where-Object { $_ })
        if ($items.Count -gt 0) { return $items }
    }
    catch {
        Register-NpuInventoryError 'pnputil' $_
    }
    return @()
}

$serverProcess = $null
$stdoutTask = $null
$stderrTask = $null
$inferenceSucceeded = $false
$probeResult = 'FAIL'
$probeReasonCode = 'probe_failed'
$failureMessage = ''
$serverExitCode = $null
$combined = ''
$npuMarkerDetected = $false
$fallbackDetected = $false
$npuInventoryErrors = @()
$npuInventoryAccessDenied = $false
$npuInventoryCommands = @()
$npuInventoryPnpUtilExitCode = $null
$npuDevices = @()
$npuDrivers = @()
$newestNpuDriver = $null
try {
    Write-Probe "NPU probe started: run_id=$RunId schema_version=$ProbeSchemaVersion"
    if ($SkipHardwarePreflight) {
        Write-Probe 'WARNING: hardware/device and driver preflight was explicitly skipped; direct llama-server evidence is required.'
    }
    else {
        $npuDevices = @(Get-NpuDevices)
        if ($npuDevices.Count -eq 0) {
            $inventoryErrorText = ($npuInventoryErrors | ForEach-Object { [string]$_ }) -join "`n"
            if (Test-AccessDeniedText $inventoryErrorText) {
                $npuInventoryAccessDenied = $true
            }
            $detail = if ($npuInventoryErrors.Count -gt 0) {
                " Inventory errors: $($npuInventoryErrors -join '; ')"
            } else { '' }
            if ($npuInventoryAccessDenied) {
                $probeReasonCode = 'npu_device_inventory_access_denied'
            } elseif ($npuInventoryErrors.Count -gt 0 -or
                ($null -ne $npuInventoryPnpUtilExitCode -and $npuInventoryPnpUtilExitCode -ne 0)) {
                $probeReasonCode = 'npu_device_inventory_error'
            } else {
                $probeReasonCode = 'npu_device_not_detected'
            }
            throw "Windows did not detect an NPU device through WMI, Get-PnpDevice, or pnputil.$detail"
        }
    Write-Probe "Detected NPU device(s): $($npuDevices -join '; ')"
    $processorNames = @(Get-CimInstance Win32_Processor | Select-Object -ExpandProperty Name -Unique)
    Write-Probe "Processor(s): $($processorNames -join '; ')"
    try {
        $npuDrivers = @(Get-CimInstance Win32_PnPSignedDriver -ErrorAction Stop |
            Where-Object { $_.DeviceName -match '\bNPU\b|Neural Processing|AI Boost' } |
            Select-Object DeviceName, DriverVersion)
    }
    catch {
        Write-Probe "WMI NPU driver enumeration failed: $($_.Exception.Message)"
        $npuDrivers = @()
    }
    if ($npuDrivers.Count -eq 0) {
        try {
            $npuDrivers = @(Get-PnpDevice -PresentOnly -ErrorAction Stop |
                Where-Object { $_.FriendlyName -match '\bNPU\b|Neural Processing|AI Boost|Ryzen AI|Hexagon' -or
                    $_.Name -match '\bNPU\b|Neural Processing|AI Boost|Ryzen AI|Hexagon' } |
                ForEach-Object {
                    $property = Get-PnpDeviceProperty -InstanceId $_.InstanceId `
                        -KeyName 'DEVPKEY_Device_DriverVersion' -ErrorAction SilentlyContinue
                    [pscustomobject]@{
                        DeviceName = if ($_.FriendlyName) { $_.FriendlyName } else { $_.Name }
                        DriverVersion = [string]$property.Data
                    }
                })
            Write-Probe 'NPU driver versions read through Get-PnpDeviceProperty.'
        }
        catch {
            Write-Probe "Get-PnpDevice NPU driver enumeration failed: $($_.Exception.Message)"
            $npuDrivers = @()
        }
    }
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
    }
    if ($DriverCheckOnly -and $SkipHardwarePreflight) {
        throw 'DriverCheckOnly cannot be combined with SkipHardwarePreflight.'
    }
    if ($DriverCheckOnly) {
        $probeResult = 'PASS'
        $probeReasonCode = 'driver_preflight_success'
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
    Write-Probe 'Skipping --list-devices; testing explicit device launch.'

    $model = Find-GgufModel $ModelPath
    $modelInfo = Get-Item -LiteralPath $model
    Write-Probe "GGUF model: $model ($([math]::Round($modelInfo.Length / 1GB, 2)) GB)"

    $port = Get-FreePort
    $args = @(
        '--model', $model,
        '--host', '127.0.0.1',
        '--port', "$port",
        '--ctx-size', '512',
        '--n-gpu-layers', '999',
        '--threads', '4',
        '--parallel', '1',
        '--verbose'
    )
    if (-not $SkipOpenVinoDevice) {
        $args = @('--device', $DeviceArgument) + $args
    }
    $deviceSummary = if ($SkipOpenVinoDevice) { 'not supplied' } else { $DeviceArgument }
    Write-Probe "Starting NPU server: --device $deviceSummary --n-gpu-layers 999 --parallel 1"

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
    if ($SkipOpenVinoDevice) {
        [void]$startInfo.EnvironmentVariables.Remove('GGML_OPENVINO_DEVICE')
    } else {
        $startInfo.EnvironmentVariables['GGML_OPENVINO_DEVICE'] = $OpenVinoDevice
    }
    $startInfo.EnvironmentVariables['GGML_OPENVINO_ENABLE_FALLBACK'] = '0'
    $startInfo.EnvironmentVariables['GGML_OPENVINO_STATEFUL_EXECUTION'] = '0'
    $startInfo.EnvironmentVariables['GGML_OPENVINO_PROFILING'] = '1'
    $startInfo.EnvironmentVariables['OV_NPU_LOG_LEVEL'] = 'LOG_INFO'
    $envSummary = if ($SkipOpenVinoDevice) { 'GGML_OPENVINO_DEVICE=<unset>' } else { "GGML_OPENVINO_DEVICE=$OpenVinoDevice" }
    Write-Probe "Launch environment: $envSummary; GGML_OPENVINO_ENABLE_FALLBACK=0; GGML_OPENVINO_STATEFUL_EXECUTION=0"
    $serverProcess = New-Object System.Diagnostics.Process
    $serverProcess.StartInfo = $startInfo
    if (-not $serverProcess.Start()) {
        throw 'llama-server process could not start.'
    }
    $stdoutTask = $serverProcess.StandardOutput.ReadToEndAsync()
    $stderrTask = $serverProcess.StandardError.ReadToEndAsync()

    $ready = $false
    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    $lastHeartbeat = Get-Date
    while ((Get-Date) -lt $deadline) {
        if ($serverProcess.HasExited) {
            throw "llama-server exited early with code $($serverProcess.ExitCode)"
        }
        if (((Get-Date) - $lastHeartbeat).TotalSeconds -ge 30) {
            $elapsed = [Math]::Floor(((Get-Date) - $deadline.AddSeconds(-$StartupTimeoutSeconds)).TotalSeconds)
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
        throw "llama-server did not become ready within $StartupTimeoutSeconds seconds."
    }
    Write-Probe 'llama-server health check passed.'

    $body = @{
        messages = @(@{ role = 'user'; content = 'Reply with only OK.' })
        max_tokens = 16
        temperature = 0
        chat_template_kwargs = @{ enable_thinking = $false }
    } | ConvertTo-Json -Depth 6 -Compress
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/v1/chat/completions" -Method Post `
        -ContentType 'application/json; charset=utf-8' -Body $body -TimeoutSec $InferenceTimeoutSeconds -UseBasicParsing
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
    $failureMessage = $_.Exception.Message
    if ($serverProcess -and $serverProcess.HasExited) {
        $serverExitCode = $serverProcess.ExitCode
    }
    Write-Probe "FAIL: $($_.Exception.Message)"
    throw
}
finally {
    if ($serverProcess) {
        if (-not $serverProcess.HasExited) {
            $serverProcess.Kill()
            $serverProcess.WaitForExit()
        }
        if ($serverProcess.HasExited) {
            $serverExitCode = $serverProcess.ExitCode
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
                $probeReasonCode = 'cpu_fallback_or_npu_unavailable'
                Write-Probe 'FAIL: llama.cpp reported NPU fallback or unavailable NPU.'
                $failureMessage = 'NPU is unavailable or fell back to CPU.'
            }
            elseif (-not $npuMarkerDetected) {
                $probeReasonCode = 'no_npu_execution_marker'
                Write-Probe 'FAIL: no explicit OpenVINO NPU execution marker was found.'
                $failureMessage = 'No proof of OpenVINO NPU execution was found in llama-server logs.'
            }
            else {
                $probeResult = 'PASS'
                $probeReasonCode = 'npu_inference_success'
                Write-Probe 'PASS: health check and inference succeeded on OpenVINO NPU; no CPU fallback marker found.'
            }
        } elseif ($fallbackDetected) {
            $probeReasonCode = 'cpu_fallback_or_npu_unavailable'
            Write-Probe 'DIAGNOSTIC: llama.cpp reported NPU fallback or unavailable NPU before inference completed.'
        } elseif (-not $npuMarkerDetected) {
            $probeReasonCode = 'no_npu_execution_marker'
            Write-Probe 'DIAGNOSTIC: no explicit OpenVINO NPU execution marker was found before inference failed.'
        }
        if ($probeResult -ne 'PASS' -and -not $failureMessage) {
            $probeReasonCode = Get-FailureReasonCode '' $combined (Get-SafeExitCode $serverExitCode)
        }
    } elseif (-not $failureMessage) {
        $failureMessage = 'llama-server did not start.'
    }
    if ($probeResult -ne 'PASS' -and $probeReasonCode -in @('', 'probe_failed')) {
        $probeReasonCode = Get-FailureReasonCode $failureMessage $combined (Get-SafeExitCode $serverExitCode)
    }
    # Do this once outside the hashtable expression. Windows PowerShell's
    # parser makes a chained -or expression in a hashtable value surprisingly
    # easy to misread, and an access-denied inventory failure must never be
    # reported as a generic missing-device failure.
    $inventoryErrorText = (($npuInventoryErrors | ForEach-Object { [string]$_ }) -join "`n")
    $inventoryAccessDeniedEvidence = $false
    if ($npuInventoryAccessDenied) {
        $inventoryAccessDeniedEvidence = $true
    }
    if (Test-AccessDeniedText $inventoryErrorText) {
        $inventoryAccessDeniedEvidence = $true
    }
    if ($probeReasonCode -eq 'npu_device_inventory_access_denied') {
        $inventoryAccessDeniedEvidence = $true
    }
    Write-Probe "Inventory evidence: access_denied=$inventoryAccessDeniedEvidence error_count=$($npuInventoryErrors.Count)" | Out-Null
    if ($probeResult -ne 'PASS' -and -not $inferenceSucceeded -and $inventoryAccessDeniedEvidence) {
        $probeReasonCode = 'npu_device_inventory_access_denied'
    }
    $summary = [ordered]@{
        schema_version = $ProbeSchemaVersion
        run_id = $RunId
        started_at = $StartedAt
        finished_at = (Get-Date).ToString('o')
        probe_script = $PSCommandPath
        probe_pid = $PID
        result = $probeResult
        reason_code = $probeReasonCode
        failure = $failureMessage
        exit_code = $serverExitCode
        device_argument = if ($SkipOpenVinoDevice) { $null } else { $DeviceArgument }
        openvino_device_env = if ($SkipOpenVinoDevice) { $null } else { $OpenVinoDevice }
        hardware_preflight_skipped = [bool]$SkipHardwarePreflight
        npu_devices = $npuDevices
        npu_drivers = $npuDrivers
        npu_inventory_access_denied = $inventoryAccessDeniedEvidence
        npu_inventory_pnputil_exit_code = $npuInventoryPnpUtilExitCode
        npu_inventory_commands = @($npuInventoryCommands)
        hardware_inventory_errors = @($npuInventoryErrors)
        npu_driver_version = if ($newestNpuDriver) { [string]$newestNpuDriver.Version } else { $null }
        fallback_disabled = $true
        inference_succeeded = $inferenceSucceeded
        npu_marker_detected = $npuMarkerDetected
        fallback_detected = $fallbackDetected
        log = $LogPath
        stdout_log = $ServerOutPath
        stderr_log = $ServerErrPath
    }
    $summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ProbeSummaryPath -Encoding utf8
}
if ($probeResult -ne 'PASS') { exit 1 }
