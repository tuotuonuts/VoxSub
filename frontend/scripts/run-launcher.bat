@echo off
REM ===========================================================================
REM  VoxSub Electron - launcher implementation (single source of truth)
REM
REM  Why this file exists:
REM    The project has launchers in three places (repo root, frontend\, and the
REM    pre-migration checkout) so that whichever one gets double-clicked, the app
REM    starts. They must all behave identically, so the actual logic lives here
REM    once and the others only forward to this file.
REM
REM  ASCII-ONLY, INCLUDING FILE PATHS REFERENCED BELOW:
REM    cmd.exe reads .bat files using the OEM codepage (CP936 on this machine),
REM    not UTF-8. A Chinese path written inside a .bat gets mis-decoded into
REM    mojibake and every `if exist` fails - the launcher then does nothing and
REM    closes. This bit us for real: two forwarding launchers were written with
REM    UTF-8 Chinese paths and silently did nothing when double-clicked.
REM    So: paths in file CONTENT stay ASCII. The file NAME may be Chinese
REM    (the filesystem is Unicode) - only the content is restricted.
REM
REM  Usage (args are passed straight through to scripts\launch.mjs):
REM    --clean      force full rebuild
REM    --no-build   skip build, launch as-is
REM    --dev        open DevTools after start
REM    --debug      enable remote debugging on port 9222
REM    --silent     no window appears (automation)
REM    --keep       do not stop running instances first
REM ===========================================================================
setlocal

cd /d "%~dp0.."

set "NODE_EXE="

REM 1) Prefer node from PATH (works when launched from a normal shell)
for %%I in (node.exe) do if not "%%~$PATH:I"=="" set "NODE_EXE=%%~$PATH:I"

REM 2) Fall back to known install locations
if not defined NODE_EXE (
  if exist "D:\nodejs\node.exe" set "NODE_EXE=D:\nodejs\node.exe"
)
if not defined NODE_EXE (
  if exist "%ProgramFiles%\nodejs\node.exe" set "NODE_EXE=%ProgramFiles%\nodejs\node.exe"
)
if not defined NODE_EXE (
  if exist "%LOCALAPPDATA%\hermes\node\node.exe" set "NODE_EXE=%LOCALAPPDATA%\hermes\node\node.exe"
)

if not defined NODE_EXE (
  echo.
  echo [ERROR] Node.js not found.
  echo         Install Node.js 20+ or add it to PATH.
  echo.
  pause
  exit /b 1
)

echo Using Node: %NODE_EXE%
echo.

"%NODE_EXE%" "%~dp0launch.mjs" %*
set "LAUNCH_RC=%ERRORLEVEL%"

REM Keep the window open on failure so the message stays readable.
REM A double-clicked window that closes instantly tells the user nothing.
if not "%LAUNCH_RC%"=="0" (
  echo.
  echo [FAILED] launcher exited with code %LAUNCH_RC%
  echo.
  pause
)

endlocal & exit /b %LAUNCH_RC%
