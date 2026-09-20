"""Pipeline 停止超时后的状态诚实性与资源门禁（工作单缺陷 #6）。

要钉死的三件事：

  1. **stop 超时后不能进入"安全空闲"**：worker 还在收尾（原生推理不可中断）时，
     状态必须是"停止中"，不能显示"已停止"。
  2. **worker 还活着时不得销毁或替换资源**：翻译器、识别器、模型目录、
     TTS worker 的门禁都要把"还在收尾"算进去。旧实现只看 ``self._running``，
     而超时后会落 IDLE，于是 ``_running`` 为假 → 把正在被使用的实例关掉/换掉。
  3. 超时必须能**自己收尾**：观察者等到 worker 真退出后落 IDLE 并重新打开门禁；
     否则"诚实的状态"就变成了"永久卡在停止中"。

时序用 Event 控制，不靠 sleep 猜。join 预算与观察窗口都是模块常量，测试里
把它们压到毫秒级，保证测试快且确定。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voxsub import pipeline as pipeline_module  # noqa: E402
from voxsub.pipeline import Pipeline, PipelineState  # noqa: E402


class _FakeWorker(threading.Thread):
    """一个"停不下来"的 worker：只有拿到放行信号才退出。

    它精确模拟原生推理/云端请求的行为 —— ``stop_event`` 已置位也不代表能立刻返回。
    """

    def __init__(self, name: str = "fake-worker") -> None:
        super().__init__(name=name, daemon=True)
        self.may_exit = threading.Event()
        self.started_running = threading.Event()
        self.exited = threading.Event()

    def run(self) -> None:
        self.started_running.set()
        self.may_exit.wait(timeout=30.0)
        self.exited.set()


@pytest.fixture()
def fast_deadlines(monkeypatch):
    """把 join 预算与观察窗口压到毫秒级，测试才快且不靠 sleep 撞运气。"""
    monkeypatch.setattr(pipeline_module, "_STOP_JOIN_SECONDS", 0.05)
    monkeypatch.setattr(pipeline_module, "_SETTLE_WATCH_SECONDS", 5.0)


@pytest.fixture()
def stalled_pipeline(fast_deadlines):
    """一个"正在运行、且 stop 会超时"的 pipeline。"""
    pipe = Pipeline()
    worker = _FakeWorker()
    pipe._threads = [worker]  # noqa: SLF001 - 直接构造"有 worker 在跑"的现场
    pipe._set_state(PipelineState.RUNNING)  # noqa: SLF001
    worker.start()
    assert worker.started_running.wait(timeout=5.0)
    try:
        yield pipe, worker
    finally:
        worker.may_exit.set()
        worker.exited.wait(timeout=5.0)


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# ------------------------------------------------------------- 状态不能撒谎

def test_stop_timeout_keeps_state_stopping(stalled_pipeline):
    """stop 超时 → 保持 STOPPING，**不是** IDLE。"""
    pipe, _worker = stalled_pipeline

    stopped = pipe.stop()

    assert stopped is False, "worker 还没退出，stop 不该报告已停"
    assert pipe.state is PipelineState.STOPPING, (
        f"超时后状态是 {pipe.state}，必须是 stopping —— 显示 idle 就是在撒谎"
    )
    assert pipe.is_running() is True, "STOPPING 仍算运行中，门禁必须保持关闭"


def test_stop_reports_stopping_to_the_ui(stalled_pipeline):
    """界面看到的文案要是"停止中"，不能是"已停止"。"""
    pipe, _worker = stalled_pipeline
    statuses: list[str] = []
    pipe.on_status(statuses.append)

    pipe.stop()

    assert any("停止中" in text for text in statuses), statuses
    assert not any(text.strip() == "已停止" for text in statuses), (
        f"超时却报了“已停止”：{statuses}")


def test_normal_stop_still_reaches_idle(fast_deadlines):
    """没有卡住 worker 时，行为与以前一致：立刻 IDLE。"""
    pipe = Pipeline()
    pipe._set_state(PipelineState.RUNNING)  # noqa: SLF001

    assert pipe.stop() is True
    assert pipe.state is PipelineState.IDLE


def test_stop_is_idempotent_when_already_settled(fast_deadlines):
    pipe = Pipeline()
    pipe._set_state(PipelineState.RUNNING)  # noqa: SLF001
    assert pipe.stop() is True

    assert pipe.stop() is True
    assert pipe.state is PipelineState.IDLE


# ------------------------------------------------------------- 资源门禁

def test_settling_blocks_model_directory_switch(stalled_pipeline, tmp_path):
    """停止超时后切换模型目录必须被拒 —— 识别器可能还在被 worker 用。"""
    pipe, _worker = stalled_pipeline
    pipe.stop()

    with pytest.raises(RuntimeError, match="无法切换模型目录"):
        pipe.set_models_dir(tmp_path / "new-models")


def test_settling_does_not_replace_translator(stalled_pipeline):
    """还在收尾时不许关掉 worker 正在用的翻译器。"""
    pipe, _worker = stalled_pipeline

    class _Recorder:
        closed = False

        def translate(self, text, src, dst, **_kwargs):  # pragma: no cover
            return text

        def close(self) -> None:
            self.closed = True

    live_translator = _Recorder()
    pipe._translator = live_translator  # noqa: SLF001
    pipe._trans_kind = "opus-fast"  # noqa: SLF001
    pipe.stop()

    pipe.set_translator("qwen-quality", {})

    assert live_translator.closed is False, "把还在被使用的翻译器关掉了"
    assert pipe._translator is live_translator, "把还在被使用的翻译器换掉了"


def test_settling_does_not_clear_asr_recognizer(stalled_pipeline):
    """识别器同理：收尾中不许把它置空。"""
    pipe, _worker = stalled_pipeline
    recognizer = object()
    pipe._asr = recognizer  # noqa: SLF001
    pipe._requested_asr_model_id = "asr-old"  # noqa: SLF001
    pipe.stop()

    pipe.set_asr_model("asr-new")

    assert pipe._asr is recognizer, "把还在被使用的识别器置空了"  # noqa: SLF001
    assert pipe._requested_asr_model_id == "asr-old", "收尾中不该改选型"  # noqa: SLF001


def test_may_replace_resources_is_the_single_gate(stalled_pipeline, fast_deadlines):
    """门禁是同一个判断：跑着不行、还在收尾也不行、都空了才行。"""
    pipe, worker = stalled_pipeline

    assert pipe._may_replace_resources() is False  # 运行中  # noqa: SLF001

    pipe.stop()
    assert pipe.state is PipelineState.STOPPING
    assert pipe._may_replace_resources() is False, "收尾中仍必须拒绝"  # noqa: SLF001

    worker.may_exit.set()
    assert worker.exited.wait(timeout=5.0)
    assert _wait_until(lambda: pipe.state is PipelineState.IDLE)
    assert pipe._may_replace_resources() is True  # noqa: SLF001


# ------------------------------------------------------------- 超时后能自愈

def test_settlement_watcher_reaches_idle_when_workers_exit(stalled_pipeline):
    """worker 真退出后，观察者必须把状态落回 IDLE —— 否则就永久卡在停止中。"""
    pipe, worker = stalled_pipeline
    pipe.stop()
    assert pipe.state is PipelineState.STOPPING

    worker.may_exit.set()

    assert _wait_until(lambda: pipe.state is PipelineState.IDLE), (
        "worker 已退出但状态没回落 —— 用户只能重启应用"
    )


def test_settlement_watcher_is_singular(stalled_pipeline):
    """反复 stop 只允许存在一个观察者线程。"""
    pipe, _worker = stalled_pipeline
    for _ in range(4):
        pipe.stop()

    watchers = [t for t in threading.enumerate()
                if t.name == "pipeline-settle-watch"]
    assert len(watchers) <= 1, f"观察者线程泄漏：{len(watchers)}"


def test_settlement_watcher_gives_up_but_stays_honest(monkeypatch):
    """worker 死活不退时：放弃等待，但状态**仍然**是停止中（不谎报已停）。"""
    monkeypatch.setattr(pipeline_module, "_STOP_JOIN_SECONDS", 0.05)
    monkeypatch.setattr(pipeline_module, "_SETTLE_WATCH_SECONDS", 0.2)

    pipe = Pipeline()
    worker = _FakeWorker(name="never-exits")
    pipe._threads = [worker]  # noqa: SLF001
    pipe._set_state(PipelineState.RUNNING)  # noqa: SLF001
    worker.start()
    assert worker.started_running.wait(timeout=5.0)

    try:
        pipe.stop()
        # 等到观察者放弃
        time.sleep(0.6)
        assert pipe.state is PipelineState.STOPPING, (
            "worker 没退却报 idle —— 这正是缺陷 #6 要禁的行为"
        )
        assert pipe._may_replace_resources() is False  # noqa: SLF001
    finally:
        worker.may_exit.set()
        worker.exited.wait(timeout=5.0)


# ------------------------------------------------------------- 退出保护

def test_close_does_not_release_resources_while_settling(stalled_pipeline):
    """close 必须等 worker 真退出才释放运行时组件（工作单 §3.4）。"""
    pipe, worker = stalled_pipeline

    class _Recorder:
        closed = False

        def close(self) -> None:
            self.closed = True

    translator = _Recorder()
    pipe._translator = translator  # noqa: SLF001

    assert pipe.close() is False, "收尾中不该报告已释放"
    assert translator.closed is False, "worker 还在用就关掉了翻译器"

    worker.may_exit.set()
    assert worker.exited.wait(timeout=5.0)
    assert _wait_until(lambda: pipe.close() is True), (
        "worker 退出后 close 应该能真正释放"
    )
    assert translator.closed is True
