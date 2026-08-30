[CmdletBinding()]
param(
    [string]$Destination = (Join-Path $env:LOCALAPPDATA "VoxSub\tools\llama"),
    [string]$CacheRoot = (Join-Path $env:LOCALAPPDATA "VoxSub\cache\llama-runtime")
)

$ErrorActionPreference = "Stop"
$manifest = Import-PowerShellDataFile (Join-Path $PSScriptRoot "llama_runtime_manifest.psd1")
$releaseBase = "https://github.com/ggml-org/llama.cpp/releases/download/$($manifest.Version)"

function Test-LlamaRuntime {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Asset,
        [Parameter(Mandatory = $true)][string]$Directory
    )

    if (-not (Test-Path -LiteralPath (Join-Path $Directory "llama-server.exe") -PathType Leaf)) {
        return $false
    }
    if ($Asset.RequiredDll -and
        -not (Test-Path -LiteralPath (Join-Path $Directory $Asset.RequiredDll) -PathType Leaf)) {
        return $false
    }
    return @(Get-ChildItem -LiteralPath $Directory -Filter "*.dll" -File -ErrorAction SilentlyContinue).Count -gt 0
}

function Get-VerifiedArchive {
    param([Parameter(Mandatory = $true)][hashtable]$Asset)

    $archive = Join-Path $CacheRoot $Asset.File
    if (Test-Path -LiteralPath $archive) {
        $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
        if ($actual -eq $Asset.Sha256) {
            return $archive
        }
        Remove-Item -LiteralPath $archive -Force
    }

    $partial = "$archive.part"
    if (Test-Path -LiteralPath $partial) {
        Remove-Item -LiteralPath $partial -Force
    }
    $url = "$releaseBase/$($Asset.File)"
    Write-Host "下载 llama.cpp $($Asset.Name) 运行时..."
    Invoke-WebRequest -Uri $url -OutFile $partial -UseBasicParsing
    $actual = (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash
    if ($actual -ne $Asset.Sha256) {
        Remove-Item -LiteralPath $partial -Force
        throw "llama.cpp $($Asset.Name) 运行时 SHA256 校验失败。"
    }
    Move-Item -LiteralPath $partial -Destination $archive
    return $archive
}

function Install-LlamaRuntime {
    param([Parameter(Mandatory = $true)][hashtable]$Asset)

    $archive = Get-VerifiedArchive -Asset $Asset
    $staging = Join-Path $CacheRoot ("extract-" + $Asset.Name + "-" + [guid]::NewGuid().ToString("N"))
    $pending = Join-Path $Destination ("." + $Asset.Name + ".pending-" + [guid]::NewGuid().ToString("N"))
    $target = Join-Path $Destination $Asset.Name
    $backup = $null
    try {
        New-Item -ItemType Directory -Force -Path $staging, $Destination | Out-Null
        Expand-Archive -LiteralPath $archive -DestinationPath $staging -Force
        $server = Get-ChildItem -LiteralPath $staging -Filter "llama-server.exe" -File -Recurse |
            Select-Object -First 1
        if (-not $server) {
            throw "llama.cpp $($Asset.Name) 压缩包不含 llama-server.exe。"
        }
        New-Item -ItemType Directory -Force -Path $pending | Out-Null
        Copy-Item -Path (Join-Path $server.Directory "*") -Destination $pending -Recurse -Force
        if (-not (Test-LlamaRuntime -Asset $Asset -Directory $pending)) {
            throw "llama.cpp $($Asset.Name) 解压后的运行时不完整。"
        }
        if (Test-Path -LiteralPath $target) {
            $backup = Join-Path $Destination ("." + $Asset.Name + ".backup-" + [guid]::NewGuid().ToString("N"))
            Move-Item -LiteralPath $target -Destination $backup
        }
        Move-Item -LiteralPath $pending -Destination $target
        if ($backup -and (Test-Path -LiteralPath $backup)) {
            Remove-Item -LiteralPath $backup -Recurse -Force
        }
        Write-Host "llama.cpp $($Asset.Name) 运行时已就绪: $target"
    } catch {
        if ($backup -and (Test-Path -LiteralPath $backup) -and
            -not (Test-Path -LiteralPath $target)) {
            Move-Item -LiteralPath $backup -Destination $target
        }
        throw
    } finally {
        if (Test-Path -LiteralPath $staging) {
            Remove-Item -LiteralPath $staging -Recurse -Force
        }
        if (Test-Path -LiteralPath $pending) {
            Remove-Item -LiteralPath $pending -Recurse -Force
        }
    }
}

try {
    New-Item -ItemType Directory -Force -Path $CacheRoot, $Destination | Out-Null
    foreach ($asset in $manifest.Assets) {
        $target = Join-Path $Destination $asset.Name
        if (Test-LlamaRuntime -Asset $asset -Directory $target) {
            Write-Host "llama.cpp $($asset.Name) 运行时已存在。"
            continue
        }
        Install-LlamaRuntime -Asset $asset
    }
} catch {
    Write-Host "llama.cpp 运行时同步失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
