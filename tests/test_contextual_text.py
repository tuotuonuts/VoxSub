from __future__ import annotations

from voxsub.contextual_text import (
    ContextualTextProcessor,
    format_partial_for_display,
    looks_incomplete,
)


def test_semantic_boundary_recognizes_incomplete_and_complete_phrases() -> None:
    assert looks_incomplete("因为目前成本比较低", "zh")
    assert looks_incomplete("I think we should", "en")
    assert not looks_incomplete("这个方案已经完成。", "zh")
    assert not looks_incomplete("好的", "zh")
    assert not looks_incomplete("This item is now complete.", "en")


def test_incomplete_fragments_merge_before_commit() -> None:
    processor = ContextualTextProcessor(source_lang="zh", hold_ms=1800)
    assert processor.submit("因为目前成本比较低", now=0.0) == []
    segments = processor.submit("所以我们下周开始执行。", now=0.8)
    assert [segment.text for segment in segments] == [
        "因为目前成本比较低所以我们下周开始执行。"
    ]
    assert segments[0].raw_text == "因为目前成本比较低所以我们下周开始执行。"


def test_incomplete_fragment_commits_at_latency_deadline() -> None:
    processor = ContextualTextProcessor(source_lang="zh", hold_ms=1200)
    assert processor.submit("我认为这个方案", now=10.0) == []
    assert processor.poll(now=11.19) == []
    segments = processor.poll(now=11.20)
    assert [segment.text for segment in segments] == ["我认为这个方案"]


def test_new_fragments_cannot_extend_the_original_latency_deadline() -> None:
    processor = ContextualTextProcessor(source_lang="zh", hold_ms=1200)
    assert processor.submit("我认为这个方案", now=10.0) == []

    segments = processor.submit("还需要", now=11.3)

    assert [segment.text for segment in segments] == ["我认为这个方案"]
    assert processor.pending_text == "还需要"
    assert [segment.text for segment in processor.poll(now=12.49)] == []
    assert [segment.text for segment in processor.poll(now=12.50)] == ["还需要"]


def test_streaming_mode_uses_boundary_wait_but_commits_decoded_text_immediately() -> None:
    processor = ContextualTextProcessor(
        source_lang="zh", defer_incomplete=False, hold_ms=1800,
    )
    assert processor.should_defer_endpoint("如果下周开始")
    segments = processor.submit("如果下周开始")
    assert [segment.text for segment in segments] == ["如果下周开始"]


def test_light_filler_cleanup_is_conservative_and_can_be_disabled() -> None:
    light = ContextualTextProcessor(source_lang="zh", defer_incomplete=False)
    segment = light.submit("嗯，今天开始讨论方案。")[0]
    assert segment.text == "今天开始讨论方案。"
    assert segment.fillers_removed == 1
    assert light.submit("嗯。")== []
    assert light.submit("啊，接下来讨论预算。")[0].text == "接下来讨论预算。"
    assert light.submit("这样处理可以吗啊？")[0].text == "这样处理可以吗啊？"

    disabled = ContextualTextProcessor(
        source_lang="zh", filler_mode="off", defer_incomplete=False,
    )
    assert disabled.submit("嗯，今天开始。")[0].text == "嗯，今天开始。"


def test_hotword_correction_allows_small_auditable_edit() -> None:
    processor = ContextualTextProcessor(
        source_lang="zh",
        hotwords="模型迁移",
        defer_incomplete=False,
    )
    segment = processor.submit("我们讨论磨鞋迁移。")[0]
    assert segment.text == "我们讨论模型迁移。"
    assert segment.corrections == (("磨鞋迁移", "模型迁移"),)
    assert segment.raw_text == "我们讨论磨鞋迁移。"


def test_repeated_recent_context_can_establish_a_canonical_term() -> None:
    processor = ContextualTextProcessor(source_lang="zh", defer_incomplete=False)
    processor.submit("今天讨论模型迁移。")
    processor.submit("接下来继续完善模型迁移。")
    segment = processor.submit("最后验证磨型迁移。")[0]
    assert segment.text == "最后验证模型迁移。"
    assert ("磨型迁移", "模型迁移") in segment.corrections


def test_preview_combines_pending_context_and_flush_preserves_raw_text() -> None:
    processor = ContextualTextProcessor(source_lang="zh")
    assert processor.submit("如果测试通过", now=0.0) == []
    assert processor.preview("我们就发布") == "如果测试通过我们就发布"
    segment = processor.flush()[0]
    assert segment.text == "如果测试通过"
    assert segment.raw_text == "如果测试通过"


def test_preview_applies_the_same_conservative_correction_and_filler_cleanup() -> None:
    processor = ContextualTextProcessor(
        source_lang="zh", hotwords="模型迁移", defer_incomplete=False
    )

    assert processor.preview("嗯，我们讨论磨鞋迁移。") == "我们讨论模型迁移。"


def test_all_caps_english_partial_is_sentence_cased_for_display_only() -> None:
    assert format_partial_for_display(
        "I THINK THIS IS EASIER TO READ. IT IS LIVE", "en"
    ) == "I think this is easier to read. It is live"
    assert format_partial_for_display("NASA uses GPT-4", "en") == "NASA uses GPT-4"
    assert format_partial_for_display("这是中文", "zh") == "这是中文"


def test_english_context_preview_formats_uppercase_decoder_tokens() -> None:
    processor = ContextualTextProcessor(
        source_lang="en",
        filler_mode="off",
        correction_enabled=False,
    )

    assert processor.preview("THIS IS A LIVE ENGLISH DRAFT") == (
        "This is a live english draft"
    )
