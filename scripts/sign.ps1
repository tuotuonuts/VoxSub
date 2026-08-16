# VoxSub self-signed code-signing helper (ASCII only comments!)
# Usage (run from project root):
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\sign.ps1 create
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\sign.ps1 sign <file>
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\sign.ps1 verify <file>
# Notes:
#   - Self-signed certs do NOT clear SmartScreen; they only keep file hashes
#     stable and reduce some AV false positives. Formal OV cert is planned (M9).
#   - Cert lives in CurrentUser store, no admin needed.
param([string]$Action, [string]$Path, [string]$Name = "VoxSub Dev (self-signed)")

function Get-Cert {
    Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
        Where-Object { $_.Subject -like "*VoxSub*" } |
        Select-Object -First 1
}

function Create-Cert {
    if (Get-Cert) { Write-Output "cert already exists, reuse it"; return }
    $c = New-SelfSignedCertificate -Type CodeSigningCert `
        -Subject "CN=$Name" -CertStoreLocation Cert:\CurrentUser\My `
        -KeyUsage DigitalSignature,NonRepudiation `
        -NotAfter (Get-Date).AddYears(3) `
        -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3")
    Write-Output ("created thumbprint: " + $c.Thumbprint)
}

function Sign-File([string]$f) {
    $c = Get-Cert
    if (-not $c) { Write-Error "no cert found, run: sign.ps1 create"; exit 1 }
    $r = Set-AuthenticodeSignature -FilePath $f -Certificate $c `
        -HashAlgorithm SHA256 -TimestampServer http://timestamp.digicert.com
    Write-Output ("sign result: " + $r.Status)
}

function Verify-File([string]$f) {
    if (-not (Test-Path $f)) { Write-Error "file not found: $f"; exit 1 }
    $r = Get-AuthenticodeSignature -FilePath $f
    $h = Get-FileHash -Path $f -Algorithm SHA256
    Write-Output ("status    : " + $r.Status)
    Write-Output ("sha256    : " + $h.Hash)
}

switch ($Action) {
    "create" { Create-Cert }
    "sign"   { Sign-File $Path }
    "verify" { Verify-File $Path }
    default  { Write-Output "usage: sign.ps1 create | sign <file> | verify <file>" }
}