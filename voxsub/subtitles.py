"""Subtitle data model and crash-safe export formats."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from voxsub.file_io import write_text_atomically


@dataclass
class SubtitleLine:
    """One source/translation pair with a relative timestamp."""

    text: str
    translation: str = ""
    ts_ms: int = 0
    is_final: bool = True


class SubtitleExporter:
    """Pure subtitle formatting separated from pipeline lifecycle concerns."""

    @staticmethod
    def format_timestamp(ms: int) -> str:
        hours, remainder = divmod(ms, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, milliseconds = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

    @classmethod
    def write_srt(
        cls,
        lines: list[SubtitleLine],
        output: Path | str,
        duration_ms: int = 1500,
    ) -> None:
        body: list[str] = []
        for index, line in enumerate(lines, start=1):
            end = line.ts_ms + (3000 if index == len(lines) else duration_ms)
            body.append(
                f"{index}\n{cls.format_timestamp(line.ts_ms)} --> "
                f"{cls.format_timestamp(end)}\n{line.text}\n{line.translation}\n"
            )
        write_text_atomically(output, "\n".join(body), encoding="utf-8-sig")

    @classmethod
    def write_vtt(cls, lines: list[SubtitleLine], output: Path | str) -> None:
        body = ["WEBVTT\n"]
        for line in lines:
            start = cls.format_timestamp(line.ts_ms).replace(",", ".")
            end = cls.format_timestamp(line.ts_ms + 3000).replace(",", ".")
            body.append(f"{start} --> {end}\n{line.text}\n{line.translation}\n")
        write_text_atomically(output, "\n".join(body), encoding="utf-8")

    @staticmethod
    def write_txt(lines: list[SubtitleLine], output: Path | str) -> None:
        write_text_atomically(
            output,
            "\n".join(f"{line.text}\t{line.translation}" for line in lines),
            encoding="utf-8",
        )


__all__ = ["SubtitleExporter", "SubtitleLine"]
