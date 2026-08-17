"""voxsub.diagnostics + voxsub.models 模块测试 (M8)。

覆盖:
- run_self_check 返回结构合法 (status 只含 ok/warn/fail)
- 模型完整性检查能在隔离目录中发现人为制造的缺失
- ModelManager.verify_all: 真实 38+ 条登记全部 ready
- fetch 断点续传: 本地 HTTP 服务伪造 206 (续传) / 200 (忽略 Range 全量重写) 两种响应
- fetch 主源失败自动切镜像

模型缺失时 (跨机器) verify_all / 完整性相关用例整体 skip。
"""
from __future__ import annotations

import hashlib
import http.server
import os
import threading
from pathlib import Path

import pytest

from voxsub.diagnostics import export_report, run_self_check
from voxsub.models import ModelManager, sha256_of

MODELS_DIR = Path(os.environ.get("LOCALAPPDATA", ".")) / "VoxSub" / "models"


def _has_real_models() -> bool:
    return (MODELS_DIR / "manifest.json").exists()


# ---------------------------------------------------------------------------
# run_self_check 结构
# ---------------------------------------------------------------------------

def test_run_self_check_structure() -> None:
    """每项 {check, status, detail}; status 只允许 ok/warn/fail。"""
    items = run_self_check()
    assert isinstance(items, list) and len(items) >= 5
    for item in items:
        assert set(item) >= {"check", "status", "detail"}
        assert item["status"] in {"ok", "warn", "fail"}, item
        assert item["check"]
        assert item["detail"]


def test_export_report_plain_text() -> None:
    """报告为纯文本, 含时间戳 / 结论 / 建议, 每行一项。"""
    report = export_report()
    assert isinstance(report, str)
    lines = report.strip().splitlines()
    assert any("自检报告" in ln for ln in lines)
    assert any("时间:" in ln for ln in lines)
    assert any("结论:" in ln for ln in lines)
    # 每个检查项一行 (icon + check 名)
    checks = [i["check"] for i in run_self_check()]
    for c in checks:
        assert any(c in ln for ln in lines), f"报告缺少检查项 {c}"


# ---------------------------------------------------------------------------
# 模型完整性
# ---------------------------------------------------------------------------

def test_model_integrity_detects_missing_file(tmp_path: Path, monkeypatch) -> None:
    """在隔离模型目录删除登记文件后报 fail，恢复后重新为 ok。"""
    import voxsub.diagnostics as diagnostics

    model_file = tmp_path / "asr" / "tokens.txt"
    model_file.parent.mkdir(parents=True)
    original = b"test-model-asset"
    model_file.write_bytes(original)
    ModelManager(tmp_path).scan()
    monkeypatch.setattr(diagnostics, "models_dir", lambda: tmp_path)

    model_file.unlink()
    entry = diagnostics._check_model_integrity()  # noqa: SLF001
    assert entry["status"] == "fail"
    assert "asr/tokens.txt" in entry["detail"]

    model_file.write_bytes(original)
    entry = diagnostics._check_model_integrity()  # noqa: SLF001
    assert entry["status"] == "ok"


@pytest.mark.skipif(not _has_real_models(), reason="本机无真实模型目录")
def test_verify_all_real_models_all_ready() -> None:
    """真机清单全部条目 ready (存在 + 大小一致, 无 missing/corrupt)。"""
    mgr = ModelManager(MODELS_DIR)
    problems = mgr.verify_all()
    assert problems == [], f"存在问题条目: {problems}"
    files = mgr.load_manifest().get("files", {})
    assert len(files) >= 38, "manifest 登记数应 >= 38"


# ---------------------------------------------------------------------------
# fetch 断点续传 (本地 HTTP 服务伪造 206 / 200)
# ---------------------------------------------------------------------------

class _RangeHandler(http.server.BaseHTTPRequestHandler):
    """伪造 Range 语义的本地服务器。

    类变量: payload (响应体), ignore_range (True=忽略 Range 恒返 200 全量),
    seen_ranges (记录收到的 Range 请求头, 供断言续传确实发生)。
    """
    payload = b""
    ignore_range = False
    seen_ranges: list[str] = []

    def do_GET(self):  # noqa: N802 (http.server 命名)
        rng = self.headers.get("Range")
        self.seen_ranges.append(rng or "")
        if rng and not self.ignore_range:
            start = int(rng.split("=")[1].split("-")[0])
            body = self.payload[start:]
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {start}-{len(self.payload) - 1}/{len(self.payload)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.payload)))
            self.end_headers()
            self.wfile.write(self.payload)

    def log_message(self, *args) -> None:  # 静默访问日志
        pass


@pytest.fixture
def http_server():
    """起一个本地 HTTP 服务, 返回 (url, handler_class)。"""
    _RangeHandler.payload = b""
    _RangeHandler.ignore_range = False
    _RangeHandler.seen_ranges = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/model.bin"
    yield url, _RangeHandler
    server.shutdown()
    thread.join(timeout=5)


def _payload(size: int = 102400) -> bytes:
    """确定性内容: bytes(range(256)) 循环, 便于 sha256 比对。"""
    block = bytes(range(256))
    return (block * (size // len(block) + 1))[:size]


def test_fetch_resume_206(http_server, tmp_path) -> None:
    """已有 .part 前缀 -> 带 Range 请求 -> 206 续传完成, sha256 一致。"""
    url, handler = http_server
    data = _payload()
    handler.payload = data
    expected = hashlib.sha256(data).hexdigest()

    dest = tmp_path / "models" / "sub" / "model.bin"
    dest.parent.mkdir(parents=True)
    dest.with_name(dest.name + ".part").write_bytes(data[:40000])  # 预置 40KB 断点

    mgr = ModelManager(tmp_path / "models")
    assert mgr.fetch("sub/model.bin", url, sha256=expected) is True

    assert dest.read_bytes() == data
    assert sha256_of(dest) == expected
    assert handler.seen_ranges and handler.seen_ranges[0].startswith("bytes=40000-"), \
        f"应带续传 Range 头, 实际: {handler.seen_ranges}"
    entry = mgr.load_manifest()["files"]["sub/model.bin"]
    assert entry["status"] == "ready" and entry["size"] == len(data)


def test_fetch_server_ignores_range_200(http_server, tmp_path) -> None:
    """服务器忽略 Range 返回 200 全量 -> 必须清空重写, 不得与旧 .part 拼接。"""
    url, handler = http_server
    data = _payload()
    handler.payload = data
    handler.ignore_range = True  # 模拟忽略 Range 的 CDN
    expected = hashlib.sha256(data).hexdigest()

    dest = tmp_path / "models" / "model.bin"
    dest.parent.mkdir(parents=True)
    # 旧 .part 是垃圾前缀 (若错误拼接, sha256 必不匹配)
    dest.with_name(dest.name + ".part").write_bytes(b"GARBAGE" * 100)

    mgr = ModelManager(tmp_path / "models")
    assert mgr.fetch("model.bin", url, sha256=expected) is True

    assert dest.read_bytes() == data, "200 响应应全量覆盖, 而非拼接旧 .part"
    assert sha256_of(dest) == expected


def test_fetch_falls_back_to_mirror(http_server, tmp_path) -> None:
    """主源连接失败 -> 自动切 mirror 下载成功。"""
    url, handler = http_server
    data = _payload(20480)
    handler.payload = data
    expected = hashlib.sha256(data).hexdigest()

    mgr = ModelManager(tmp_path / "models")
    dead_url = "http://127.0.0.1:1/nope.bin"  # port 1 立即 refused
    assert mgr.fetch("m.bin", dead_url, sha256=expected, mirror=url) is True
    assert sha256_of(tmp_path / "models" / "m.bin") == expected


def test_fetch_sha_mismatch_returns_false(http_server, tmp_path) -> None:
    """下载成功但 sha256 不符 -> fetch 返回 False, 条目登记为 partial。"""
    url, handler = http_server
    handler.payload = _payload(8192)
    mgr = ModelManager(tmp_path / "models")
    bogus = "0" * 64
    assert mgr.fetch("bad.bin", url, sha256=bogus) is False
    entry = mgr.load_manifest()["files"]["bad.bin"]
    assert entry["status"] == "partial"
