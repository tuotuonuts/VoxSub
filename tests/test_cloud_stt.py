from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np

from voxsub.cloud_stt import CloudSTT, samples_to_wav
from voxsub.translate._http_client import normalize_api_base


def _serve_stt_once():
    captured: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            captured["path"] = self.path
            captured["body"] = body
            captured["content_type"] = self.headers.get("Content-Type", "")
            captured["auth"] = self.headers.get("Authorization", "")
            payload = json.dumps({"text": "云端识别结果"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    return server, captured


def test_normalize_api_base_accepts_both_v1_forms() -> None:
    assert normalize_api_base("https://example.com") == "https://example.com/v1"
    assert normalize_api_base("https://example.com/v1/") == "https://example.com/v1"


def test_samples_to_wav_is_mono_16bit() -> None:
    wav = samples_to_wav(np.array([0.0, 0.5, -0.5], dtype=np.float32))
    assert wav[:4] == b"RIFF"
    assert b"WAVE" in wav[:16]


def test_cloud_stt_posts_audio_with_its_own_model_and_key() -> None:
    server, captured = _serve_stt_once()
    port = server.server_address[1]
    stt = CloudSTT(
        {
            "stt_api_key": "stt-secret",
            "stt_base_url": f"http://127.0.0.1:{port}/v1",
            "stt_model": "cloud-stt-model",
        },
        allowlist={"127.0.0.1"},
    )
    try:
        assert stt.ready()
        assert stt.transcribe_samples(
            np.zeros(1600, dtype=np.float32), source_lang="zh"
        ) == "云端识别结果"
    finally:
        server.server_close()

    body = captured["body"]
    content_type = str(captured["content_type"])
    assert captured["path"] == "/v1/audio/transcriptions"
    assert captured["auth"] == "Bearer stt-secret"
    assert "multipart/form-data" in content_type
    assert b'name="model"' in body
    assert b"cloud-stt-model" in body
    assert b'name="language"' in body
    assert b"zh" in body
    assert b'filename="voxsub.wav"' in body


def test_cloud_stt_without_credentials_is_not_ready() -> None:
    stt = CloudSTT({"stt_base_url": "https://api.openai.com/v1"})
    assert stt.ready() is False
