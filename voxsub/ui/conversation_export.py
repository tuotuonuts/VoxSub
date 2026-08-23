"""Qt-independent conversation snapshot export."""
from __future__ import annotations

from pathlib import Path

from voxsub.file_io import write_text_atomically
from voxsub.subtitles import SubtitleExporter, SubtitleLine


def write_conversation_snapshot(
    conversation: tuple[tuple[str, str, int], ...],
    path: Path | str,
) -> Path:
    lines = [SubtitleLine(text=source, translation=translation, ts_ms=timestamp)
             for source, translation, timestamp in conversation]
    output = Path(path)
    suffix = output.suffix.lower()
    if suffix == ".srt":
        SubtitleExporter.write_srt(lines, output)
    elif suffix == ".vtt":
        SubtitleExporter.write_vtt(lines, output)
    else:
        if suffix != ".txt":
            output = output.with_suffix(".txt")
        write_text_atomically(
            output,
            "\n\n".join(f"{line.text}\n{line.translation}" for line in lines),
            encoding="utf-8",
        )
    return output


__all__ = ["write_conversation_snapshot"]
