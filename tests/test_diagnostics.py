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
from voxsub.models import ModelManager, fetch_file, sha256_of

MODELS_DIR = Path(os.environ.get("LOCALAPPDATA", ".")) / "VoxSub" / "models"


def _has_real_models() -> bool:
    # A stale manifest is common after an interrupted download or an upgrade.
    # Only run the expensive all-assets assertion when the local cache is
    # already complete; missing/corrupt-cache behavior is covered by the
    # isolated integrity tests below.
    if not (MODELS_DIR / "manifest.json").exists():
        return False
    try:
        return ModelManager(MODELS_DIR).verify_all() == []
    except OSError:
        return False


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


def test_export_report_uses_supplied_snapshot(monkeypatch) -> None:
    """导出页面当前快照时不能偷偷再次执行昂贵自检。"""
    import voxsub.diagnostics as diagnostics

    monkeypatch.setattr(
        diagnostics, "run_self_check",
        lambda: pytest.fail("export_report should not rerun self-check"),
    )
    report = diagnostics.export_report([
        {"check": "ASR 冒烟", "status": "fail", "detail": "缺少 encoder onnx",
         "suggestion": "点击修复"},
    ])
    assert "ASR 冒烟" in report
    assert "缺少 encoder onnx" in report


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


def test_model_integrity_detects_corrupt_catalog_asset_without_manifest(
        tmp_path: Path, monkeypatch) -> None:
    """A marketplace GGUF must fail self-check even when no manifest exists."""
    import hashlib
    from dataclasses import replace

    import voxsub.diagnostics as diagnostics
    from voxsub.model_catalog import get_model

    base = get_model("mt-hy-mt2-1.8b-q8")
    assert base is not None
    payload = b"expected-model"
    model = replace(
        base,
        download_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        install_rel="translate/test-corrupt",
        legacy_install_rels=(),
    )
    path = tmp_path / model.install_rel / model.required_paths[0]
    path.parent.mkdir(parents=True)
    path.write_bytes(b"corrupt")
    monkeypatch.setattr(diagnostics, "models_dir", lambda: tmp_path)
    monkeypatch.setattr(diagnostics, "CATALOG", (model,))

    entry = diagnostics._check_model_integrity()  # noqa: SLF001

    assert entry["status"] == "fail"
    assert "大小或 SHA256" in entry["detail"]
    assert entry["repair"]["model_ids"] == [model.id]


def test_vad_check_reports_missing_without_implicit_repair(
        tmp_path: Path, monkeypatch) -> None:
    """自检只报告 VAD 状态，修复动作必须由按钮显式触发。"""
    import voxsub.bootstrap_models as bootstrap_models
    import voxsub.diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "models_dir", lambda: tmp_path)
    called = []
    monkeypatch.setattr(
        bootstrap_models,
        "ensure_bundled_vad",
        lambda *_args, **_kwargs: called.append(True),
    )

    result = diagnostics._check_vad_smoke()  # noqa: SLF001

    assert result["status"] == "fail"
    assert result["repair"] == {"kind": "vad"}
    assert called == []


def test_repair_self_check_uses_only_declared_targets(tmp_path: Path, monkeypatch) -> None:
    """修复只消费最新结果里的显式目标，不猜测其它配置或运行时问题。"""
    from types import SimpleNamespace

    import voxsub.bootstrap_models as bootstrap_models
    import voxsub.diagnostics as diagnostics

    calls: list[tuple[str, str, bool]] = []
    vad_calls: list[Path] = []

    class _Marketplace:
        def __init__(self, root):
            assert Path(root) == tmp_path

        def install(self, model, preference="auto", force=False):
            calls.append((model.id, preference, force))

    store = SimpleNamespace(load=lambda: {"download_source": "china"})
    monkeypatch.setattr(diagnostics, "resolve_models_root", lambda _store=None: tmp_path)
    monkeypatch.setattr(diagnostics, "ModelMarketplace", _Marketplace)
    monkeypatch.setattr(
        diagnostics,
        "get_model",
        lambda model_id: SimpleNamespace(id=model_id),
    )
    monkeypatch.setattr(
        bootstrap_models,
        "ensure_bundled_vad",
        lambda root: vad_calls.append(Path(root)) or Path(root) / "vad" / "silero_vad_v5.onnx",
    )

    results = [
        {"check": "ORT providers", "status": "fail"},
        {"check": "资源", "status": "warn", "repair": {"kind": "runtime"}},
        {"check": "ASR 冒烟", "status": "fail",
         "repair": {"kind": "models", "model_ids": ["asr-broken", "asr-broken"]}},
        {"check": "VAD 冒烟", "status": "fail", "repair": {"kind": "vad"}},
        {"check": "已通过", "status": "ok",
         "repair": {"kind": "models", "model_ids": ["must-not-run"]}},
    ]

    outcome = diagnostics.repair_self_check(results, store=store)

    assert outcome == {"repaired": ["基础 VAD", "asr-broken"], "errors": []}
    assert vad_calls == [tmp_path]
    assert calls == [("asr-broken", "china", True)]


def test_repair_self_check_skips_results_without_repair_descriptor(
        tmp_path: Path, monkeypatch) -> None:
    """没有明确修复目标的 ORT/资源警告不应触发隐式操作。"""
    import voxsub.diagnostics as diagnostics

    calls = []

    class _Marketplace:
        def __init__(self, _root):
            pass

        def install(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(diagnostics, "resolve_models_root", lambda _store=None: tmp_path)
    monkeypatch.setattr(diagnostics, "ModelMarketplace", _Marketplace)

    outcome = diagnostics.repair_self_check([
        {"check": "ORT providers", "status": "fail"},
        {"check": "磁盘/内存余量", "status": "warn"},
    ])

    assert outcome == {"repaired": [], "errors": []}
    assert calls == []


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
    truncate_first_at = 0
    request_count = 0
    seen_ranges: list[str] = []

    def do_GET(self):  # noqa: N802 (http.server 命名)
        type(self).request_count += 1
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
            if self.truncate_first_at and self.request_count == 1:
                self.wfile.write(body[:self.truncate_first_at])
                self.close_connection = True
            else:
                self.wfile.write(body)
        else:
            body = self.payload
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.truncate_first_at and self.request_count == 1:
                self.wfile.write(body[:self.truncate_first_at])
                self.close_connection = True
            else:
                self.wfile.write(body)

    def log_message(self, *args) -> None:  # 静默访问日志
        pass


@pytest.fixture
def http_server():
    """起一个本地 HTTP 服务, 返回 (url, handler_class)。"""
    _RangeHandler.payload = b""
    _RangeHandler.ignore_range = False
    _RangeHandler.truncate_first_at = 0
    _RangeHandler.request_count = 0
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


def test_fetch_does_not_promote_unverified_part_file(http_server, tmp_path) -> None:
    """A .part file needs size/hash evidence before it can become final."""
    url, handler = http_server
    data = _payload(96000)
    handler.payload = data
    dest = tmp_path / "unconstrained.bin"
    dest.with_name(dest.name + ".part").write_bytes(data[:16000])

    assert fetch_file(url, dest) is True

    assert dest.read_bytes() == data
    assert handler.seen_ranges[0] == "bytes=16000-"


def test_fetch_resumes_after_cdn_early_eof(http_server, tmp_path) -> None:
    """CDN 静默提前 EOF 时保留断点，下一请求必须 Range 续传。"""
    url, handler = http_server
    data = _payload(256000)
    handler.payload = data
    handler.truncate_first_at = 64000
    expected = hashlib.sha256(data).hexdigest()
    dest = tmp_path / "large-model.bin"

    assert fetch_file(
        url, dest, expected_sha=expected, expected_size=len(data)) is True

    assert dest.read_bytes() == data
    assert not dest.with_name(dest.name + ".part").exists()
    assert handler.seen_ranges[:2] == ["", "bytes=64000-"]


def test_fetch_recovers_short_file_promoted_by_old_downloader(
        http_server, tmp_path) -> None:
    """0.3.9 将提前 EOF 文件提升为 dest 后，升级版应原地续传。"""
    url, handler = http_server
    data = _payload(192000)
    handler.payload = data
    expected = hashlib.sha256(data).hexdigest()
    dest = tmp_path / "legacy-incomplete.bin"
    dest.write_bytes(data[:48000])

    assert fetch_file(
        url, dest, expected_sha=expected, expected_size=len(data)) is True

    assert dest.read_bytes() == data
    assert handler.seen_ranges[0] == "bytes=48000-"


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


@pytest.mark.parametrize("rel", ["../outside.bin", "..\\outside.bin"])
def test_model_manager_rejects_download_path_outside_root(tmp_path: Path, rel: str) -> None:
    """A damaged catalog path must fail before any network or disk operation."""
    mgr = ModelManager(tmp_path / "models")

    with pytest.raises(ValueError, match="模型路径"):
        mgr.fetch(rel, "https://example.invalid/model.bin")

    assert not (tmp_path / "outside.bin").exists()


def test_model_manager_reports_unsafe_manifest_path(tmp_path: Path) -> None:
    """Integrity checks must not follow hand-edited manifest paths elsewhere."""
    mgr = ModelManager(tmp_path / "models")
    mgr.save_manifest({
        "version": 1,
        "files": {
            "../outside.bin": {
                "size": 1,
                "sha256": "",
                "status": "ready",
            },
        },
    })

    problems = mgr.verify_all()

    assert problems[0]["rel"] == "../outside.bin"
    assert problems[0]["status"] == "invalid"
