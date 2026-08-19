"""Validate every Model Hub entry against the Intel NPU runtime contract."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from voxsub.model_catalog import CATALOG, ModelSpec  # noqa: E402
from voxsub.models import fetch_file, sha256_of  # noqa: E402
from voxsub.npu_validation import npu_compatibility  # noqa: E402


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _run(command: list[str], log_path: Path, env: dict[str, str]) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"RUN: {' '.join(command)}", flush=True)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            log.write(line)
        return process.wait()


def _download(model: ModelSpec, models_dir: Path) -> Path:
    destination = models_dir / model.id / model.asset_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        size_ok = destination.stat().st_size == model.download_bytes
        sha_ok = bool(model.sha256) and sha256_of(destination) == model.sha256
        if size_ok and sha_ok:
            print(f"REUSE: {model.id} ({destination.stat().st_size} bytes)", flush=True)
            return destination
        destination.unlink()

    urls = [source.url for source in model.sources if source.url]
    if not urls:
        raise RuntimeError(f"No download source is configured for {model.id}.")
    progress_state = {"bucket": -1, "last": 0.0}

    def progress(done: int, total: int, source: str) -> None:
        now = time.monotonic()
        bucket = int(done * 20 / total) if total else -1
        if bucket != progress_state["bucket"] or now - progress_state["last"] >= 30:
            percent = f"{done / total * 100:.1f}%" if total else f"{done} bytes"
            print(f"DOWNLOAD {model.id}: {percent} from {source}", flush=True)
            progress_state.update(bucket=bucket, last=now)

    ok = fetch_file(
        urls[0],
        destination,
        expected_sha=model.sha256,
        mirrors=urls[1:],
        progress=progress,
    )
    if not ok:
        raise RuntimeError(f"Download or SHA256 verification failed for {model.id}.")
    if destination.stat().st_size != model.download_bytes:
        raise RuntimeError(
            f"Size mismatch for {model.id}: expected {model.download_bytes}, "
            f"got {destination.stat().st_size}."
        )
    return destination


def _direct_failure_code(log_text: str) -> str:
    lower = log_text.casefold()
    if "driver" in lower and ("too old" in lower or "could not be read" in lower):
        return "driver_incompatible"
    if "runtime file missing" in lower or "llama-server.exe not found" in lower:
        return "runtime_missing"
    if "fallback" in lower or "npu is unavailable" in lower:
        return "fallback_detected"
    if "did not become ready" in lower or "exited early" in lower:
        return "npu_runtime_failed"
    if "no proof" in lower or "no explicit openvino npu" in lower:
        return "npu_evidence_missing"
    return "npu_inference_failed"


def _probe_model(model: ModelSpec, model_path: Path, runtime_dir: Path,
                 output_dir: Path, python_exe: Path) -> dict:
    model_dir = output_dir / "models" / model.id
    app_dir = model_dir / "application"
    direct_dir = model_dir / "explicit-npu"
    env = os.environ.copy()
    env.update({
        "PYTHONUTF8": "1",
        "VOXSUB_LLAMA_DIR": str(runtime_dir),
        "VOXSUB_NPU_TEST_MODEL": str(model_path),
        "VOXSUB_LOG": "INFO",
        "OV_NPU_LOG_LEVEL": "LOG_INFO",
        "GGML_OPENVINO_PROFILING": "1",
    })
    app_command = [
        str(python_exe), str(REPO_ROOT / "scripts" / "npu_app_probe.py"),
        "--model-id", model.id,
        "--model-path", str(model_path),
        "--runtime-dir", str(runtime_dir),
        "--output-dir", str(app_dir),
        "--force", "auto",
    ]
    app_code = _run(app_command, model_dir / "application-command.log", env)
    app_report = _read_json(app_dir / "app-path.json")

    direct_command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(REPO_ROOT / "scripts" / "npu_probe.ps1"),
        "-ModelPath", str(model_path),
        "-LlamaDir", str(runtime_dir),
        "-OutputDir", str(direct_dir),
    ]
    direct_code = _run(direct_command, model_dir / "explicit-command.log", env)
    direct_log_path = direct_dir / "probe.log"
    direct_log = direct_log_path.read_text(
        encoding="utf-8-sig", errors="replace") if direct_log_path.is_file() else ""

    result = {
        "model_id": model.id,
        "model_name": model.name,
        "model_path": str(model_path),
        "model_size": model_path.stat().st_size,
        "model_sha256": model.sha256,
        "runtime": model.runtime,
        "automatic_route": app_report,
        "explicit_npu_exit_code": direct_code,
    }
    if direct_code == 0 and app_code == 0:
        result.update(
            status="verified",
            reason_code="explicit_and_automatic_passed",
            npu_available=True,
        )
        return result
    if direct_code == 0:
        result.update(
            status="failed",
            reason_code="automatic_route_failed",
            npu_available=False,
        )
        return result

    cpu_dir = model_dir / "cpu-baseline"
    cpu_command = app_command[:-2] + ["--force", "cpu"]
    cpu_command[cpu_command.index(str(app_dir))] = str(cpu_dir)
    cpu_code = _run(cpu_command, model_dir / "cpu-command.log", env)
    result.update(
        status="failed",
        reason_code=_direct_failure_code(direct_log),
        npu_available=False,
        cpu_baseline_passed=cpu_code == 0,
        cpu_baseline=_read_json(cpu_dir / "app-path.json"),
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--models-dir", required=True)
    parser.add_argument("--output-dir", default=str(REPO_ROOT / ".npu-probe"))
    parser.add_argument("--model-id", action="append", default=[])
    args = parser.parse_args()

    runtime_dir = Path(args.runtime_dir).resolve()
    models_dir = Path(args.models_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)
    selected_ids = set(args.model_id)
    models = [model for model in CATALOG if not selected_ids or model.id in selected_ids]
    missing_ids = selected_ids - {model.id for model in models}
    if missing_ids:
        raise SystemExit(f"Unknown model id(s): {', '.join(sorted(missing_ids))}")

    eligible = sorted(
        (model for model in models if model.runtime == "llama-hy-mt2" and model.npu_supported),
        key=lambda model: model.download_bytes,
    )
    required_download = 0
    for model in eligible:
        cached = models_dir / model.id / model.asset_name
        cached_bytes = cached.stat().st_size if cached.is_file() else 0
        # An invalid cached file is removed before download, so only the net
        # additional space is required. This also handles truncated files.
        required_download += max(0, model.download_bytes - cached_bytes)
    disk = shutil.disk_usage(models_dir)
    if disk.free < required_download + 2_000_000_000:
        _write_json(output_dir / "catalog-summary.json", {
            "result": "FAIL",
            "reason_code": "insufficient_disk",
            "required_bytes": required_download + 2_000_000_000,
            "free_bytes": disk.free,
            "models": [],
        })
        print("FAIL: not enough disk space for the NPU model matrix.", flush=True)
        return 1

    results: list[dict] = []
    for model in models:
        if model not in eligible:
            evidence = npu_compatibility(model.id)
            results.append({
                "model_id": model.id,
                "model_name": model.name,
                "runtime": model.runtime,
                "status": "unsupported",
                "reason_code": "runtime_unsupported",
                "npu_available": False,
                "reason": evidence.reason_en,
            })

    for index, model in enumerate(eligible, 1):
        print(f"\n===== NPU MODEL {index}/{len(eligible)}: {model.id} =====", flush=True)
        try:
            model_path = _download(model, models_dir)
            result = _probe_model(
                model, model_path, runtime_dir, output_dir, Path(sys.executable))
        except Exception as exc:
            result = {
                "model_id": model.id,
                "model_name": model.name,
                "runtime": model.runtime,
                "status": "failed",
                "reason_code": "asset_or_probe_exception",
                "npu_available": False,
                "error": str(exc),
            }
        results.append(result)
        _write_json(output_dir / "catalog-summary.partial.json", {
            "result": "RUNNING", "models": results})

    results.sort(key=lambda item: next(
        index for index, model in enumerate(CATALOG) if model.id == item["model_id"]))
    expected_ok = all(
        item["status"] == ("verified" if item["runtime"] == "llama-hy-mt2" else "unsupported")
        for item in results
    )
    summary = {
        "schema_version": 1,
        "result": "PASS" if expected_ok else "FAIL",
        "runtime_dir": str(runtime_dir),
        "models_dir": str(models_dir),
        "models": results,
    }
    _write_json(output_dir / "catalog-summary.json", summary)
    lines = ["# Intel NPU model matrix", ""]
    for item in results:
        lines.append(
            f"- {item['model_id']}: {item['status']} ({item['reason_code']})")
    (output_dir / "catalog-summary.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if expected_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
