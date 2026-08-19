"""Exercise VoxSub's real GGUF startup path on an Intel NPU computer."""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import traceback
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_DIR = REPO_ROOT / ".npu-probe"
REPORT_PATH = PROBE_DIR / "app-path.json"
SERVER_LOG_PATH = PROBE_DIR / "app-path-server.log"


def _find_model() -> Path:
    configured = os.environ.get("VOXSUB_NPU_TEST_MODEL", "").strip()
    if configured:
        path = Path(configured)
        if path.is_file():
            return path.resolve()
        raise FileNotFoundError(f"Configured GGUF model does not exist: {path}")

    path_file = REPO_ROOT / ".npu-assets" / "models" / "model-path.txt"
    if path_file.is_file():
        path = Path(path_file.read_text(encoding="utf-8-sig").strip())
        if path.is_file():
            return path.resolve()
    raise FileNotFoundError("Prepared NPU probe model was not found.")


def _find_runtime_dir() -> Path:
    configured = os.environ.get("VOXSUB_LLAMA_DIR", "").strip()
    roots = [Path(configured)] if configured else []
    roots.append(REPO_ROOT / ".npu-assets" / "openvino")
    for root in roots:
        if not root.is_dir():
            continue
        direct = root / "llama-server.exe"
        server = direct if direct.is_file() else next(root.rglob("llama-server.exe"), None)
        if server is not None:
            return server.resolve().parent
    raise FileNotFoundError("Prepared OpenVINO llama runtime was not found.")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    model = _find_model()
    runtime_dir = _find_runtime_dir()
    os.environ["VOXSUB_LLAMA_DIR"] = str(runtime_dir)
    os.environ["VOXSUB_LOG"] = "INFO"

    sys.path.insert(0, str(REPO_ROOT))
    from voxsub.translate.qwen import QwenQualityTranslator

    report: dict[str, object] = {
        "model": str(model),
        "runtime_dir": str(runtime_dir),
        "result": "FAIL",
    }
    translator = QwenQualityTranslator(
        model_path=model,
        n_ctx=512,
        n_threads=4,
        max_tokens=16,
        port=_free_port(),
        model_name="NPU application-path probe",
    )
    started = time.monotonic()
    try:
        endpoint = translator._ensure()
        elapsed = time.monotonic() - started
        runtime = translator._runtime
        report.update({
            "endpoint": endpoint,
            "startup_seconds": round(elapsed, 3),
            "backend": runtime.backend if runtime else "cpu",
            "target": runtime.target if runtime else "CPU",
            "failed_runtimes": [list(item) for item in sorted(translator._failed_runtimes)],
        })
        if runtime is None or runtime.backend != "openvino" or runtime.target != "NPU":
            raise RuntimeError(
                "VoxSub application path did not select OpenVINO NPU: "
                f"backend={report['backend']} target={report['target']}"
            )
        if ("openvino", "NPU") in translator._failed_runtimes:
            raise RuntimeError("OpenVINO NPU was incorrectly marked as failed.")
        report["result"] = "PASS"
        print(
            "PASS: VoxSub application startup selected OpenVINO NPU "
            f"and became ready in {elapsed:.1f}s."
        )
        return 0
    except Exception as exc:
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
        print(f"FAIL: VoxSub application NPU startup path: {exc}", file=sys.stderr)
        return 1
    finally:
        translator.close()
        time.sleep(0.5)
        server_output = "\n".join(translator._server_output_tail)
        SERVER_LOG_PATH.write_text(server_output, encoding="utf-8")
        report["server_output_lines"] = len(translator._server_output_tail)
        REPORT_PATH.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
