from __future__ import annotations

import logging

from voxsub.file_transcriber import FileRecognizer
from voxsub.subtitles import SubtitleLine


class _BrokenTranslator:
    def translate(self, *_args, **_kwargs):
        raise RuntimeError("backend unavailable")


def test_translation_failure_logs_segment_without_subtitle_text(caplog) -> None:
    secret_text = "用户私密字幕内容"
    lines = [SubtitleLine(secret_text)]
    with caplog.at_level(logging.WARNING, logger="voxsub.file_transcriber"):
        FileRecognizer._translate(
            lines, _BrokenTranslator(), "ja", "zh", validate_translation=False,
        )
    assert lines[0].translation == ""
    assert "segment=1" in caplog.text
    assert "RuntimeError" in caplog.text
    assert secret_text not in caplog.text


def test_translation_failure_does_not_emit_source_as_destination(caplog) -> None:
    lines = [SubtitleLine("字幕原文")]
    with caplog.at_level(logging.WARNING, logger="voxsub.file_transcriber"):
        FileRecognizer._translate(
            lines, _BrokenTranslator(), "ja", "zh", validate_translation=False,
        )
    assert lines[0].translation == ""
    assert "字幕原文" not in caplog.text
