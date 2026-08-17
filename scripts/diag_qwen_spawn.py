#!/usr/bin/env python
"""qwen _spawn 卡点诊断: 逐步打印定位挂起位置。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voxsub.translate.qwen import QwenQualityTranslator

t = QwenQualityTranslator()
print("构造 OK, server_exe =", t._server_exe)
print("model exists =", t._model_path.exists(), t._model_path)
print("start port =", t._start_port)

port = t._pick_free_port()
print("pick_free_port =", port)

print("spawning llama-server ...")
t0 = time.perf_counter()
try:
    t._spawn()
    print(f"_spawn OK ({time.perf_counter()-t0:.1f}s), port={t._port}")
except Exception as e:
    print(f"_spawn 失败 ({time.perf_counter()-t0:.1f}s): {type(e).__name__}: {e}")
finally:
    t.close()
    print("closed")
