#!/usr/bin/env bash
# VoxSub release signing helper.
#
# Uses the Windows CurrentUser certificate store. The private key is never
# exported and no certificate password is stored in the repository.
#
# Usage: bash scripts/sign.sh <path-to-exe>
# Signs in place, prints sha256.
set -euo pipefail

EXE="${1:?usage: sign.sh <exe-path>}"
EXE_WIN="$(cygpath -w "$EXE")"

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command \
  "\$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Where-Object { \$_.Subject -like '*VoxSub*' -and \$_.HasPrivateKey -and \$_.NotAfter -gt (Get-Date) } | Sort-Object NotAfter -Descending | Select-Object -First 1; if (-not \$cert) { throw 'VoxSub code-signing certificate not found' }; \$sig = Set-AuthenticodeSignature -FilePath '$EXE_WIN' -Certificate \$cert -HashAlgorithm SHA256 -TimestampServer 'http://timestamp.digicert.com'; if (-not \$sig.SignerCertificate) { throw \$sig.StatusMessage }; Write-Output ('[sign] signer: ' + \$sig.SignerCertificate.Subject); Write-Output ('[sign] status: ' + \$sig.Status); Get-FileHash -LiteralPath '$EXE_WIN' -Algorithm SHA256 | ForEach-Object { Write-Output ('[sign] sha256: ' + \$_.Hash) }"
