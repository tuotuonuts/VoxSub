[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ModelPath,
    [string]$LlamaDir = $env:VOXSUB_LLAMA_DIR,
    [string]$PythonPath = "",
    [string]$OutputDir = "",
    [ValidateRange(1, 20)]
    [int]$Iterations = 3,
    [ValidateRange(0, 300)]
    [int]$DelaySeconds = 2,
    [ValidateRange(30, 3600)]
    [int]$StartupTimeoutSeconds = 600,
    [ValidateRange(5, 600)]
    [int]$InferenceTimeoutSeconds = 60,
    [switch]$SkipHardwarePreflight
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ModelPath = [System.IO.Path]::GetFullPath($ModelPath)
if (-not (Test-Path -LiteralPath $ModelPath -PathType Leaf)) {
    throw "GGUF model does not exist: $ModelPath"
}

function Resolve-Python {
    if ($PythonPath) {
        $candidate = [System.IO.Path]::GetFullPath($PythonPath)
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        throw "Python executable does not exist: $candidate"
    }
    $candidates = @(
        (Join-Path $RepoRoot '.venv\Scripts\python.exe'),
        (Join-Path $RepoRoot '.venv-codex\Scripts\python.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    throw 'Python 3.11+ was not found. Run 更新并启动测试版.bat first.'
}

function Resolve-LlamaDir {
    if ($LlamaDir) {
        $candidate = [System.IO.Path]::GetFullPath($LlamaDir)
        if (Test-Path -LiteralPath (Join-Path $candidate 'llama-server.exe') -PathType Leaf) {
            return $candidate
        }
        throw "llama-server.exe does not exist in: $candidate"
    }
    $roots = @(
        (Join-Path $RepoRoot '.npu-assets\openvino'),
        (Join-Path $env:LOCALAPPDATA 'VoxSub\tools\llama')
    )
    if ($env:TEMP) {
        $roots += @(Get-ChildItem -LiteralPath $env:TEMP -Directory -Filter 'VoxSub_npu_runtime_*' -ErrorAction SilentlyContinue |
            ForEach-Object { $_.FullName })
    }
    foreach ($root in $roots | Select-Object -Unique) {
        if (-not (Test-Path -LiteralPath $root -PathType Container)) { continue }
        $server = Get-ChildItem -LiteralPath $root -Filter 'llama-server.exe' -File -Recurse -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($server) { return $server.DirectoryName }
    }
    throw 'OpenVINO llama-server.exe was not found. Provide -LlamaDir.'
}

function Write-Utf8([string]$Path, [object]$Value) {
    $Value | Out-File -LiteralPath $Path -Encoding utf8
}

function Get-SafeInventory([scriptblock]$Action, [string]$Label) {
    try {
        return @(& $Action)
    }
    catch {
        return [ordered]@{
            source = $Label
            error = $_.Exception.Message
        }
    }
}

function Read-ProbeSummary([string]$Directory) {
    $path = Join-Path $Directory 'probe-summary.json'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return [ordered]@{ result = 'FAIL'; reason_code = 'probe_summary_missing'; summary = $path }
    }
    try {
        $summary = Get-Content -LiteralPath $path -Raw -Encoding utf8 | ConvertFrom-Json
        if ([int]$summary.schema_version -lt 2 -or
            [string]::IsNullOrWhiteSpace([string]$summary.run_id)) {
            return [ordered]@{
                result = 'FAIL'
                reason_code = 'probe_summary_stale_or_invalid'
                exit_code = $null
                run_id = $null
                summary = $path
            }
        }
        return [ordered]@{
            result = [string]$summary.result
            reason_code = [string]$summary.reason_code
            exit_code = $summary.exit_code
            run_id = [string]$summary.run_id
            schema_version = [int]$summary.schema_version
            summary = $path
        }
    }
    catch {
        return [ordered]@{ result = 'FAIL'; reason_code = 'probe_summary_invalid'; summary = $path }
    }
}

function Invoke-Probe([hashtable]$Variant, [string[]]$Arguments, [string]$Directory, [string]$LogPath) {
    $started = Get-Date
    $output = @(& powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass @Arguments 2>&1 | Out-String)
    $exitCode = $LASTEXITCODE
    Write-Utf8 $LogPath ($output -join '')
    $summary = Read-ProbeSummary $Directory
    return [ordered]@{
        name = $Variant.name
        description = $Variant.description
        device_argument = $Variant.device_argument
        openvino_device_env = $Variant.openvino_device_env
        skip_openvino_device_env = [bool]$Variant.skip_openvino_device_env
        exit_code = $exitCode
        elapsed_seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 3)
        log = $LogPath
        probe = $summary
    }
}

function Invoke-AppProbe([string]$Name, [string[]]$Arguments, [string]$LogPath, [string]$Python) {
    $started = Get-Date
    $output = @(& $Python @Arguments 2>&1 | Out-String)
    $exitCode = $LASTEXITCODE
    Write-Utf8 $LogPath ($output -join '')
    return [ordered]@{
        name = $Name
        exit_code = $exitCode
        elapsed_seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 3)
        log = $LogPath
    }
}

try {
    $python = Resolve-Python
    $runtimeDir = Resolve-LlamaDir
    if (-not $OutputDir) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $OutputDir = Join-Path $env:LOCALAPPDATA "VoxSub\diagnostics\npu-matrix\$stamp"
    }
    $OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

    $systemSnapshot = [ordered]@{
        timestamp = (Get-Date).ToString('o')
        computer = $env:COMPUTERNAME
        os = Get-SafeInventory { Get-CimInstance Win32_OperatingSystem -ErrorAction Stop | Select-Object Caption,Version,BuildNumber } 'Win32_OperatingSystem'
        cpu = Get-SafeInventory { Get-CimInstance Win32_Processor -ErrorAction Stop | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors } 'Win32_Processor'
        npu_devices = Get-SafeInventory { Get-PnpDevice -PresentOnly -ErrorAction Stop |
            Where-Object { $_.FriendlyName -match '\bNPU\b|Neural Processing|AI Boost|Ryzen AI|Hexagon' } |
            Select-Object FriendlyName,Status,InstanceId } 'Get-PnpDevice'
        npu_drivers = Get-SafeInventory { Get-CimInstance Win32_PnPSignedDriver -ErrorAction Stop |
            Where-Object { $_.DeviceName -match '\bNPU\b|Neural Processing|AI Boost|Ryzen AI|Hexagon' } |
            Select-Object DeviceName,DriverVersion,Manufacturer } 'Win32_PnPSignedDriver'
        hardware_preflight_skipped = [bool]$SkipHardwarePreflight
        python = $python
        runtime_dir = $runtimeDir
        model = $ModelPath
    }
    Write-Utf8 (Join-Path $OutputDir 'system-snapshot.json') ($systemSnapshot | ConvertTo-Json -Depth 8)

    $results = @()
    # The first entry is VoxSub's production launch contract. The other two
    # are direct-server diagnostics: they never alter normal application use.
    $variants = @(
        @{ name = 'production_openvino0_env_npu'; description = 'VoxSub production contract: --device OPENVINO0 plus GGML_OPENVINO_DEVICE=NPU'; device_argument = 'OPENVINO0'; openvino_device_env = 'NPU'; skip_openvino_device_env = $false },
        @{ name = 'explicit_npu_env_npu'; description = 'Diagnostic: --device NPU plus GGML_OPENVINO_DEVICE=NPU'; device_argument = 'NPU'; openvino_device_env = 'NPU'; skip_openvino_device_env = $false },
        @{ name = 'openvino0_without_device_env'; description = 'Diagnostic: --device OPENVINO0 with device environment unset'; device_argument = 'OPENVINO0'; openvino_device_env = ''; skip_openvino_device_env = $true }
    )
    for ($index = 1; $index -le $Iterations; $index++) {
        $runDir = Join-Path $OutputDir ("run-{0:D2}" -f $index)
        New-Item -ItemType Directory -Path $runDir -Force | Out-Null
        Write-Host "NPU probe $index/${Iterations}: three direct llama-server launch variants + forced VoxSub path"
        $direct = @()
        foreach ($variant in $variants) {
            $variantDir = Join-Path $runDir ("direct-" + $variant.name)
            $directArgs = @(
                '-File', (Join-Path $RepoRoot 'scripts\npu_probe.ps1'),
                '-ModelPath', $ModelPath,
                '-LlamaDir', $runtimeDir,
                '-OutputDir', $variantDir,
                '-DeviceArgument', $variant.device_argument,
                '-StartupTimeoutSeconds', "$StartupTimeoutSeconds",
                '-InferenceTimeoutSeconds', "$InferenceTimeoutSeconds"
            )
            if ($variant.skip_openvino_device_env) {
                $directArgs += '-SkipOpenVinoDevice'
            } else {
                $directArgs += @('-OpenVinoDevice', $variant.openvino_device_env)
            }
            if ($SkipHardwarePreflight) {
                $directArgs += '-SkipHardwarePreflight'
            }
            $direct += Invoke-Probe $variant $directArgs $variantDir (Join-Path $runDir ("direct-" + $variant.name + '-command.log'))
        }

        $appArgs = @(
            (Join-Path $RepoRoot 'scripts\npu_app_probe.py'),
            '--model-id', 'matrix-probe',
            '--model-path', $ModelPath,
            '--runtime-dir', $runtimeDir,
            '--output-dir', (Join-Path $runDir 'voxsub-forced-npu'),
            '--force', 'npu'
        )
        $app = Invoke-AppProbe 'voxsub_forced_npu' $appArgs (Join-Path $runDir 'voxsub-command.log') $python

        $automatic = $null
        if ($index -eq 1) {
            $autoArgs = @(
                (Join-Path $RepoRoot 'scripts\npu_app_probe.py'),
                '--model-id', 'matrix-probe',
                '--model-path', $ModelPath,
                '--runtime-dir', $runtimeDir,
                '--output-dir', (Join-Path $runDir 'voxsub-automatic'),
                '--force', 'auto'
            )
            $automatic = Invoke-AppProbe 'voxsub_automatic' $autoArgs (Join-Path $runDir 'voxsub-automatic-command.log') $python
        }

        $results += [ordered]@{
            iteration = $index
            direct = $direct[0]
            direct_variants = $direct
            forced_voxsub = $app
            automatic_voxsub = $automatic
        }
        if ($index -lt $Iterations -and $DelaySeconds -gt 0) {
            Start-Sleep -Seconds $DelaySeconds
        }
    }

    $productionDirect = @($results | ForEach-Object { @($_.direct_variants | Where-Object { $_.name -eq 'production_openvino0_env_npu' }) })
    $directPasses = @($results | ForEach-Object { @($_.direct_variants | Where-Object { $_.probe.result -eq 'PASS' }) })
    $appPasses = @($results | Where-Object { $_.forced_voxsub.exit_code -eq 0 })
    $summary = [ordered]@{
        schema_version = 2
        result = if ($productionDirect.Count -eq $Iterations -and
            @($productionDirect | Where-Object { $_.probe.result -ne 'PASS' }).Count -eq 0 -and
            $appPasses.Count -eq $Iterations) { 'PASS' } else { 'FAIL' }
        diagnosis = if ($directPasses.Count -gt 0 -and $appPasses.Count -eq 0) { 'direct_npu_works_but_voxsub_path_fails' } elseif ($directPasses.Count -gt 0) { 'one_or_more_direct_variants_proved_npu_inference' } else { 'no_variant_proved_npu_inference' }
        iterations = $Iterations
        model = $ModelPath
        runtime_dir = $runtimeDir
        python = $python
        output_dir = $OutputDir
        results = $results
        note = 'Only production_openvino0_env_npu is VoxSub normal behavior. Other direct variants are diagnostics and never change application routing. automatic_voxsub is informational because default routing may intentionally choose a discrete GPU before NPU.'
    }
    $summaryPath = Join-Path $OutputDir 'matrix-summary.json'
    Write-Utf8 $summaryPath ($summary | ConvertTo-Json -Depth 12)
    $summary | ConvertTo-Json -Depth 12
    if ($summary.result -eq 'PASS') { exit 0 }
    exit 1
}
catch {
    Write-Host ("ERROR: " + $_.Exception.Message) -ForegroundColor Red
    exit 2
}
