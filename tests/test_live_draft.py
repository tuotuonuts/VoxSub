from __future__ import annotations

from voxsub.live_draft import DraftView, LiveDraftState


def test_live_draft_coalesces_updates_and_keeps_safe_lagging_translation() -> None:
    now = [10.0]
    state = LiveDraftState(
        debounce_seconds=0.2,
        min_interval_seconds=0.5,
        clock=lambda: now[0],
    )

    assert state.update_source("欢迎") == DraftView("欢迎")
    assert state.take_translation_request() is None
    now[0] = 10.2
    old_request = state.take_translation_request()
    assert old_request is not None

    now[0] = 10.25
    assert state.update_source("欢迎使用") == DraftView("欢迎使用")
    assert state.accept_translation(old_request, "Welcome") == DraftView(
        "欢迎使用", "Welcome"
    )
    assert state.take_translation_request() is None

    now[0] = 10.7
    current_request = state.take_translation_request()
    assert current_request is not None
    assert state.accept_translation(current_request, "Welcome to use") == DraftView(
        "欢迎使用", "Welcome to use"
    )


def test_continuous_partial_updates_do_not_starve_translation_debounce() -> None:
    now = [0.0]
    state = LiveDraftState(
        debounce_seconds=0.18,
        min_interval_seconds=0.45,
        clock=lambda: now[0],
    )

    assert state.update_source("This") == DraftView("This")
    for timestamp, source in (
        (0.10, "This is"),
        (0.14, "This is a"),
        (0.17, "This is a live"),
    ):
        now[0] = timestamp
        state.update_source(source)
        assert state.take_translation_request() is None

    now[0] = 0.18
    request = state.take_translation_request()
    assert request is not None
    assert request.source == "This is a live"


def test_incompatible_stale_translation_is_rejected() -> None:
    now = [0.0]
    state = LiveDraftState(
        debounce_seconds=0.0,
        min_interval_seconds=0.0,
        clock=lambda: now[0],
    )

    state.update_source("Hello world")
    old_request = state.take_translation_request()
    assert old_request is not None
    state.update_source("Goodbye everyone")

    assert state.accept_translation(old_request, "你好，世界") is None


def test_progressive_update_keeps_the_last_accepted_translation_visible() -> None:
    now = [0.0]
    state = LiveDraftState(
        debounce_seconds=0.0,
        min_interval_seconds=0.0,
        clock=lambda: now[0],
    )

    state.update_source("This is")
    request = state.take_translation_request()
    assert request is not None
    assert state.accept_translation(request, "这是") == DraftView("This is", "这是")

    assert state.update_source("This is live") == DraftView("This is live", "这是")


def test_final_translation_blocks_preview_work_but_not_next_sentence_visuals() -> None:
    now = [0.0]
    state = LiveDraftState(
        debounce_seconds=0.0,
        min_interval_seconds=0.0,
        clock=lambda: now[0],
    )
    assert state.update_source("第一句") == DraftView("第一句")
    state.begin_final()

    # Recognition of the next sentence remains visible while the prior final
    # translation has priority, but it cannot start a competing preview call.
    assert state.update_source("下一") == DraftView("下一")
    assert state.take_translation_request() is None
    assert state.update_source("下一句话") == DraftView("下一句话")
    assert state.finish_final() == DraftView("下一句话")

    request = state.take_translation_request()
    assert request is not None
    assert state.accept_translation(request, "Next sentence") == DraftView(
        "下一句话", "Next sentence"
    )
