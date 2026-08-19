param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDir,
    [Parameter(Mandatory = $true)]
    [string]$CacheRoot
)

$ErrorActionPreference = "Stop"
$LlamaVersion = "b10470"
$OpenVinoVersion = "2026.2.1"
$Root = Split-Path -Parent $PSScriptRoot

function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required build command is missing: $Name"
    }
}

function Require-File([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required NPU runtime file is missing: $Path"
    }
}

$Required = @(
    "llama-server.exe",
    "ggml-openvino.dll",
    "openvino_intel_npu_plugin.dll",
    "runtime-dependencies.txt"
)
if ((@($Required | Where-Object {
    Test-Path -LiteralPath (Join-Path $OutputDir $_) -PathType Leaf
}).Count) -eq $Required.Count) {
    Write-Host "[npu-runtime] reuse existing runtime: $OutputDir" -ForegroundColor Green
    exit 0
}

Require-Command "git"
Require-Command "cmake"
Require-Command "curl.exe"
New-Item -ItemType Directory -Path $CacheRoot, $OutputDir -Force | Out-Null

$sourceRoot = Join-Path $CacheRoot "llama-src-$LlamaVersion"
$buildRoot = Join-Path $CacheRoot "llama-build-no-npuw-$LlamaVersion"
$openVinoRoot = Join-Path $CacheRoot "openvino-toolkit-$OpenVinoVersion"
$vcpkgRoot = Join-Path $CacheRoot "vcpkg"

if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot "CMakeLists.txt") -PathType Leaf)) {
    if (Test-Path -LiteralPath $sourceRoot) {
        Remove-Item -LiteralPath $sourceRoot -Recurse -Force
    }
    git clone --depth 1 --branch $LlamaVersion https://github.com/ggml-org/llama.cpp.git $sourceRoot
    if ($LASTEXITCODE -ne 0) { throw "llama.cpp clone failed (exit $LASTEXITCODE)." }
}

# The private NPUW compile configuration in the official b10470 source is not
# accepted by the Intel NPU driver used by the supported Windows probe machine.
# Remove only that option; the public OpenVINO graph path remains unchanged.
$extra = Join-Path $sourceRoot "ggml\src\ggml-openvino\ggml-openvino-extra.cpp"
Require-File $extra
$source = Get-Content -LiteralPath $extra -Raw
$configPattern = New-Object Text.RegularExpressions.Regex(
    '(?s)\s*if \(device_name == "NPU"\) \{.*?\}\s*else if \(cache_dir && strlen\(cache_dir\) > 0\) \{\s*compile_config\.insert\(ov::cache_dir\(cache_dir\)\);\s*compile_config\.insert\(ov::cache_mode\(ov::CacheMode::OPTIMIZE_SIZE\)\);\s*\}'
)
$patchedSource = $configPattern.Replace(
    $source,
    "`r`n    if (cache_dir && strlen(cache_dir) > 0) {`r`n        compile_config.insert(ov::cache_dir(cache_dir));`r`n        compile_config.insert(ov::cache_mode(ov::CacheMode::OPTIMIZE_SIZE));`r`n    }",
    1
)
if ($patchedSource -ne $source) {
    Set-Content -LiteralPath $extra -Value $patchedSource -Encoding utf8
} elseif ($source -match 'NPU_COMPILER_DYNAMIC_QUANTIZATION|NPU_USE_NPUW|NPUW_[A-Z_]+') {
    throw "Could not remove the private NPUW compile configuration."
}
$finalSource = Get-Content -LiteralPath $extra -Raw
if ($finalSource -match 'NPU_COMPILER_DYNAMIC_QUANTIZATION|NPU_USE_NPUW|NPUW_[A-Z_]+') {
    throw "A private NPUW compile option remains after patching."
}

$openVinoZip = Join-Path $CacheRoot "openvino-toolkit-windows-$OpenVinoVersion.zip"
$openVinoUrl = "https://storage.openvinotoolkit.org/repositories/openvino/packages/$OpenVinoVersion/windows/openvino_toolkit_windows_2026.2.1.21919.ede283a88e3_x86_64.zip"
if (-not (Test-Path -LiteralPath $openVinoZip -PathType Leaf)) {
    curl.exe --ipv4 --fail --location --retry 5 --retry-delay 5 --connect-timeout 20 `
        --max-time 1800 --output $openVinoZip $openVinoUrl
    if ($LASTEXITCODE -ne 0) { throw "OpenVINO SDK download failed (exit $LASTEXITCODE)." }
}
if (-not (Test-Path -LiteralPath (Join-Path $openVinoRoot "*"))) {
    Expand-Archive -LiteralPath $openVinoZip -DestinationPath $openVinoRoot -Force
}
$ovConfig = Get-ChildItem -LiteralPath $openVinoRoot -Filter "OpenVINOConfig.cmake" -File -Recurse |
    Select-Object -First 1
if (-not $ovConfig) { throw "OpenVINOConfig.cmake was not found after extraction." }
$ovCmakeDir = Split-Path -Parent $ovConfig.FullName
$ovPackageRoot = Split-Path -Parent (Split-Path -Parent $ovCmakeDir)

if (-not (Test-Path -LiteralPath (Join-Path $vcpkgRoot "vcpkg.exe") -PathType Leaf)) {
    if (Test-Path -LiteralPath $vcpkgRoot) {
        Remove-Item -LiteralPath $vcpkgRoot -Recurse -Force
    }
    git clone --depth 1 https://github.com/microsoft/vcpkg.git $vcpkgRoot
    if ($LASTEXITCODE -ne 0) { throw "vcpkg clone failed (exit $LASTEXITCODE)." }
    & (Join-Path $vcpkgRoot "bootstrap-vcpkg.bat") -disableMetrics
    if ($LASTEXITCODE -ne 0) { throw "vcpkg bootstrap failed (exit $LASTEXITCODE)." }
}
& (Join-Path $vcpkgRoot "vcpkg.exe") install opencl --triplet x64-windows --disable-metrics
if ($LASTEXITCODE -ne 0) { throw "vcpkg OpenCL install failed (exit $LASTEXITCODE)." }

cmake -S $sourceRoot -B $buildRoot -G "Visual Studio 17 2022" -A x64 `
    -DGGML_OPENVINO=ON -DLLAMA_CURL=OFF -DLLAMA_OPENSSL=OFF `
    -DOpenVINO_DIR="$ovCmakeDir" `
    -DCMAKE_TOOLCHAIN_FILE="$(Join-Path $vcpkgRoot 'scripts\buildsystems\vcpkg.cmake')"
if ($LASTEXITCODE -ne 0) { throw "llama.cpp CMake configure failed (exit $LASTEXITCODE)." }
cmake --build $buildRoot --config Release --target llama-server --parallel 4
if ($LASTEXITCODE -ne 0) { throw "llama.cpp build failed (exit $LASTEXITCODE)." }

$runtimeDir = Join-Path $buildRoot "bin\Release"
Require-File (Join-Path $runtimeDir "llama-server.exe")
if (Test-Path -LiteralPath $OutputDir) {
    Get-ChildItem -LiteralPath $OutputDir -Force | Remove-Item -Recurse -Force
}
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
Copy-Item -Path (Join-Path $runtimeDir "*") -Destination $OutputDir -Recurse -Force
$ovRuntimeBin = Join-Path $ovPackageRoot "runtime\bin\intel64\Release"
if (-not (Test-Path -LiteralPath $ovRuntimeBin -PathType Container)) {
    throw "OpenVINO runtime bin directory was not found: $ovRuntimeBin"
}
Copy-Item -Path (Join-Path $ovRuntimeBin "*") -Destination $OutputDir -Recurse -Force
$openClDll = Join-Path $vcpkgRoot "installed\x64-windows\bin\OpenCL.dll"
if (Test-Path -LiteralPath $openClDll -PathType Leaf) {
    Copy-Item -LiteralPath $openClDll -Destination $OutputDir -Force
}

$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) {
    throw "Visual Studio vswhere.exe is required to package the C++ runtime dependencies."
}
$vsInstall = (& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Redist.14.Latest -property installationPath |
    Select-Object -First 1)
if (-not $vsInstall) { throw "Visual C++ redistributable directory could not be located." }
$vcRuntimeDll = Get-ChildItem -LiteralPath (Join-Path $vsInstall "VC\Redist\MSVC") `
    -Filter "vcruntime140.dll" -File -Recurse |
    Where-Object {
        $_.FullName -match "(?i)\\x64\\Microsoft\.VC[^\\]*\.CRT\\vcruntime140\.dll$" -and
        $_.FullName -notmatch "(?i)\\onecore\\"
    } | Sort-Object FullName -Descending | Select-Object -First 1
if (-not $vcRuntimeDll) { throw "The x64 Visual C++ runtime DLL directory was not found." }
Copy-Item -Path (Join-Path $vcRuntimeDll.Directory.FullName "*.dll") -Destination $OutputDir -Force
$vcompDll = Get-ChildItem -LiteralPath (Join-Path $vsInstall "VC\Redist\MSVC") `
    -Filter "vcomp140.dll" -File -Recurse |
    Where-Object {
        $_.FullName -match "(?i)\\x64\\Microsoft\.VC[^\\]*\.OpenMP\\vcomp140\.dll$" -and
        $_.FullName -notmatch "(?i)\\onecore\\"
    } | Sort-Object FullName -Descending | Select-Object -First 1
if (-not $vcompDll) { throw "The x64 Visual C++ OpenMP runtime DLL was not found." }
Copy-Item -LiteralPath $vcompDll.FullName -Destination $OutputDir -Force
if (-not (Test-Path -LiteralPath (Join-Path $OutputDir "tbb12.dll") -PathType Leaf)) {
    $tbbDll = Get-ChildItem -LiteralPath $openVinoRoot -Filter "tbb12.dll" -File -Recurse |
        Sort-Object FullName -Descending | Select-Object -First 1
    if (-not $tbbDll) { throw "The OpenVINO x64 tbb12.dll dependency was not found." }
    Copy-Item -LiteralPath $tbbDll.FullName -Destination $OutputDir -Force
}

$dumpbin = Get-ChildItem -LiteralPath (Join-Path $vsInstall "VC\Tools\MSVC") `
    -Filter "dumpbin.exe" -File -Recurse |
    Where-Object { $_.FullName -match "(?i)\\bin\\Hostx64\\x64\\dumpbin\.exe$" } |
    Sort-Object FullName -Descending | Select-Object -First 1
if (-not $dumpbin) { throw "The x64 dumpbin.exe tool was not found." }
$dependencyReport = Join-Path $OutputDir "runtime-dependencies.txt"
Set-Content -LiteralPath $dependencyReport -Value "build: llama.cpp $LlamaVersion; OpenVINO $OpenVinoVersion; no-NPUW patch`r`ndumpbin: $($dumpbin.FullName)" -Encoding utf8
$runtimeBinaries = Get-ChildItem -LiteralPath $OutputDir -File |
    Where-Object { $_.Extension -in @(".exe", ".dll") } | Sort-Object Name
foreach ($runtimeBinary in $runtimeBinaries) {
    Add-Content -LiteralPath $dependencyReport -Value "`r`n===== $($runtimeBinary.Name) =====" -Encoding utf8
    $dependencyOutput = & $dumpbin.FullName /dependents $runtimeBinary.FullName 2>&1
    if ($LASTEXITCODE -ne 0) { throw "dumpbin failed for $($runtimeBinary.Name) (exit $LASTEXITCODE)." }
    Add-Content -LiteralPath $dependencyReport -Value $dependencyOutput -Encoding utf8
}
Get-ChildItem -LiteralPath $OutputDir -File | Sort-Object Name |
    ForEach-Object { "$($_.Name)`t$($_.Length)" } |
    Set-Content -LiteralPath (Join-Path $OutputDir "runtime-files.txt") -Encoding utf8

foreach ($item in $Required) { Require-File (Join-Path $OutputDir $item) }
Write-Host "[npu-runtime] built reproducible no-NPUW runtime: $OutputDir" -ForegroundColor Green
