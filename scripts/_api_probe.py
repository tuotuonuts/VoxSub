#!/usr/bin/env python
"""一次性 API 探测(2): OnlineRecognizer / VadModel 构造签名与方法。"""
import inspect

import sherpa_onnx as so

for cls in ("OnlineRecognizer", "VadModel", "OnlineStream", "FeatureExtractorConfig", "VadModelConfig"):
    obj = getattr(so, cls, None)
    if obj is None:
        print(f"{cls}: MISSING")
        continue
    try:
        sig = inspect.signature(obj.__init__)
    except (TypeError, ValueError):
        sig = "?"
    print(f"\n{cls}{sig}")
    methods = [m for m in dir(obj) if not m.startswith("_")]
    print("  methods:", ", ".join(methods))