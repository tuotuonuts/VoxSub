[CmdletBinding()]
param(
    [string]$Destination = (Join-Path $env:LOCALAPPDATA "VoxSub\tools\llama"),
    [string]$CacheRoot = (Join-Path $env:LOCALAPPDATA "VoxSub\cache\llama-runtime")
)

$ErrorActionPreference = "Stop"
$manifest = Import-PowerShellDataFile (Join-Path $PSScriptRoot "llama_runtime_manifest.psd1")
$releaseBase = "https://github.com/ggml-org/llama.cpp/releases/download/$($manifest.Version)"
$maxDownloadAttempts = 3

function Get-ArchiveDiagnostics {
    param([Parameter(Mandatory = $true)][string]$Path)

    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    $buffer = New-Object byte[] 16
    $count = 0
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $count = $stream.Read($buffer, 0, $buffer.Length)
    } finally {
        $stream.Dispose()
    }
    $prefix = if ($count -gt 0) {
        (($buffer[0..($count - 1)] | ForEach-Object { "{0:X2}" -f $_ }) -join " ")
    } else {
        "<empty>"
    }
    [pscustomobject]@{
        Size = [int64]$item.Length
        Sha256 = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
        Prefix = $prefix
        IsZip = ($count -ge 2 -and $buffer[0] -eq 0x50 -and $buffer[1] -eq 0x4B)
    }
}

function Invoke-LlamaDownload {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][int]$Attempt
    )

    # curl.exe is included with supported Windows 10/11 versions. Use it on
    # retries because it follows GitHub's release-asset redirect independently
    # of Windows PowerShell's Invoke-WebRequest implementation.
    $curl = if ($Attempt -gt 1) { Get-Command "curl.exe" -ErrorAction SilentlyContinue } else { $null }
    if ($curl) {
        Write-Host "使用 curl.exe 重新下载 llama.cpp 运行时..."
        & $curl.Source --fail --location --retry 1 --connect-timeout 30 `
            --header "Accept: application/octet-stream" `
            --header "Cache-Control: no-cache" `
            --user-agent "VoxSub-runtime-bootstrap/$($manifest.Version)" `
            --output $Destination $Url
        if ($LASTEXITCODE -ne 0) {
            throw "curl.exe 下载失败，退出码 $LASTEXITCODE"
        }
        return
    }

    Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing `
        -UserAgent "VoxSub-runtime-bootstrap/$($manifest.Version)" `
        -Headers @{
            Accept = "application/octet-stream"
            "Cache-Control" = "no-cache"
        }
}

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
    foreach ($requiredDll in @($Asset.RequiredDlls)) {
        if ($requiredDll -and
            -not (Test-Path -LiteralPath (Join-Path $Directory $requiredDll) -PathType Leaf)) {
            return $false
        }
    }
    return @(Get-ChildItem -LiteralPath $Directory -Filter "*.dll" -File -ErrorAction SilentlyContinue).Count -gt 0
}

function Get-VerifiedArchive {
    param([Parameter(Mandatory = $true)][hashtable]$Asset)

    $archive = Join-Path $CacheRoot $Asset.File
    $expectedHash = ([string]$Asset.Sha256).ToUpperInvariant()
    $expectedSize = if ($Asset.Size) { [int64]$Asset.Size } else { $null }
    if (Test-Path -LiteralPath $archive) {
        $cached = Get-ArchiveDiagnostics -Path $archive
        if ($cached.Sha256 -eq $expectedHash -and
            ($null -eq $expectedSize -or $cached.Size -eq $expectedSize)) {
            return $archive
        }
        Write-Host ("缓存资产无效，将重新下载: {0} (size={1}, sha256={2}, prefix={3})" -f
            $Asset.Name, $cached.Size, $cached.Sha256.Substring(0, 12), $cached.Prefix) -ForegroundColor Yellow
        Remove-Item -LiteralPath $archive -Force
    }

    $partial = "$archive.part"
    $url = "$releaseBase/$($Asset.File)"
    $lastFailure = $null
    for ($attempt = 1; $attempt -le $maxDownloadAttempts; $attempt++) {
        if (Test-Path -LiteralPath $partial) {
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
        }
        Write-Host "下载 llama.cpp $($Asset.Name) 运行时 (第 $attempt/$maxDownloadAttempts 次)..."
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $downloadUrl = if ($attempt -eq 1) { $url } else { "$url?voxsub_retry=$attempt" }
            Invoke-LlamaDownload -Url $downloadUrl -Destination $partial -Attempt $attempt
            if (-not (Test-Path -LiteralPath $partial -PathType Leaf)) {
                throw "下载命令未生成文件"
            }
            $downloaded = Get-ArchiveDiagnostics -Path $partial
            $sizeMessage = if ($null -eq $expectedSize) { "未知" } else { [string]$expectedSize }
            if ($null -ne $expectedSize -and $downloaded.Size -ne $expectedSize) {
                throw ("下载大小不符 (expected={0} actual={1}; sha256={2}; prefix={3})" -f
                    $sizeMessage, $downloaded.Size, $downloaded.Sha256.Substring(0, 12), $downloaded.Prefix)
            }
            if (-not $downloaded.IsZip) {
                throw ("下载响应不是 ZIP (size={0}; sha256={1}; prefix={2})" -f
                    $downloaded.Size, $downloaded.Sha256.Substring(0, 12), $downloaded.Prefix)
            }
            if ($downloaded.Sha256 -ne $expectedHash) {
                throw ("SHA256 校验失败 (expected={0} actual={1}; size={2}; prefix={3})" -f
                    $expectedHash, $downloaded.Sha256, $downloaded.Size, $downloaded.Prefix)
            }
            Move-Item -LiteralPath $partial -Destination $archive -Force
            return $archive
        } catch {
            $lastFailure = $_.Exception.Message
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
            if ($attempt -lt $maxDownloadAttempts) {
                Write-Host "下载校验失败，将更换缓存请求并重试：$lastFailure" -ForegroundColor Yellow
                Start-Sleep -Seconds $attempt
            }
        }
    }
    throw "llama.cpp $($Asset.Name) 运行时同步失败（已重试 $maxDownloadAttempts 次）：$lastFailure。请检查代理/网关是否拦截 GitHub 下载。"
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
