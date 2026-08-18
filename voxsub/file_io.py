"""Small, crash-safe file writers shared by exports and subtitle generation."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_text_atomically(path: Path | str, text: str, *, encoding: str = "utf-8") -> Path:
    """Write text beside its destination, then atomically replace the old file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            newline="",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".part",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        return destination
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
