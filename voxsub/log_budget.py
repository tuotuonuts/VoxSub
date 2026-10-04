"""Single-owner rolling log budget. Only voxsub.log[.N] is managed here."""
from __future__ import annotations
import logging.handlers
from pathlib import Path

DEFAULT_MB = 50
MIN_MB = 10
MAX_MB = 10240


class BudgetLogHandler(logging.handlers.RotatingFileHandler):
    def __init__(self, filename: str, limit_mb: int = DEFAULT_MB) -> None:
        if Path(filename).is_symlink():
            raise OSError("Refusing to write through a log symlink")
        self.limit_mb = max(MIN_MB, min(MAX_MB, int(limit_mb)))
        super().__init__(filename, maxBytes=self.limit_mb * 1024 * 1024 // 10,
                         backupCount=9, encoding="utf-8")

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record)[:16384]

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if self.stream is None:
            self.stream = self._open()
        self.stream.seek(0, 2)
        byte_count = len((self.format(record) + "\n").encode("utf-8", errors="replace"))
        return self.stream.tell() + byte_count >= self.maxBytes

    def set_budget(self, limit_mb: int) -> None:
        self.acquire()
        try:
            self.limit_mb = max(MIN_MB, min(MAX_MB, int(limit_mb)))
            self.maxBytes = self.limit_mb * 1024 * 1024 // 10
            self.trim()
        finally:
            self.release()

    def trim(self) -> None:
        """Oldest rotations first; never touch arbitrary logs or exported reports."""
        root = Path(self.baseFilename)
        if self.stream is not None:
            self.stream.flush()
        if root.is_file() and not root.is_symlink() and root.stat().st_size > self.maxBytes:
            self.doRollover()
        paths = [root] + [Path(str(root) + "." + str(i)) for i in range(1, 10)]
        sizes = {p: p.stat().st_size for p in paths if p.is_file() and not p.is_symlink()}
        total = sum(sizes.values())
        budget = self.limit_mb * 1024 * 1024
        for path in reversed(paths[1:]):
            if total <= budget:
                break
            if path in sizes:
                try:
                    path.unlink()
                    total -= sizes[path]
                except OSError:
                    continue
