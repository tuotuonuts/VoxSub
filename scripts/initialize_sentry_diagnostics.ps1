[CmdletBinding()]
param(
    [string]$SentryBase = "https://de.sentry.io",
    [string]$ProjectSlug = "voxsub"
)

$ErrorActionPreference = "Stop"

function Get-TokenFromSecurePrompt {
    Write-Host "仅在此本机提示中粘贴 Sentry API Token；输入不会显示、不会写入 Git 或程序配置。" -ForegroundColor Yellow
    $secureToken = Read-Host "Sentry API Token" -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

function Protect-TokenFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        & icacls $Path /inheritance:r /grant:r "${identity}:(R,W)" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "icacls 退出码 $LASTEXITCODE"
        }
    } catch {
        Write-Warning "无法收紧令牌文件权限：$($_.Exception.Message)"
    }
}

function Invoke-SentryGet {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][hashtable]$Headers
    )

    return Invoke-RestMethod -Method Get -Headers $Headers -Uri "$normalizedBase$Path"
}

try {
    $normalizedBase = $SentryBase.TrimEnd("/")
    if (-not ($normalizedBase -match "^https://[^/]+$")) {
        throw "Sentry API 地址必须是 https://<host>，例如 https://de.sentry.io。"
    }
    if ([string]::IsNullOrWhiteSpace($ProjectSlug)) {
        throw "项目 slug 不能为空。"
    }

    $localAppData = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { $env:USERPROFILE }
    if (-not $localAppData) {
        throw "无法定位当前用户的本地数据目录。"
    }
    $diagnosticsDir = Join-Path $localAppData "VoxSub"
    $null = New-Item -ItemType Directory -Force -Path $diagnosticsDir
    $tokenPath = Join-Path $diagnosticsDir "sentry_auth_token.txt"
    $configPath = Join-Path $diagnosticsDir "sentry_api.json"

    $token = ""
    if (Test-Path -LiteralPath $tokenPath) {
        $token = (Get-Content -LiteralPath $tokenPath -Raw).Trim()
    }
    if ([string]::IsNullOrWhiteSpace($token)) {
        $token = Get-TokenFromSecurePrompt
        if ([string]::IsNullOrWhiteSpace($token)) {
            throw "未输入 Sentry API Token。"
        }
        [IO.File]::WriteAllText(
            $tokenPath,
            $token.Trim() + [Environment]::NewLine,
            [Text.UTF8Encoding]::new($false)
        )
        Protect-TokenFile -Path $tokenPath
    }

    $headers = @{ Authorization = "Bearer $token" }
    try {
        $organizations = @(Invoke-SentryGet -Path "/api/0/organizations/" -Headers $headers)
    } catch {
        throw "无法访问 Sentry API。请检查网络、数据区地址，以及令牌的 org:read、project:read、event:read 权限。原始错误：$($_.Exception.Message)"
    }
    if ($organizations.Count -eq 0) {
        throw "该令牌没有可访问的 Sentry 组织。请确认令牌权限和账号项目权限。"
    }

    $matches = @()
    foreach ($organization in $organizations) {
        $orgSlug = [string]$organization.slug
        $projects = @(Invoke-SentryGet -Path "/api/0/organizations/$orgSlug/projects/" -Headers $headers)
        foreach ($project in $projects) {
            if ([string]$project.slug -eq $ProjectSlug) {
                $matches += [pscustomobject]@{
                    OrganizationSlug = $orgSlug
                    ProjectSlug = [string]$project.slug
                    ProjectName = [string]$project.name
                }
            }
        }
    }

    if ($matches.Count -eq 0) {
        $available = ($organizations | ForEach-Object { [string]$_.slug }) -join ", "
        throw "在可访问组织中未找到项目 slug '$ProjectSlug'。可访问组织：$available。"
    }
    if ($matches.Count -gt 1) {
        $available = ($matches | ForEach-Object { "$($_.OrganizationSlug)/$($_.ProjectSlug)" }) -join ", "
        throw "发现多个同名项目：$available。请用 -ProjectSlug 指定唯一项目后重试。"
    }

    $match = $matches[0]
    $config = [ordered]@{
        sentry_base = $normalizedBase
        organization_slug = $match.OrganizationSlug
        project_slug = $match.ProjectSlug
        configured_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    $config | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding utf8

    Write-Host "Sentry API 只读诊断已配置。" -ForegroundColor Green
    Write-Host "项目: $($match.OrganizationSlug)/$($match.ProjectSlug) ($($match.ProjectName))"
    Write-Host "令牌: $tokenPath"
    Write-Host "配置: $configPath"
    Write-Host "可运行 .\scripts\get_sentry_issues.ps1 查看未解决 Issue。"
} catch {
    Write-Host "Sentry API 配置失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
