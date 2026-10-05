"""Real installed OCR + OPUS, synthetic pixels, in-process IPC handlers.

No desktop capture, windows, sound, cloud, user config or model downloads.
Results store metadata only. Requires installed builtin RapidOCR and OPUS en-zh.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "frontend/backend"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ["LOCALAPPDATA"] = str(args.output / "local")
    os.environ["APPDATA"] = str(args.output / "roaming")
    from PIL import Image, ImageDraw, ImageFont
    import numpy as np
    import ipc_server
    from voxsub import ocr
    from voxsub.config_store import ConfigStore
    from voxsub.translate.opus import OpusFastTranslator
    from voxsub.file_io import write_text_atomically
    import rapidocr
    # Explicit files prevent any implicit model download.
    weights = Path(rapidocr.__file__).parent / "models"
    paths = {"Det.model_path": weights / "PP-OCRv6_det_small.onnx",
             "Rec.model_path": weights / "PP-OCRv6_rec_small.onnx",
             "Cls.model_path": weights / "ch_ppocr_mobile_v2.0_cls_mobile.onnx"}
    if not all(path.is_file() for path in paths.values()):
        raise RuntimeError("Installed RapidOCR weights missing; do not download during acceptance")
    original_create = ocr.RapidOcrEngine._create_engine
    def create(self, params):
        return original_create(self, {**params, **{key: str(path) for key,path in paths.items()},
            "EngineConfig.onnxruntime.use_cuda": False, "EngineConfig.onnxruntime.use_dml": False,
            "EngineConfig.onnxruntime.intra_op_num_threads": 2,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1})
    ocr.RapidOcrEngine._create_engine = create
    ocr.preferred_ocr_backend = lambda: ("CPU", {})
    ocr.TranslatorFactory.create = lambda *_: OpusFastTranslator(args.models / "translate/opus", threads=2, providers=["CPUExecutionProvider"])
    ConfigStore().update({"models_root": str(args.models), "ocr_cache_root": str(args.output / "cache"),
        "ocr_model_id": "ocr-rapidocr-v6-small-builtin", "lang_pair": "en-zh", "translate_tier": "fast", "ocr_cache_limit": 2})
    service = ipc_server.BackendService()
    from contract_validation import ContractRegistry
    registry = ContractRegistry(ROOT / "contracts")
    def invoke(command, values):
        registry.validate_args(command, values)
        result = service.handle(command, values)
        registry.validate_result(command, result)
        return result
    font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 42)
    image = Image.new("RGB", (1000, 240), "white")
    draw = ImageDraw.Draw(image)
    draw.text((35, 25), "Meeting starts at nine.", fill="black", font=font)
    draw.text((35, 120), "Please review the document.", fill="black", font=font)
    source = args.output / "synthetic # sample.png"
    image.save(source)
    cases = []
    def recognize(label, live=False, path=source, **kw):
        started = time.perf_counter()
        result = invoke("ocr_recognize", {"path": str(path), "source": "en", "target": "zh", "translate": True, "live": live, **kw})
        cases.append({"case": label, "lines": len(result["lines"]), "translated_lines": sum(bool(l["translation"]) for l in result["lines"]),
            "failed_lines": result["failedLines"], "unchanged": result["unchanged"], "model": result["ocrModelId"],
            "backend": result["ocrBackend"], "ocr_ms": result["ocrElapsedMs"], "translation_ms": result["translateElapsedMs"],
            "wall_ms": round((time.perf_counter()-started)*1000,2), "translation_requests": result["translationRequests"]})
        return result
    try:
        shot = recognize("static")
        assert len(shot["lines"]) >= 2 and shot["failedLines"] == 0
        first = recognize("live-first", live=True)
        repeat = recognize("live-identical", live=True)
        assert repeat["unchanged"]
        changed = image.copy()
        changed.putpixel((999,239),(253,254,255))
        changed_path = args.output / "changed.png"
        changed.save(changed_path)
        fresh = recognize("live-one-pixel-change", path=changed_path, live=True)
        assert not fresh["unchanged"] and fresh["translationRequests"] == 0
        blank_path = args.output / "blank.png"
        Image.new("RGB", image.size, "white").save(blank_path)
        empty = recognize("live-empty", path=blank_path, live=True)
        assert not empty["lines"]
        off = recognize("recognition-only", translate=False)
        assert not off["translation"] and off["translationRequests"] == 0
        out = args.output / "translated.png"
        rendered = invoke("render_ocr_image", {"source": str(source), "target": str(out), "lines": [{"translation": line["translation"], "box": line["box"]} for line in shot["lines"]]})
        with Image.open(out) as exported:
            assert exported.size == image.size
            assert not np.array_equal(np.asarray(exported), np.asarray(image))
        for _ in range(3):
            invoke("render_ocr_image", {"source": str(source), "lines": [{"translation": line["translation"], "box": line["box"]} for line in shot["lines"]]})
        assert len(list((args.output / "cache/translated").glob("*.png"))) == 2
        invoke("ocr_release", {})
        resumed = recognize("live-after-release", live=True)
        assert not resumed["unchanged"]
        report = {"status": "PASS", "boundary": "Real installed CPU models + synthetic images + actual in-process handler; not screen capture, Electron paint, production stdio or accuracy benchmark",
                  "cases": cases, "rendered_lines": rendered["lines"], "export_size": list(image.size), "cache_limit_verified": 2}
        write_text_atomically(args.output / "result.json", json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=True))
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
