[CmdletBinding()]
param(
    [ValidateRange(1, 100)][int]$Limit = 20,
    [string]$Query = "is:unresolved",
    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

try {
    $localAppData = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { $env:USERPROFILE }
    $diagnosticsDir = Join-Path $localAppData "VoxSub"
    $tokenPath = Join-Path $diagnosticsDir "sentry_auth_token.txt"
    $configPath = Join-Path $diagnosticsDir "sentry_api.json"
    if (-not (Test-Path -LiteralPath $tokenPath)) {
        throw "未找到 Sentry API Token。请先运行 .\scripts\initialize_sentry_diagnostics.ps1。"
    }
    if (-not (Test-Path -LiteralPath $configPath)) {
        throw "未找到 Sentry API 配置。请先运行 .\scripts\initialize_sentry_diagnostics.ps1。"
    }

    $token = (Get-Content -LiteralPath $tokenPath -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($token)) {
        throw "Sentry API Token 文件为空。请重新运行初始化脚本。"
    }
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    $base = ([string]$config.sentry_base).TrimEnd("/")
    $org = [string]$config.organization_slug
    $project = [string]$config.project_slug
    if ([string]::IsNullOrWhiteSpace($base) -or
        [string]::IsNullOrWhiteSpace($org) -or
        [string]::IsNullOrWhiteSpace($project)) {
        throw "Sentry API 配置不完整。请重新运行初始化脚本。"
    }

    $headers = @{ Authorization = "Bearer $token" }
    $encodedQuery = [uri]::EscapeDataString($Query)
    $uri = "$base/api/0/projects/$org/$project/issues/?query=$encodedQuery&limit=$Limit"
    $response = Invoke-RestMethod -Method Get -Headers $headers -Uri $uri
    # Windows PowerShell can retain a top-level JSON array as one ArrayList,
    # which otherwise renders an empty table instead of one row per Issue.
    $issues = @(
        foreach ($issue in $response) {
            $issue
        }
    )
    if ($AsJson) {
        $issues | ConvertTo-Json -Depth 8
    } elseif ($issues.Count -eq 0) {
        Write-Host "没有匹配的 Sentry Issue。"
    } else {
        $issues |
            Select-Object shortId, title, count, firstSeen, lastSeen, status |
            Format-Table -AutoSize
    }
} catch {
    Write-Host "读取 Sentry Issue 失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
