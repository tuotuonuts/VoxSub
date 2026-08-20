"""Windows 按应用声音隔离的单元与真机冒烟测试。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import numpy as np
import pytest

from voxsub.process_audio import (
    CaptureTarget,
    ProcessLoopbackSource,
    _capture_root_pid,
    list_capture_targets,
)


def test_capture_target_label() -> None:
    target = CaptureTarget(42, "Meeting.exe", "团队周会")
    assert target.label == "Meeting.exe — 团队周会"


def test_process_source_rejects_invalid_pid() -> None:
    with pytest.raises(ValueError, match="正整数"):
        ProcessLoopbackSource(0)


def test_teams_child_window_promotes_to_host_process() -> None:
    class _Proc:
        def __init__(self, pid, name, parent=None):
            self.pid = pid
            self._name = name
            self._parent = parent

        def name(self):
            return self._name

        def parent(self):
            return self._parent

    host = _Proc(100, "ms-teams.exe")
    webview = _Proc(200, "msedgewebview2.exe", host)

    class _Psutil:
        @staticmethod
        def Process(pid):
            assert pid == 200
            return webview

    assert _capture_root_pid(200, _Psutil) == 100


def test_same_process_children_promote_to_their_root() -> None:
    class _Proc:
        def __init__(self, pid, parent=None):
            self.pid = pid
            self._parent = parent

        def name(self):
            return "chrome.exe"

        def parent(self):
            return self._parent

    root = _Proc(300)
    child = _Proc(301, root)

    class _Psutil:
        @staticmethod
        def Process(pid):
            return child

    assert _capture_root_pid(301, _Psutil) == 300


def test_list_capture_targets_deduplicates_pid(monkeypatch) -> None:
    import recap.discovery

    class _Window:
        def __init__(self, pid: int, title: str):
            self.pid, self.title = pid, title

    pid = os.getpid() + 10000
    monkeypatch.setattr(recap.discovery, "list_windows", lambda: [
        _Window(pid, "短标题"),
        _Window(pid, "信息更完整的会议窗口"),
        _Window(os.getpid(), "VoxSub 自身"),
        _Window(pid + 1, ""),
    ])
    targets = list_capture_targets()
    assert len(targets) == 1
    assert targets[0].pid == pid
    assert targets[0].window_title == "信息更完整的会议窗口"


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("VOXSUB_TEST_PROCESS_AUDIO") != "1",
                    reason="设置 VOXSUB_TEST_PROCESS_AUDIO=1 才播放短测试音并验证进程捕获")
def test_process_loopback_captures_target_tone() -> None:
    """ffplay 目标进程短音 → process loopback 应捕获到非静音 PCM。"""
    ffplay = shutil.which("ffplay")
    if not ffplay:
        pytest.skip("PATH 中没有 ffplay")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [ffplay, "-loglevel", "quiet", "-nodisp", "-autoexit", "-volume", "3",
         "-f", "lavfi", "-i", "sine=frequency=523:duration=5"],
        creationflags=flags,
    )
    src = ProcessLoopbackSource(proc.pid)
    try:
        time.sleep(0.4)
        src.start()
        chunks = [src.read_chunk() for _ in range(55)]
        live = [c for c in chunks if c is not None]
        assert live
        assert all(c.dtype == np.float32 and c.shape == (480,) for c in live)
        peak = max(float(np.max(np.abs(c))) for c in live)
        assert peak > 1e-5, f"目标进程捕获结果持续静音, peak={peak:.7f}"
    finally:
        src.stop()
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("VOXSUB_TEST_PROCESS_AUDIO") != "1",
                    reason="设置 VOXSUB_TEST_PROCESS_AUDIO=1 才播放短测试音并验证进程隔离")
def test_process_loopback_excludes_other_process_tone() -> None:
    """目标静音进程 + 其它进程播放声音时，目标流应保持静音。"""
    ffplay = shutil.which("ffplay")
    if not ffplay:
        pytest.skip("PATH 中没有 ffplay")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    target = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"], creationflags=flags)
    other = subprocess.Popen(
        [ffplay, "-loglevel", "quiet", "-nodisp", "-autoexit", "-volume", "3",
         "-f", "lavfi", "-i", "sine=frequency=659:duration=5"],
        creationflags=flags,
    )
    src = ProcessLoopbackSource(target.pid)
    try:
        time.sleep(0.4)
        src.start()
        chunks = [src.read_chunk() for _ in range(40)]
        live = [c for c in chunks if c is not None]
        assert live
        peak = max(float(np.max(np.abs(c))) for c in live)
        assert peak < 1e-5, f"捕获到了目标进程之外的声音, peak={peak:.7f}"
    finally:
        src.stop()
        for proc in (target, other):
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
