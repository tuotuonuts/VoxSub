"""Pipeline 会话状态回调（on_state）的测试。

## 为什么需要这些测试

用户报了两个问题，根因是同一个：**会话状态从未传到界面**。

  · 「麦克风同传的暂停和继续功能没有实现」
  · 「所有模式都没有结束按钮」

渲染层的 store 里 `running` / `paused` 只有初始值 false，既没有事件也没有查询 ——
于是主按钮永远显示"开始"、结束按钮永远隐藏、暂停/继续的代码分支永远走不到。

修法是在 pipeline 上加 `on_state` 回调（状态真正变化时通知），IPC 层转发成
`state` 事件，渲染层据此更新。这里守住回调这一层：

  · 生命周期转换（start/stop）要通知
  · 暂停/继续要通知（它们**不改** PipelineState，只用 _pause_evt 表达 ——
    所以不能只靠 _set_state 的钩子，必须显式发，这条最容易漏）
  · 不支持暂停的模式（C 离线文件）不得发出误导性的通知
  · 订阅者抛异常不能影响其它订阅者与主流程

测试全部用假音频源 + 假分段器（同 test_pipeline.py 的做法），
**不打开真实音频设备**，因此可以在无人值守环境里跑。
"""

from __future__ import annotations

import numpy as np
import pytest

from voxsub.pipeline import Pipeline, PipelineState


class FakeSource:
    """假音频源：反复吐出 16k 静音块，直到 stop。不碰真实设备。"""

    sample_rate = 16000

    def __init__(self) -> None:
        self._stop = False

    def start(self) -> None:
        self._stop = False

    def read_chunk(self):
        if self._stop:
            return None
        return np.zeros(480, dtype=np.float32)

    def stop(self) -> None:
        self._stop = True

    def close(self) -> None:
        pass


class _NullSegmenter:
    """假分段器：不加载模型，只满足生命周期接口。"""

    def feed(self, _chunk) -> None:
        return

    def flush(self) -> None:
        return


@pytest.fixture()
def quiet_pipeline(monkeypatch):
    """一个不加载模型、不打开音频设备的 Pipeline。"""
    p = Pipeline()
    monkeypatch.setattr(p, "_make_source", lambda: FakeSource())
    monkeypatch.setattr(p, "_build_real_time", lambda: setattr(p, "_seg", _NullSegmenter()))
    monkeypatch.setattr(p, "_start_tts_worker", lambda: None)
    yield p
    # 兜底收尾：测试失败时也别留下线程
    try:
        p.stop()
    except Exception:
        pass


def test_state_callback_fires_on_start_and_stop(quiet_pipeline: Pipeline) -> None:
    """生命周期转换必须通知订阅者 —— 这是"开始按钮变结束"的依据。"""
    p = quiet_pipeline
    events: list[tuple[bool, bool]] = []
    p.on_state(lambda: events.append((p.is_running(), p.is_paused())))

    assert events == [], "订阅后、启动前不该有通知"

    p.start()
    assert p.is_running()
    assert events, "启动后必须收到通知"
    assert events[-1][0] is True, f"最后一次通知应报告运行中，实际 {events[-1]}"

    p.stop()
    assert not p.is_running()
    assert events[-1][0] is False, f"停止后最后一次通知应报告未运行，实际 {events[-1]}"


def test_state_callback_fires_on_pause_and_resume(quiet_pipeline: Pipeline) -> None:
    """暂停/继续必须通知。

    这条最容易漏：暂停**不改** PipelineState（用 _pause_evt 表达），
    所以 _set_state 的钩子抓不到它 —— 必须由 pause()/resume() 显式发出。
    """
    p = quiet_pipeline
    p.set_mode("a")
    events: list[tuple[bool, bool]] = []
    p.on_state(lambda: events.append((p.is_running(), p.is_paused())))

    p.start()
    baseline = len(events)

    p.pause()
    assert p.is_paused(), "pause() 后 is_paused() 应为 True"
    assert len(events) > baseline, "暂停后必须收到通知"
    assert events[-1] == (True, True), f"暂停态应为 (running=True, paused=True)，实际 {events[-1]}"

    resumed = len(events)
    p.resume()
    assert not p.is_paused(), "resume() 后 is_paused() 应为 False"
    assert len(events) > resumed, "继续后必须收到通知"
    assert events[-1] == (True, False), f"继续态应为 (True, False)，实际 {events[-1]}"

    p.stop()


def test_pause_is_not_reported_for_unsupported_mode(quiet_pipeline: Pipeline) -> None:
    """C 模式（离线文件）不支持暂停，不得发出"已暂停"的误导性通知。

    后端 pause() 对 C 模式直接返回。若这里还发通知，界面会显示"已暂停"
    而实际仍在处理文件 —— 用户以为停下了，其实没有。
    """
    p = quiet_pipeline
    p.set_mode("c")
    assert p.mode == "c", "前置条件：模式应为 c"

    events: list[tuple[bool, bool]] = []
    p.on_state(lambda: events.append((p.is_running(), p.is_paused())))

    p.pause()
    assert not p.is_paused(), "C 模式不该进入暂停态"
    assert all(not paused for _running, paused in events), (
        f"C 模式不该发出 paused=True 的通知，实际 {events}"
    )


def test_pause_before_start_is_a_noop(quiet_pipeline: Pipeline) -> None:
    """未运行时暂停应无副作用，也不该产生误导性通知。"""
    p = quiet_pipeline
    events: list[tuple[bool, bool]] = []
    p.on_state(lambda: events.append((p.is_running(), p.is_paused())))

    p.pause()
    assert not p.is_paused()
    assert all(not paused for _running, paused in events), f"实际 {events}"

    p.resume()
    assert not p.is_paused()


def test_all_subscribers_are_notified(quiet_pipeline: Pipeline) -> None:
    """多个订阅者都要收到通知（IPC 层与其它观察者可能同时订阅）。"""
    p = quiet_pipeline
    first: list[bool] = []
    second: list[bool] = []
    p.on_state(lambda: first.append(p.is_running()))
    p.on_state(lambda: second.append(p.is_running()))

    p.start()
    assert first and second, f"两个订阅者都应收到通知，实际 {len(first)} / {len(second)}"
    p.stop()


def test_failing_subscriber_does_not_break_others(quiet_pipeline: Pipeline) -> None:
    """一个订阅者抛异常，不能影响其它订阅者，也不能中断主流程。

    IPC 层的回调会往 stdout 写事件；万一它抛错（管道断开等），
    不能让 pipeline 的生命周期操作跟着失败。
    """
    p = quiet_pipeline
    survivor: list[bool] = []

    def boom() -> None:
        raise RuntimeError("订阅者故意失败")

    p.on_state(boom)
    p.on_state(lambda: survivor.append(p.is_running()))

    p.start()  # 不应抛出
    assert p.is_running(), "启动流程不该被订阅者的异常打断"
    assert survivor, "另一个订阅者仍应收到通知"

    p.stop()
    assert not p.is_running()


def test_state_is_readable_without_any_subscriber(quiet_pipeline: Pipeline) -> None:
    """没有订阅者时状态仍可读 —— 界面连上后靠 state 命令主动拉取。"""
    p = quiet_pipeline
    assert p.is_running() is False
    assert p.is_paused() is False
    p.start()
    assert p.is_running() is True
    assert p.state is PipelineState.RUNNING
    p.stop()
    assert p.is_running() is False
    assert p.state is PipelineState.IDLE
