"""QwenQualityTranslator 并发安全回归测试。

问题（独立审查发现的 TOCTOU 竞态）: _ensure() 检查在锁外做, _spawn() 无条件执行
→ 并发初次调用可双开 llama-server 孤儿化第一个。

修复: double-checked locking。本测试 monkeypatch _spawn 只做计数,
验证"并发 N 线程首次 _ensure 只 spawn 一次"——不启动真进程避免真实 spawn 语义。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voxsub.translate.qwen import QwenQualityTranslator  # noqa: E402


class _FakeProc:
    """最小"存活进程"替身: 提供 _ensure 依赖的 poll() (返回 None = 存活)。"""

    def __init__(self, pid: int = 1) -> None:
        self.pid = pid

    def poll(self):  # noqa: A003 - 对齐 subprocess.Popen.poll 语义
        return None  # None = 仍在运行

    def wait(self, timeout: float | None = None) -> int:
        return 0  # 假进程立即退出

    def terminate(self) -> None:
        pass


def _make_qwen(tmp_path: Path) -> QwenQualityTranslator:
    """构造未就绪的 translator (model_path 指向存在的伪 gguf, server 可递归寻找)。"""
    tools = tmp_path / "tools" / "llama"
    tools.mkdir(parents=True)
    fake_exe = tools / "llama-server.exe"
    fake_exe.write_bytes(b"MZ fake")
    models = tmp_path / "models" / "llm"
    models.mkdir(parents=True)
    gguf = models / "qwen2.5-1.5b-instruct-q4_k_m.gguf"
    gguf.write_bytes(b"x" * 600_000)
    # 强制 monkeypatch 默认路径, 让构造不因缺模型抛错
    q = QwenQualityTranslator(model_path=gguf, n_ctx=64, n_threads=1)
    return q


def test_concurrent_first_ensure_spawns_once(tmp_path: Path, monkeypatch) -> None:
    """8 线程并发首次 _ensure: 只有一次 _spawn (双检锁回归)。"""
    q = _make_qwen(tmp_path)
    # 用最小假 server: monkeypatch _spawn 使其快速"成功"并记录调用次数
    spawned = {"n": 0}

    def fake_spawn(self) -> None:
        spawned["n"] += 1
        self._proc = _FakeProc(9999)      # 带 poll() 的假进程
        self._port = 9999
        self._endpoint = "http://127.0.0.1:9999/v1/chat/completions"

    monkeypatch.setattr(QwenQualityTranslator, "_spawn", fake_spawn)
    monkeypatch.setattr(QwenQualityTranslator, "close", lambda self: None)

    barrier = threading.Barrier(8)
    endpoints: list[str] = []
    errors: list[Exception] = []

    def worker() -> None:
        barrier.wait()
        try:
            endpoints.append(q._ensure())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)

    # _spawn 必须恰好执行一次 (并发竞态修复前会是多次)
    assert spawned["n"] == 1, f"并发首次调用应只 spawn 1 次, 实际 {spawned['n']}"
    # 所有线程拿到同一 endpoint
    assert len(errors) == 0, f"不应有线程抛错: {errors}"
    assert all(e == "http://127.0.0.1:9999/v1/chat/completions" for e in endpoints)
    assert len(endpoints) == 8


def test_healthy_reuses_endpoint_no_respawn(tmp_path: Path, monkeypatch) -> None:
    """server 已就绪时反复 _ensure 不重复 spawn。"""
    q = _make_qwen(tmp_path)
    q._endpoint = "http://127.0.0.1:9999/v1/chat/completions"
    q._proc = _FakeProc(1)
    q._port = 9999
    n = {"val": 0}

    def fake_spawn(self) -> None:
        n["val"] += 1

    monkeypatch.setattr(QwenQualityTranslator, "_spawn", fake_spawn)
    for _ in range(5):
        assert q._ensure() == q._endpoint
    assert n["val"] == 0  # 就绪时绝不重 spawn