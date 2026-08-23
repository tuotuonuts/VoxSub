from __future__ import annotations

from voxsub.live_draft import DraftView, LiveDraftState


def test_live_draft_replaces_source_and_rejects_stale_translation() -> None:
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
    assert state.accept_translation(old_request, "Welcome") is None
    assert state.take_translation_request() is None

    now[0] = 10.7
    current_request = state.take_translation_request()
    assert current_request is not None
    assert state.accept_translation(current_request, "Welcome to use") == DraftView(
        "欢迎使用", "Welcome to use"
    )


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

