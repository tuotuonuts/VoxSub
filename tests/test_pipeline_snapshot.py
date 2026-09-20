"""缺陷 #9：切语言/换配置时，**在途任务**必须用它提交时的快照。

## 覆盖的用户需求（工作单 §3.5）

1. 运行中切语言，**新**的句子用新语言对（用户预期）；
2. **已经进了队列**的句子用它们入队时的语言对；
3. 已经显示出来的旧结果继续显示，不清屏；
4. 会影响结果的配置变更要能观测（generation）并进入缓存/失败签名；
5. 不能悄悄用新配置解释旧任务。

## 三条语义规则

- 对谁生效：``generation`` 变化之后**新提交**的任务用新配置。在途任务（翻译
  队列条目、上下文待稳定文本、云 STT 音频、草稿请求）持有自己的
  :class:`_LangSnapshot`。
- 旧任务怎么办：**继续用原快照**跑完（不请求取消），只有"草稿"这种实时视图
  例外 —— 它在翻译期间发现代次变了就丢弃结果。
- 旧结果能不能继续显示：能。已经 emit 出去的字幕不会被回滚，语言变更也不会
  触发清屏事件。

## 线程纪律

并发用例用 ``threading.Event`` 做 barrier，不用 sleep 碰运气：慢翻译器在
``translate()`` 里 set 一个"已进入"事件并等"放行"事件，主线程据此确定
"翻译已经开始"之后再切语言。
"""
from __future__ import annotations

import dataclasses
import queue
import threading
import time
from pathlib import Path

import pytest

from voxsub.contextual_text import ContextualTextProcessor
from voxsub.live_draft import DraftTranslationRequest
from voxsub.pipeline import Pipeline, PipelineState, _LangSnapshot, _QueuedTranslation


class _RecordingTranslator:
    """替身翻译器：只记录收到的 (文本, src, dst)，不碰任何真实模型。"""

    def __init__(self, calls: list[tuple[str, str, str]] | None = None,
                 reply: str = "translated") -> None:
        self.calls = calls if calls is not None else []
        self.reply = reply

    def translate(self, text: str, src: str, dst: str, **_kwargs) -> str:
        self.calls.append((text, src, dst))
        return self.reply


def _drain_loop(pipeline: Pipeline) -> None:
    """同步跑完翻译工作线程（不真起线程）：入队的东西必须已经就位。"""
    pipeline._translation_input_done.set()  # noqa: SLF001
    pipeline._translation_loop()  # noqa: SLF001


# ---------------------------------------------------------------- 需求 1 & 2

def test_queued_sentence_keeps_the_pair_it_was_queued_with() -> None:
    """运行中切语言：**已入队**的句子仍用旧语言对翻译，不会被丢掉。"""
    p = Pipeline()
    p.set_langs("zh", "en")
    calls: list[tuple[str, str, str]] = []
    p._trans_kind = None  # 关掉译文语言校验，只观察语言对  # noqa: SLF001
    p._translator = _RecordingTranslator(calls, reply="译文")  # noqa: SLF001
    emitted: list[tuple[str, str]] = []
    p.on_utterance(lambda source, translation: emitted.append((source, translation)))

    p._on_sentence("第一句")      # 入队：zh->en
    p.set_langs("ja", "en")       # 用户此刻切语言
    _drain_loop(p)

    # 旧行为（缺陷 #9）会按 ja 过滤这句中文 → 直接消失，一条字幕都不发。
    assert calls == [("第一句", "zh", "en")]
    assert emitted == [("第一句", "译文")]


def test_sentence_queued_after_the_switch_uses_the_new_pair() -> None:
    """**新**入队的句子用新语言对（用户预期）。"""
    p = Pipeline()
    p.set_langs("zh", "en")
    calls: list[tuple[str, str, str]] = []
    p._trans_kind = None  # noqa: SLF001
    p._translator = _RecordingTranslator(calls, reply="译文")  # noqa: SLF001

    p._on_sentence("旧句")        # 入队：zh->en
    p.set_langs("ja", "en")       # 切语言
    p._on_sentence("新しい文")     # 入队：ja->en
    _drain_loop(p)

    assert calls == [("旧句", "zh", "en"), ("新しい文", "ja", "en")]


def test_translation_running_while_user_switches_language_keeps_old_pair() -> None:
    """真并发：翻译**已经在跑**时切语言，这条任务不被重新定向。"""
    p = Pipeline()
    p.set_langs("zh", "en")
    calls: list[tuple[str, str, str]] = []
    entered = threading.Event()
    release = threading.Event()

    class _BlockingTranslator:
        def translate(self, text: str, src: str, dst: str, **_kwargs) -> str:
            calls.append((text, src, dst))
            entered.set()
            assert release.wait(timeout=10.0), "翻译未能被放行"
            return "translated"

    p._translator = _BlockingTranslator()  # noqa: SLF001
    p._trans_kind = None  # noqa: SLF001
    p._on_sentence("第一句")

    worker = threading.Thread(target=p._translation_loop, name="snapshot-test")
    p._translation_input_done.set()  # noqa: SLF001
    worker.start()
    try:
        assert entered.wait(timeout=10.0), "翻译线程没有开始"
        # 翻译已经拿着旧快照在跑了，此刻用户切语言。
        p.set_langs("ja", "en")
    finally:
        release.set()
        worker.join(timeout=10.0)

    assert not worker.is_alive()
    assert calls == [("第一句", "zh", "en")]


def test_context_run_started_before_the_switch_keeps_its_pair() -> None:
    """上下文阶段：正在等待稳定的旧语言句子不会被新语言过滤掉。

    注意必须把状态设成 RUNNING：``set_langs`` 在**停机**路径上会清掉
    ``_context_processor``（下次 start 重建），只有运行中才保留上下文链路 ——
    也就是这里要覆盖的真实场景。
    """
    p = Pipeline()
    p.set_langs("zh", "en")
    p._context_processor = ContextualTextProcessor(  # noqa: SLF001
        source_lang="zh", hold_ms=1800, defer_incomplete=True,
    )
    p._set_state(PipelineState.RUNNING)  # noqa: SLF001
    p._on_sentence("因为目前成本比较低")  # noqa: SLF001
    p._on_sentence("所以我们下周开始执行。")  # noqa: SLF001

    p.set_langs("ja", "en")  # 用户在上下文还没稳定时切语言
    assert p._context_processor is not None  # noqa: SLF001

    p._context_input_done.set()  # noqa: SLF001
    p._context_loop()  # noqa: SLF001

    entry = p._translation_queue.get_nowait()  # noqa: SLF001
    assert isinstance(entry, _QueuedTranslation)
    assert entry.text == "因为目前成本比较低所以我们下周开始执行。"
    assert (entry.snapshot.src, entry.snapshot.dst) == ("zh", "en")


def test_queued_audio_keeps_the_source_language_hint_of_its_enqueue_point() -> None:
    """音频入队时的云 STT 语言提示同样来自快照，不随后续切语言漂移。"""
    p = Pipeline()
    p.set_langs("zh", "en")
    seen: list[str] = []

    class _Cloud:
        @staticmethod
        def transcribe_samples(audio, *, source_lang):
            seen.append(source_lang)
            return "原文"

    p._is_cloud_stt = True  # noqa: SLF001
    p._cloud_stt = _Cloud()  # noqa: SLF001
    p._queue_generative_audio(__import__("numpy").ones(320, dtype="float32"))  # noqa: SLF001
    p.set_langs("ja", "en")  # 音频还在队列里时切语言

    p._recognition_input_done.set()  # noqa: SLF001
    p._recognition_loop()  # noqa: SLF001

    assert seen == ["zh"]


# ------------------------------------------------------------------- 需求 3

def test_switch_does_not_clear_already_shown_subtitles() -> None:
    """已显示的旧结果继续显示；切语言也不清屏。"""
    p = Pipeline()
    p.set_langs("zh", "en")
    p._trans_kind = None  # noqa: SLF001
    p._translator = _RecordingTranslator(reply="译文")  # noqa: SLF001
    utterances: list[tuple[str, str]] = []
    drafts: list[tuple[str, str]] = []
    p.on_utterance(lambda source, translation: utterances.append((source, translation)))
    p.on_draft(lambda source, translation: drafts.append((source, translation)))

    p._translate_sentence("第一句")  # noqa: SLF001
    shown = list(utterances)
    assert shown == [("第一句", "译文")]

    p.set_langs("ja", "en")

    assert utterances == shown, "切语言不该回滚已经显示的字幕"
    # 清屏靠 emit 一对空串实现；切语言期间绝不能发这种事件。
    assert drafts == [], f"切语言触发了草稿/清屏事件: {drafts}"


# --------------------------------------------------------- 需求 4: generation

def test_generation_is_a_public_read_only_accessor() -> None:
    """IPC 层要能读到 generation，但不能写。"""
    p = Pipeline()
    assert p.config_generation == 0
    with pytest.raises(AttributeError):
        p.config_generation = 7  # type: ignore[misc]


def test_same_language_pair_does_not_bump_generation() -> None:
    """重复提交同一语言对（含只差归一化写法）不加代次，否则缓存全废。"""
    p = Pipeline()
    p.set_langs("zh", "en")
    baseline = p.config_generation

    p.set_langs("zh", "en")
    p.set_langs("ZH", "en")   # 归一化之后是同一对
    p.set_langs("zh", "EN")

    assert p.config_generation == baseline


def test_result_affecting_config_changes_bump_generation(tmp_path: Path) -> None:
    """语言对/档位/ASR 模型/模型目录/ASR 调优变了就 +1；没变就不加。"""
    p = Pipeline()
    steps: list[tuple[str, int]] = []

    def snapshot(label: str) -> int:
        value = p.config_generation
        steps.append((label, value))
        return value

    start = snapshot("start")
    p.set_langs("ja", "en")
    after_langs = snapshot("set_langs")
    assert after_langs == start + 1

    p.set_translator("qwen-quality")
    after_tier = snapshot("set_translator")
    assert after_tier == after_langs + 1
    p.set_translator("qwen-quality")          # 同值重发：不加
    assert snapshot("set_translator(same)") == after_tier

    p.set_asr_model("asr-qwen3-0.6b-int8")
    after_model = snapshot("set_asr_model")
    assert after_model == after_tier + 1
    p.set_asr_model("asr-qwen3-0.6b-int8")    # 同值重发：不加
    assert snapshot("set_asr_model(same)") == after_model

    p.set_asr_tuning({"profile": "context"})
    after_tuning = snapshot("set_asr_tuning")
    assert after_tuning == after_model + 1
    p.set_asr_tuning({"profile": "context"})          # 同值重发：不加
    assert snapshot("set_asr_tuning(same)") == after_tuning
    p.set_asr_tuning({"asr_tuning_profile": "context"})  # 归一化后等价：不加
    assert snapshot("set_asr_tuning(alias)") == after_tuning

    p.set_models_dir(tmp_path / "models-a")
    after_dir = snapshot("set_models_dir")
    assert after_dir == after_tuning + 1
    p.set_models_dir(tmp_path / "models-a")    # 同一目录：不加
    assert snapshot("set_models_dir(same)") == after_dir


def test_snapshot_pair_and_generation_match_the_moment_of_capture() -> None:
    """快照取的是"某一刻的语言对 + 那一刻的代次"，不会读到半更新状态。"""
    p = Pipeline()
    p.set_langs("zh", "en")
    first = p._lang_snapshot()  # noqa: SLF001
    assert first == _LangSnapshot("zh", "en", first.generation)

    p.set_langs("ja", "zh")
    second = p._lang_snapshot()  # noqa: SLF001

    assert (first.src, first.dst) == ("zh", "en")
    assert (second.src, second.dst) == ("ja", "zh")
    assert second.generation == first.generation + 1
    assert first != second


def test_snapshot_is_immutable() -> None:
    """"不可变"必须是真不可变，不是约定。"""
    entry = _QueuedTranslation("句子", _LangSnapshot("zh", "en", 3), 1.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.snapshot.dst = "ja"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.snapshot = _LangSnapshot("ja", "en", 4)  # type: ignore[misc]


# ------------------------------------------------------ 需求 5: 缓存/失败键

def test_failure_signature_includes_generation_so_old_keys_miss() -> None:
    """同一段文字 + 语言对没变，但代次变了 → 失败签名必须换新（旧键不命中）。"""
    p = Pipeline()
    p._trans_kind = "mock"  # noqa: SLF001
    p._pair_fail_key = None  # noqa: SLF001

    p._log_translate_failure("文字", None, 1.0, RuntimeError("boom"),  # noqa: SLF001
                             p._lang_snapshot())  # noqa: SLF001
    first_key = p._pair_fail_key
    assert first_key == ("mock", "zh", "en", 0)

    # 只换代次，语言对与档位都不动。
    p.set_asr_tuning({"profile": "context"})
    p._log_translate_failure("文字", None, 1.0, RuntimeError("boom"),  # noqa: SLF001
                             p._lang_snapshot())  # noqa: SLF001
    second_key = p._pair_fail_key

    assert second_key == ("mock", "zh", "en", p.config_generation)
    assert second_key != first_key, "代次变了，旧失败键不该继续命中"


def test_failure_signature_still_collapses_within_one_generation() -> None:
    """同一代内重复失败继续被折叠 —— 别把去重一起改没了。"""
    p = Pipeline()
    p._trans_kind = "mock"  # noqa: SLF001
    p._pair_fail_key = None  # noqa: SLF001
    snapshot = p._lang_snapshot()  # noqa: SLF001

    p._log_translate_failure("一", None, 1.0, RuntimeError("boom"), snapshot)  # noqa: SLF001
    first_key = p._pair_fail_key
    p._log_translate_failure("二", None, 1.0, RuntimeError("boom"), snapshot)  # noqa: SLF001

    assert p._pair_fail_key == first_key


# ------------------------------------------------------------ live draft 语义

def test_draft_result_is_kept_when_config_is_stable() -> None:
    """对照组：配置没动，草稿译文正常贴上。"""
    p = Pipeline()
    p.set_langs("zh", "en")
    p._trans_kind = None  # noqa: SLF001
    p._translator = _RecordingTranslator(reply="hello")  # noqa: SLF001
    drafts: list[tuple[str, str]] = []
    p.on_draft(lambda source, translation: drafts.append((source, translation)))

    p._live_draft.update_source("第一句")  # noqa: SLF001
    p._translate_draft(DraftTranslationRequest(1, "第一句"),  # noqa: SLF001
                       p._lang_snapshot())  # noqa: SLF001

    assert drafts == [("第一句", "hello")]


def test_draft_result_is_dropped_when_config_changes_mid_flight() -> None:
    """草稿是实时视图：翻译期间配置变了就丢结果，不把旧配置的译文贴上去。"""
    p = Pipeline()
    p.set_langs("zh", "en")
    p._trans_kind = None  # noqa: SLF001
    drafts: list[tuple[str, str]] = []
    p.on_draft(lambda source, translation: drafts.append((source, translation)))

    class _SwitchingTranslator:
        def translate(self, text: str, src: str, dst: str, **_kwargs) -> str:
            p.set_langs("ja", "en")   # 翻译期间用户切了语言
            return "hello"

    p._translator = _SwitchingTranslator()  # noqa: SLF001
    p._live_draft.update_source("第一句")  # noqa: SLF001
    p._translate_draft(DraftTranslationRequest(1, "第一句"),  # noqa: SLF001
                       p._lang_snapshot())  # noqa: SLF001

    assert drafts == [], "配置在翻译期间变了，旧方向的草稿译文不该显示"


def test_draft_after_the_switch_uses_the_new_pair() -> None:
    """切语言之后新取的草稿快照，用的就是新语言对。"""
    p = Pipeline()
    p.set_langs("zh", "en")
    calls: list[tuple[str, str, str]] = []
    p._trans_kind = None  # noqa: SLF001
    p._translator = _RecordingTranslator(calls, reply="hello")  # noqa: SLF001

    p.set_langs("ja", "zh")
    p._live_draft.update_source("新しい文")  # noqa: SLF001
    p._translate_draft(DraftTranslationRequest(1, "新しい文"),  # noqa: SLF001
                       p._lang_snapshot())  # noqa: SLF001

    assert calls == [("新しい文", "ja", "zh")]


# --------------------------------------------------- 队列条目自身携带快照

def test_queue_entry_carries_text_snapshot_and_timestamp() -> None:
    """入队条目必须自带 (文本, 快照, 时间)，而不是只有一个裸字符串。"""
    p = Pipeline()
    p.set_langs("zh", "en")

    p._on_sentence("第一句")  # noqa: SLF001

    entry = p._translation_queue.get_nowait()  # noqa: SLF001
    assert isinstance(entry, _QueuedTranslation)
    assert entry.text == "第一句"
    assert (entry.snapshot.src, entry.snapshot.dst) == ("zh", "en")
    assert entry.snapshot.generation == p.config_generation
    assert isinstance(entry.queued_at, float)
    assert entry.queued_at <= time.monotonic()


def test_legacy_bare_string_entries_still_translate() -> None:
    """兼容路径：集成方直接 put 裸字符串时仍然能翻（退化成出队时的配置）。"""
    p = Pipeline()
    p.set_langs("zh", "en")
    calls: list[tuple[str, str, str]] = []
    p._trans_kind = None  # noqa: SLF001
    p._translator = _RecordingTranslator(calls, reply="译文")  # noqa: SLF001

    p._translation_queue.put("第一句")  # noqa: SLF001
    _drain_loop(p)

    assert calls == [("第一句", "zh", "en")]


def test_queue_stays_empty_when_nothing_was_queued() -> None:
    """回归护栏：loop 空转不该产出任何条目，也不该卡住。"""
    p = Pipeline()
    _drain_loop(p)
    with pytest.raises(queue.Empty):
        p._translation_queue.get_nowait()  # noqa: SLF001
