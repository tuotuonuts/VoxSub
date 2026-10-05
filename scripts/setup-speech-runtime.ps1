param([switch]$China, [ValidateSet("cpu", "cuda")][string]$Device = "cpu")
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Remove-Item Env:PYTHONHOME,Env:PYTHONPATH -ErrorAction SilentlyContinue
$python = Join-Path $root '.venv\Scripts\python.exe'
$folder = if ($Device -eq 'cuda') { '.venv-speech-cuda' } else { '.venv-speech' }
$lock = if ($Device -eq 'cuda') { 'requirements-speech-cuda.lock' } else { 'requirements-speech.lock' }
$runtime = Join-Path $root $folder
if (-not (Test-Path -LiteralPath (Join-Path $runtime 'Scripts\python.exe'))) {
    & $python -m venv $runtime
    if ($LASTEXITCODE -ne 0) { throw 'Could not create speech environment' }
}
$argsList = @('-m','pip','install','-r',(Join-Path $root $lock))
if ($China) { $argsList += @('--index-url','https://pypi.tuna.tsinghua.edu.cn/simple') }
& (Join-Path $runtime 'Scripts\python.exe') @argsList
if ($LASTEXITCODE -ne 0) { throw 'Speech runtime installation failed; no model configuration changed' }
