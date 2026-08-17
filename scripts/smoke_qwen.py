#!/usr/bin/env python
"""质量档 Qwen 冒烟: 真实 llama-server 子进程翻译验证。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voxsub.translate.qwen import QwenQualityTranslator

t = QwenQualityTranslator()
s = time.perf_counter()
try:
    r = t.translate("今天天气很好，我们去公园散步。", "zh", "en")
    print("质量档译文:", repr(r))
    print("耗时: %.2fs" % (time.perf_counter() - s))
    print("SMOKE_QWEN_PASS")
except Exception as e:
    print("质量档失败:", type(e).__name__, e)
finally:
    t.close()
