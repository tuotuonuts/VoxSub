@echo off
REM ===========================================================================
REM  VoxSub Electron - one-click dev launcher
REM
REM  ASCII-ONLY on purpose: this file is double-clicked from Explorer, where the
REM  console codepage is CP936 and UTF-8 text renders as mojibake or breaks
REM  parsing. All user-facing text stays English here; the app itself is Chinese.
REM
REM  Usage:
REM    double-click          -> incremental build + launch
REM    launch.bat --clean    -> force full rebuild
REM    launch.bat --no-build -> skip build, launch as-is
REM    launch.bat --dev      -> open DevTools after start
REM    launch.bat --debug    -> enable remote debugging on port 9222
REM    launch.bat --keep     -> do not stop running instances first
REM ===========================================================================
setlocal
cd /d "%~dp0"

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

"%NODE_EXE%" "%~dp0scripts\launch.mjs" %*
set "LAUNCH_RC=%ERRORLEVEL%"

REM Keep the window open on failure so the message stays readable.
REM A double-clicked window that closes instantly tells the user nothing.
if not "%LAUNCH_RC%"=="0" (
  echo.
  echo [FAILED] launcher exited with code %LAUNCH_RC%
  echo.
  pause
)

endlocal
