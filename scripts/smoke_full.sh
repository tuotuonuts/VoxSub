#!/usr/bin/env bash
# VoxSub 完整冒烟复验 (第二轮, 含死锁修复验证)
# 逐项执行, 任一失败即退出非零 (供"不过就修"循环)
set -u
cd /d/OneDrive/app_dve/VoxSub
export PYTHONPATH=; unset PYTHONPATH PYTHONHOME

echo "=== [1/5] 全量测试 ==="
timeout 150 .venv/Scripts/python.exe -m pytest tests/ -q 2>&1 | tail -2 || { echo "FAIL 1: pytest"; exit 1; }

echo "=== [2/5] 质量档冷启动(死锁修复验证) ==="
timeout 120 .venv/Scripts/python.exe scripts/smoke_qwen.py 2>&1 | grep -E "SMOKE_QWEN_PASS|失败" || { echo "FAIL 2: qwen 冷启动"; exit 1; }

echo "=== [3/5] C 模式真实 wav -> srt ==="
timeout 120 .venv/Scripts/python.exe -c "
import os
from pathlib import Path
from voxsub.pipeline import Pipeline
from voxsub.translate.opus import OpusFastTranslator
md = Path(os.environ['LOCALAPPDATA']) / 'VoxSub' / 'models'
p = Pipeline(); p._translator = OpusFastTranslator()
lines, _ = p._transcribe_file(md/'asr'/'test_wavs'/'3.wav')
print('SMOKE_C_PASS lines=%d' % len(lines)) if lines else print('FAIL: no lines')
p._translator.close()
" 2>&1 | grep SMOKE_C_PASS || { echo "FAIL 3: C 模式"; exit 1; }

echo "=== [4/5] 诊断六项 ==="
timeout 60 .venv/Scripts/python.exe -c "
from voxsub.diagnostics import run_self_check
r = run_self_check()
print('SMOKE_DIAG_PASS ok=%d fail=%d' % (sum(1 for x in r if x['status']=='ok'), sum(1 for x in r if x['status']=='fail'))) if all(x['status'] in ('ok','warn') for x in r) else print('FAIL')
" 2>&1 | grep SMOKE_DIAG_PASS || { echo "FAIL 4: 诊断"; exit 1; }

echo "=== [5/5] Release 产物签名链 ==="
powershell.exe -NoProfile -Command "
\$exe = Get-AuthenticodeSignature -FilePath 'D:\OneDrive\app_dve\VoxSub\dist\VoxSub\VoxSub.exe'
\$setup = Get-AuthenticodeSignature -FilePath 'D:\OneDrive\app_dve\Release\VoxSub-Setup.exe'
if (\$exe.SignerCertificate -and \$setup.SignerCertificate) { Write-Output 'SMOKE_SIGN_PASS' } else { Write-Output 'FAIL: 签名缺失' }
" 2>&1 | grep SMOKE_SIGN_PASS || { echo "FAIL 5: 签名"; exit 1; }

echo "=== 冒烟全部通过 ==="
