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
import weakref
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
def stalled_pipeline(fast_deadlines, tmp_path):
    """一个"正在运行、且 stop 会超时"的 pipeline，使用隔离模型路径。"""
    pipe = Pipeline(models=tmp_path / "models")
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


def test_start_claim_blocks_resource_setters_until_start_finishes(tmp_path):
    """资源替换不应穿过 start() 已认领但尚未发布 worker 的窗口。"""
    pipe = Pipeline(models=tmp_path / "models")
    original_models_dir = pipe._models_dir  # noqa: SLF001
    original_tuning = dict(pipe._asr_tuning)  # noqa: SLF001
    assert pipe._claim_start() is True  # noqa: SLF001
    try:
        assert pipe.state is PipelineState.IDLE
        assert pipe._start_in_progress is True  # noqa: SLF001
        with pytest.raises(RuntimeError, match="无法切换模型目录"):
            pipe.set_models_dir(tmp_path / "replacement")
        pipe.set_translator("qwen-quality", {"token": "test-only"})
        pipe.set_asr_model("asr-test")
        pipe.set_stt("cloud", {"endpoint": "https://example.invalid"})
        pipe.set_asr_tuning({"asr_vad_threshold": 0.7})
        assert pipe._models_dir == original_models_dir  # noqa: SLF001
        assert pipe._requested_trans_kind == "opus-fast"  # noqa: SLF001
        assert pipe._requested_asr_model_id == "asr-zipformer-bilingual-fast"  # noqa: SLF001
        assert pipe._requested_stt_provider == "local"  # noqa: SLF001
        assert pipe._asr_tuning == original_tuning  # noqa: SLF001
    finally:
        pipe._cancel_start()  # noqa: SLF001
        pipe._finish_start()  # noqa: SLF001

    pipe.set_asr_model("asr-after-start")
    assert pipe._requested_asr_model_id == "asr-after-start"  # noqa: SLF001
    assert pipe.close() is True


def test_closed_pipeline_rejects_resource_replacement(tmp_path):
    """关闭后的 Pipeline 不得重建或接受新的资源配置。"""
    pipe = Pipeline(models=tmp_path / "models")
    assert pipe.close() is True
    with pytest.raises(RuntimeError, match="无法切换模型目录"):
        pipe.set_models_dir(tmp_path / "replacement")
    pipe.set_translator("qwen-quality", {"token": "test-only"})
    pipe.set_asr_model("asr-test")
    pipe.set_stt("cloud", {"endpoint": "https://example.invalid"})
    pipe.set_asr_tuning({"asr_vad_threshold": 0.7})
    assert pipe._models_dir == tmp_path / "models"  # noqa: SLF001
    assert pipe._requested_trans_kind == "opus-fast"  # noqa: SLF001
    assert pipe._requested_asr_model_id == "asr-zipformer-bilingual-fast"  # noqa: SLF001
    assert pipe._requested_stt_provider == "local"  # noqa: SLF001
    assert pipe._asr_tuning == {"profile": "auto", "hotwords": ""}  # noqa: SLF001


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


def test_normal_stop_still_reaches_idle(fast_deadlines, tmp_path):
    """没有卡住 worker 时，行为与以前一致：立刻 IDLE。"""
    pipe = Pipeline(models=tmp_path / "models")
    pipe._set_state(PipelineState.RUNNING)  # noqa: SLF001

    assert pipe.stop() is True
    assert pipe.state is PipelineState.IDLE


@pytest.mark.parametrize("stalled", [False, True])
def test_stop_after_worker_publication_is_completed_by_start_handoff(
        fast_deadlines, tmp_path, monkeypatch, stalled):
    """An ordinary stop after publication must still join or install a watcher."""
    pipe = Pipeline(models=tmp_path / "models")
    published, release_start = threading.Event(), threading.Event()
    worker = _FakeWorker(name="publication-handoff-worker")
    if not stalled:
        worker.may_exit.set()
    monkeypatch.setattr(pipe, "_new_realtime_threads", lambda: [worker])
    original_finish = pipe._finish_start
    errors = []
    source_stopped = threading.Event()
    joined = threading.Event()

    class _Source:
        def stop(self):
            source_stopped.set()

    pipe._source = _Source()
    original_join = worker.join

    def observed_join(timeout=None):
        joined.set()
        return original_join(timeout=timeout)

    monkeypatch.setattr(worker, "join", observed_join)

    def blocked_finish():
        published.set()
        if not release_start.wait(timeout=2):
            raise AssertionError("publication barrier was not released")
        original_finish()

    monkeypatch.setattr(pipe, "_finish_start", blocked_finish)

    def start():
        try:
            pipe.start()
        except Exception as exc:
            errors.append(exc)

    starter = threading.Thread(target=start, daemon=True)
    starter.start()
    try:
        assert published.wait(timeout=2)
        assert worker.started_running.wait(timeout=2)
        assert pipe.state is PipelineState.RUNNING
        assert pipe.stop() is False
        assert pipe._stop_evt.is_set()
        release_start.set()
        starter.join(timeout=2)
        assert not starter.is_alive()
        assert errors == []
        assert source_stopped.is_set(), "deferred stop never reached the published source"
        assert joined.is_set(), "deferred stop never joined the published workers"
        if stalled:
            assert pipe.state is PipelineState.STOPPING
            watcher = pipe._settle_watcher
            assert watcher.is_alive()
            assert not pipe._may_replace_resources()
            worker.may_exit.set()
            worker.join(timeout=2)
            watcher.join(timeout=2)
            assert not watcher.is_alive()
        assert pipe.state is PipelineState.IDLE
        assert pipe._threads == []
        assert pipe._may_replace_resources()
        assert pipe._closed is False, "ordinary stop must not become close"
    finally:
        release_start.set()
        worker.may_exit.set()
        starter.join(timeout=2)
        if worker.ident is not None:
            worker.join(timeout=2)
        pipe.close()
        watcher = getattr(pipe, "_settle_watcher", None)
        if watcher is not None:
            watcher.join(timeout=2)


def test_stop_is_idempotent_when_already_settled(fast_deadlines, tmp_path):
    pipe = Pipeline(models=tmp_path / "models")
    pipe._set_state(PipelineState.RUNNING)  # noqa: SLF001
    assert pipe.stop() is True

    assert pipe.stop() is True
    assert pipe.state is PipelineState.IDLE


# ------------------------------------------------------------- 资源门禁

def test_failed_worker_blocks_stt_resource_replacement(stalled_pipeline):
    """FAILED 但 worker 未退出时，切 STT 不得关闭/丢弃在用组件。"""
    pipe, _worker = stalled_pipeline

    class _CloudSTT:
        closed = False

        def close(self) -> None:
            self.closed = True

    pipe._set_state(PipelineState.FAILED)  # noqa: SLF001
    cloud_stt = _CloudSTT()
    vad = object()
    segmenter = object()
    pipe._requested_stt_provider = "cloud"  # noqa: SLF001
    pipe._stt_config = {"endpoint": "old"}  # noqa: SLF001
    pipe._cloud_stt = cloud_stt  # noqa: SLF001
    pipe._vad = vad  # noqa: SLF001
    pipe._seg = segmenter  # noqa: SLF001

    pipe.set_stt("local", {})

    assert cloud_stt.closed is False
    assert pipe._requested_stt_provider == "cloud"  # noqa: SLF001
    assert pipe._stt_config == {"endpoint": "old"}  # noqa: SLF001
    assert pipe._cloud_stt is cloud_stt  # noqa: SLF001
    assert pipe._vad is vad  # noqa: SLF001
    assert pipe._seg is segmenter  # noqa: SLF001


def test_failed_worker_blocks_asr_tuning_replacement(stalled_pipeline):
    """FAILED 但 worker 未退出时，调优更新不得清除在用识别器/分段器。"""
    pipe, _worker = stalled_pipeline
    pipe._set_state(PipelineState.FAILED)  # noqa: SLF001
    recognizer = object()
    vad = object()
    segmenter = object()
    context_processor = object()
    pipe._asr = recognizer  # noqa: SLF001
    pipe._vad = vad  # noqa: SLF001
    pipe._seg = segmenter  # noqa: SLF001
    pipe._context_processor = context_processor  # noqa: SLF001
    pipe._asr_tuning = {}  # noqa: SLF001
    generation = pipe.config_generation

    pipe.set_asr_tuning({"asr_vad_threshold": 0.33})

    assert pipe._asr_tuning == {}  # noqa: SLF001
    assert pipe.config_generation == generation
    assert pipe._asr is recognizer  # noqa: SLF001
    assert pipe._vad is vad  # noqa: SLF001
    assert pipe._seg is segmenter  # noqa: SLF001
    assert pipe._context_processor is context_processor  # noqa: SLF001


def test_failed_worker_keeps_asr_references_when_languages_change(stalled_pipeline):
    """FAILED 但 worker 未退出时，语言更新仍不得拆掉它正在使用的链路。"""
    pipe, _worker = stalled_pipeline
    pipe._set_state(PipelineState.FAILED)  # noqa: SLF001
    recognizer = object()
    vad = object()
    segmenter = object()
    context_processor = object()
    pipe._asr = recognizer  # noqa: SLF001
    pipe._vad = vad  # noqa: SLF001
    pipe._seg = segmenter  # noqa: SLF001
    pipe._context_processor = context_processor  # noqa: SLF001
    pipe._src_lang = "en"  # noqa: SLF001
    pipe._dst_lang = "zh"  # noqa: SLF001
    generation = pipe.config_generation

    pipe.set_langs("zh", "en")

    assert (pipe._src_lang, pipe._dst_lang) == ("zh", "en")  # noqa: SLF001
    assert pipe.config_generation == generation + 1
    assert pipe._asr is recognizer  # noqa: SLF001
    assert pipe._vad is vad  # noqa: SLF001
    assert pipe._seg is segmenter  # noqa: SLF001
    assert pipe._context_processor is context_processor  # noqa: SLF001


def test_stuck_tts_worker_blocks_restart_after_stop_timeout(fast_deadlines, tmp_path):
    """An idle-looking Pipeline must not start over an audio worker that is still alive."""
    pipe = Pipeline(models=tmp_path / "models")

    class _TTSWorker:
        name = "pipeline-tts"
        alive = True

        @property
        def is_alive(self):
            return self.alive

        def stop(self):
            return not self.alive

    worker = _TTSWorker()
    pipe._tts_worker = worker
    pipe._set_state(PipelineState.RUNNING)

    assert pipe.stop() is False
    assert pipe.state is PipelineState.STOPPING
    assert pipe._is_settling() is True
    with pytest.raises(RuntimeError, match="上一任务仍在安全收尾"):
        pipe.start()

    worker.alive = False
    watcher = pipe._settle_watcher
    watcher.join(timeout=1)
    assert not watcher.is_alive()
    assert pipe.state is PipelineState.IDLE
    assert pipe._tts_worker is None
    assert pipe.close() is True


def test_running_tts_worker_is_not_a_settling_window(fast_deadlines, tmp_path):
    """The TTS worker is intentionally alive during normal operation/hot reload."""
    pipe = Pipeline(models=tmp_path / "models")

    class _TTSWorker:
        is_alive = True

        def stop(self):
            self.is_alive = False
            return True

    worker = _TTSWorker()
    pipe._tts_worker = worker
    pipe._set_state(PipelineState.RUNNING)
    assert pipe._is_settling() is False

    pipe._set_state(PipelineState.IDLE)
    worker.stop()
    assert pipe.close() is True


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
    # IDLE is published before the stop finalizer releases its ownership.
    # Wait for the actual resource gate, not just the earlier state event.
    assert _wait_until(pipe._may_replace_resources)  # noqa: SLF001


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


def test_settlement_watcher_gives_up_but_stays_honest(monkeypatch, tmp_path):
    """worker 死活不退时：放弃等待，但状态**仍然**是停止中（不谎报已停）。"""
    monkeypatch.setattr(pipeline_module, "_STOP_JOIN_SECONDS", 0.05)
    monkeypatch.setattr(pipeline_module, "_SETTLE_WATCH_SECONDS", 0.2)

    pipe = Pipeline(models=tmp_path / "models")
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

def test_close_finishes_cleanup_after_timed_out_worker_exits(stalled_pipeline):
    """A timed-out close must retain itself and finish cleanup without a later owner call."""
    pipe, worker = stalled_pipeline

    class _Recorder:
        def __init__(self):
            self.closed = threading.Event()

        def close(self) -> None:
            self.closed.set()

    class _LocalRecognizer:
        pass

    translator = _Recorder()
    recognizer = _LocalRecognizer()
    recognizer_ref = weakref.ref(recognizer)
    pipe._translator = translator  # noqa: SLF001
    pipe._asr = recognizer  # noqa: SLF001

    assert pipe.close() is False
    pipeline_ref = weakref.ref(pipe)
    del recognizer
    del pipe

    assert pipeline_ref() is not None, "异步收尾期间 Pipeline 必须仍由 watcher 持有"
    worker.may_exit.set()
    assert worker.exited.wait(timeout=5.0)
    assert translator.closed.wait(timeout=2.0), "worker 退出后 close 应自动完成资源释放"
    assert recognizer_ref() is None, "自动 close 应解除本地识别器引用"


def test_close_keeps_cleanup_watcher_after_its_first_deadline(
        monkeypatch, stalled_pipeline, caplog):
    """A delayed worker must still be cleaned if it exits after the first watch window."""
    monkeypatch.setattr(pipeline_module, "_STOP_JOIN_SECONDS", 0.01)
    monkeypatch.setattr(pipeline_module, "_SETTLE_WATCH_SECONDS", 0.05)
    pipe, worker = stalled_pipeline

    class _Recorder:
        def __init__(self):
            self.closed = threading.Event()

        def close(self) -> None:
            self.closed.set()

    translator = _Recorder()
    pipe._translator = translator  # noqa: SLF001

    assert pipe.close() is False
    assert _wait_until(
        lambda: any("仍未退出" in record.getMessage() for record in caplog.records),
        timeout=2.0,
    ), "test must observe the first settlement deadline before releasing the worker"
    assert not translator.closed.is_set()

    worker.may_exit.set()
    assert worker.exited.wait(timeout=5.0)
    assert translator.closed.wait(timeout=2.0), (
        "close request must retry resource cleanup after a later worker settlement"
    )
    assert pipe.state is PipelineState.IDLE



def test_close_request_at_watcher_handoff_keeps_cleanup_owner(monkeypatch, tmp_path):
    """A close racing an ordinary-watch timeout must not strand cleanup."""
    monkeypatch.setattr(pipeline_module, "_STOP_JOIN_SECONDS", 0.01)
    monkeypatch.setattr(pipeline_module, "_SETTLE_WATCH_SECONDS", 0.03)
    pipe = Pipeline(models=tmp_path / "models")
    worker = _FakeWorker(name="handoff-worker")
    pipe._threads = [worker]  # noqa: SLF001
    pipe._set_state(PipelineState.RUNNING)  # noqa: SLF001
    worker.start()
    assert worker.started_running.wait(timeout=2.0)

    class _Recorder:
        def __init__(self):
            self.closed = threading.Event()

        def close(self) -> None:
            self.closed.set()

    translator = _Recorder()
    pipe._translator = translator  # noqa: SLF001
    handoff_reached = threading.Event()
    release_handoff = threading.Event()
    original_timeout = pipe._handle_settlement_timeout  # noqa: SLF001

    def pause_at_handoff(alive, tts_alive, timeout_reported):
        result = original_timeout(alive, tts_alive, timeout_reported)
        if result[0] is None:
            handoff_reached.set()
            if not release_handoff.wait(timeout=5.0):
                raise AssertionError("test did not release the watcher handoff")
        return result

    pipe._handle_settlement_timeout = pause_at_handoff  # noqa: SLF001
    close_done = threading.Event()
    close_results: list[bool] = []

    def close_pipeline() -> None:
        close_results.append(pipe.close())
        close_done.set()

    closer = threading.Thread(target=close_pipeline, name="test-close-handoff", daemon=True)
    try:
        assert pipe.stop() is False
        assert handoff_reached.wait(timeout=2.0), "watcher did not reach timeout handoff"
        closer.start()
        assert close_done.wait(timeout=2.0), "close should return bounded while worker is blocked"
        assert close_results == [False]
        release_handoff.set()
        worker.may_exit.set()
        assert worker.exited.wait(timeout=2.0)
        assert translator.closed.wait(timeout=2.0), (
            "the watcher retired after an ordinary timeout and stranded the later close"
        )
    finally:
        release_handoff.set()
        worker.may_exit.set()
        worker.join(timeout=2.0)
        if closer.ident is not None:
            closer.join(timeout=2.0)
        if not translator.closed.is_set():
            pipe.close()
        watcher = getattr(pipe, "_settle_watcher", None)
        if watcher is not None:
            watcher.join(timeout=2.0)

def test_settling_does_not_restart_the_tts_worker(stalled_pipeline, monkeypatch):
    """收尾窗口里不许拆建 TTS worker（它也在被 worker 持有）。

    运行中热重载 TTS 是**刻意支持**的（换模型立刻生效），所以这条门禁不能用
    `_may_replace_resources`（那会把热重载一起否掉），只能是"排除收尾中"。
    """
    pipe, _worker = stalled_pipeline
    calls: list[str] = []
    monkeypatch.setattr(pipe, "_stop_tts_worker", lambda: calls.append("stop"))
    monkeypatch.setattr(pipe, "_start_tts_worker", lambda: calls.append("start"))
    pipe._tts_enabled = True  # noqa: SLF001
    pipe._tts_model_ids = {"zh": "old-zh", "en": "old-en"}  # noqa: SLF001

    pipe.stop()                      # 进入收尾窗口
    assert pipe.state is PipelineState.STOPPING
    calls.clear()

    pipe.set_tts_models({"zh": "new-zh", "en": "new-en"})

    assert calls == [], f"收尾中拆建了 TTS worker：{calls}"


def test_running_state_still_hot_reloads_the_tts_worker(fast_deadlines, monkeypatch, tmp_path):
    """正常运行中仍然要热重载 —— 门禁不能把这条刻意能力一起关掉。"""
    pipe = Pipeline(models=tmp_path / "models")
    pipe._set_state(PipelineState.RUNNING)  # noqa: SLF001
    pipe._tts_enabled = True  # noqa: SLF001
    pipe._tts_model_ids = {"zh": "old-zh", "en": "old-en"}  # noqa: SLF001
    calls: list[str] = []
    monkeypatch.setattr(pipe, "_stop_tts_worker", lambda: calls.append("stop"))
    monkeypatch.setattr(pipe, "_start_tts_worker", lambda: calls.append("start"))

    pipe.set_tts_models({"zh": "new-zh", "en": "new-en"})

    assert calls == ["stop", "start"], f"运行中应当热重载：{calls}"


@pytest.mark.parametrize("setter_name", ["set_tts", "set_tts_models"])
@pytest.mark.parametrize("lifecycle", ["closed", "starting"])
def test_tts_setters_do_not_change_or_start_resources_during_final_lifecycle(
        tmp_path, monkeypatch, setter_name: str, lifecycle: str) -> None:
    pipe = Pipeline(models=tmp_path / "models")
    original_enabled = pipe._tts_enabled  # noqa: SLF001
    original_models = dict(pipe._tts_model_ids)  # noqa: SLF001
    if lifecycle == "closed":
        assert pipe.close() is True
    else:
        pipe._start_in_progress = True  # noqa: SLF001

    starts: list[bool] = []
    monkeypatch.setattr(pipe, "_start_tts_worker", lambda: starts.append(True))
    if setter_name == "set_tts":
        pipe.set_tts(not original_enabled)
    else:
        pipe.set_tts_models({"zh": "new-zh", "en": "new-en"})

    assert pipe._tts_enabled is original_enabled  # noqa: SLF001
    assert pipe._tts_model_ids == original_models  # noqa: SLF001
    assert starts == []
    if lifecycle == "starting":
        pipe._start_in_progress = False  # noqa: SLF001
        assert pipe.close() is True


@pytest.mark.parametrize("setter_name", ["set_tts", "set_tts_models"])
def test_tts_hot_reload_and_close_are_serialized(
        tmp_path, monkeypatch, setter_name: str) -> None:
    """A close cannot finish between a TTS setter's eligibility check and restart."""
    pipe = Pipeline(models=tmp_path / "models")
    pipe._set_state(PipelineState.RUNNING)  # noqa: SLF001
    if setter_name == "set_tts":
        pipe._tts_enabled = False  # noqa: SLF001
        active = False
    else:
        pipe._tts_enabled = True  # noqa: SLF001
        pipe._tts_model_ids = {"zh": "old-zh", "en": "old-en"}  # noqa: SLF001
        active = True

    eligible_check = threading.Event()
    release_check = threading.Event()
    original_check = pipe._is_settling  # noqa: SLF001

    def paused_check() -> bool:
        eligible_check.set()
        if not release_check.wait(timeout=5.0):
            raise AssertionError("test did not release the TTS eligibility check")
        return original_check()

    monkeypatch.setattr(pipe, "_is_settling", paused_check)

    def start_worker() -> None:
        nonlocal active
        active = True

    def stop_worker() -> bool:
        nonlocal active
        active = False
        return True

    monkeypatch.setattr(pipe, "_start_tts_worker", start_worker)
    monkeypatch.setattr(pipe, "_stop_tts_worker", stop_worker)
    setter_done = threading.Event()
    close_started = threading.Event()
    close_done = threading.Event()
    close_results: list[bool] = []

    def update_tts() -> None:
        if setter_name == "set_tts":
            pipe.set_tts(True)
        else:
            pipe.set_tts_models({"zh": "new-zh", "en": "new-en"})
        setter_done.set()

    def close_pipeline() -> None:
        close_started.set()
        close_results.append(pipe.close())
        close_done.set()

    setter = threading.Thread(target=update_tts, name="test-tts-update", daemon=True)
    closer = threading.Thread(target=close_pipeline, name="test-tts-close", daemon=True)
    try:
        setter.start()
        assert eligible_check.wait(timeout=2.0)
        closer.start()
        assert close_started.wait(timeout=2.0)
        assert not close_done.wait(timeout=0.05), (
            "close completed while a TTS setter still owned the pre-close decision"
        )
        release_check.set()
        assert setter_done.wait(timeout=2.0)
        assert close_done.wait(timeout=2.0)
        assert close_results == [True]
        assert active is False, "a TTS worker was reopened after Pipeline.close()"
    finally:
        release_check.set()
        if setter.ident is not None:
            setter.join(timeout=2.0)
        if closer.ident is not None:
            closer.join(timeout=2.0)
        if not pipe._closed:  # noqa: SLF001
            pipe.close()
