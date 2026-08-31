from __future__ import annotations

from voxsub.asr import _clean_asr_result
from voxsub.text_cleaning import strip_model_control_tokens


def test_control_tokens_cut_decoder_spillover() -> None:
    assert strip_model_control_tokens(
        "创造发明，实现，而这些。<|endoftext|>Humanity."
    ) == "创造发明，实现，而这些。"


def test_inline_control_tokens_are_removed() -> None:
    assert strip_model_control_tokens(
        "<|assistant|> Hello <|im_end|>"
    ) == "Hello"


def test_plain_text_is_preserved() -> None:
    assert strip_model_control_tokens("  hello   world  ") == "hello world"


def test_asr_cleanup_does_not_log_source_text(caplog) -> None:
    with caplog.at_level("WARNING", logger="voxsub.asr"):
        assert _clean_asr_result("hello<|endoftext|>spill") == "hello"
    assert "spill" not in caplog.text
    assert "raw_chars" in caplog.text
