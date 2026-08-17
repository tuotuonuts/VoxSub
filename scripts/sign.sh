#!/usr/bin/env bash
# VoxSub release signing helper.
#
# Tool: signtool.exe (Windows SDK, official) - NOT osslsigncode 2.14-mingw,
# which fails intermittently with "Unable to read input file" on PyInstaller
# 6.22 generated PE files (verified 2026-08-17).
# PowerShell 5.1 also ANSI-encodes args for native exes, so we drive signtool
# from bash where arg passing is UTF-8 safe.
#
# Usage: bash scripts/sign.sh <path-to-exe>
# Signs in place, prints sha256.
set -euo pipefail

EXE="${1:?usage: sign.sh <exe-path>}"
EXE_WIN="$(cygpath -w "$EXE")"          # signtool needs Windows path
LAPPDATA_WIN="$(cygpath -w "$LOCALAPPDATA")"
TOOL="$LAPPDATA_WIN/VoxSub/tools/signtool.exe"
PFX="$LAPPDATA_WIN/Temp/voxsub_dev.pfx"
PASS="VoxSubDev2026!"

"$TOOL" sign /f "$PFX" /p "$PASS" /fd SHA256 \
  /tr http://timestamp.digicert.com /td SHA256 "$EXE_WIN" || {
  echo "[sign] timestamp failed, retry without timestamp" >&2
  "$TOOL" sign /f "$PFX" /p "$PASS" /fd SHA256 "$EXE_WIN"
}
echo "[sign] OK: $EXE_WIN"
powershell.exe -NoProfile -Command "Get-FileHash -Path '$EXE_WIN' -Algorithm SHA256 | ForEach-Object { Write-Output ('[sign] sha256: ' + \$_.Hash) }"
