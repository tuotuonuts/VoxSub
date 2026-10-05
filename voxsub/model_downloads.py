"""Durable model download sessions; file paths come only from the trusted catalog.

A prepared request is persisted before it is queued. No worker is started on scan:
records left queued/running by a previous process are reported as paused. Control
requests use this coordinator's lock/event, not the slow job queue.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from voxsub.downloader import DownloadCancelled
from voxsub.file_io import write_text_atomically

ACTIVE = {"queued", "downloading", "pausing", "verifying"}


class ModelDownloads:
    def __init__(self, marketplace: Any, emit: Callable[[dict], None] | None = None):
        self.market = marketplace
        self.root = marketplace.models_dir.resolve()
        self.folder = self.root / ".downloads"
        self.emit = emit or (lambda state: None)
        self.lock = threading.RLock()
        self.states: dict[str, dict] = {}
        self.workers: dict[str, threading.Event] = {}
        self.last_report: dict[str, float] = {}

    def _path(self, relative: str) -> Path:
        lexical = self.folder / relative
        path = lexical.resolve()
        if path != lexical or self.folder.resolve() != self.folder:
            raise ValueError("拒绝访问下载目录中的链接或越界路径")
        if self.root not in path.parents or self.folder not in path.parents:
            raise ValueError("拒绝访问下载目录之外的路径")
        return path

    def _record_path(self, model: Any) -> Path:
        return self._path(f".states/{model.id}.json")

    def _assets(self, model: Any) -> list[Path]:
        paths = []
        if model.asset_name:
            paths.append(self._path(model.asset_name))
        names = {item.install_rel for source in model.sources for item in source.files}
        paths.extend(self._path(f"{model.id}/{name}") for name in names)
        for path in paths:
            rel = str(path.relative_to(self.folder))
            self._path(rel + ".part")
            self._path(rel + ".part.resume.json")
        return paths

    def _bytes(self, model: Any) -> int:
        total = 0
        for path in self._assets(model):
            part = path.with_name(path.name + ".part")
            candidate = path if path.is_file() else part
            if candidate.is_file():
                total += candidate.stat().st_size
        return min(total, model.download_bytes) if model.download_bytes else total

    def _read(self, model: Any) -> dict | None:
        try:
            path = self._record_path(model)
            if path.stat().st_size > 128 * 1024:
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("modelId") != model.id or data.get("status") not in ACTIVE | {"paused", "done", "deleted"}:
                return None
            if type(data.get("revision")) is not int or not 0 <= data["revision"] < 2**53:
                return None
            if not isinstance(data.get("token"), str) or data.get("source") not in {"auto", "global", "china"}:
                return None
            if not isinstance(data.get("error", ""), str):
                return None
            return data
        except (OSError, ValueError, AttributeError, TypeError):
            return None

    def _base(self, model: Any) -> dict:
        return {"modelId": model.id, "modelsRoot": str(self.root), "token": uuid.uuid4().hex,
                "revision": 0, "completed": 0, "total": model.download_bytes,
                "status": "paused", "stage": "已暂停", "source": "auto", "error": ""}

    def _load(self, model: Any) -> dict | None:
        if model.id in self.states:
            return self.states[model.id]
        data = self._read(model)
        size = self._bytes(model)
        if data is None and not size:
            return None
        state = self._base(model)
        if data:
            for key in ("token", "revision", "source", "status", "error"):
                state[key] = data.get(key, state[key])
        state["completed"] = size
        if state["status"] in ACTIVE:
            state.update(status="paused", stage="已暂停", error="")
        self.states[model.id] = state
        return state

    def snapshot(self, model: Any) -> dict | None:
        with self.lock:
            state = self._load(model)
            if not state or state["status"] in {"done", "deleted"}:
                return None
            return dict(state)

    def _save(self, model: Any, state: dict) -> dict:
        state["revision"] = int(state["revision"]) + 1
        path = self._record_path(model)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomically(path, json.dumps(state, ensure_ascii=False), encoding="utf-8")
        snapshot = dict(state)
        self.emit(snapshot)
        return snapshot

    def prepare(self, model: Any, source: str) -> dict:
        if source not in {"auto", "global", "china"}:
            raise ValueError("无效下载源，须为 auto / global / china")
        with self.lock:
            old = self._load(model)
            if model.id in self.workers or (old and old["status"] in ACTIVE):
                raise RuntimeError("该模型的下载仍在进行，请先等待暂停完成")
            state = self._base(model)
            if old:
                state["revision"] = old["revision"]
            state.update(completed=self._bytes(model), source=source, status="queued", stage="等待下载")
            self.states[model.id] = state
            return self._save(model, state)

    def pause(self, model: Any, token: str) -> dict | None:
        with self.lock:
            state = self._load(model)
            if not state or state["token"] != token:
                raise RuntimeError("下载任务已变化，请刷新模型列表")
            if state["status"] in {"done", "deleted"}:
                return None
            event = self.workers.get(model.id)
            if event:
                event.set()
            state.update(status="pausing" if event else "paused",
                         stage="正在暂停" if event else "已暂停")
            return self._save(model, state)

    def _report(self, model: Any, token: str, done: int, total: int, stage: str) -> None:
        with self.lock:
            state = self.states[model.id]
            if state["token"] != token:
                return
            state.update(completed=done, total=total, stage=stage)
            if stage == "校验并安装" and state["status"] != "pausing":
                state["status"] = "verifying"
            if state["status"] == "pausing":
                state["stage"] = "正在暂停"
            now = time.monotonic()
            if now - self.last_report.get(model.id, 0) >= .25:
                self.last_report[model.id] = now
                self._save(model, state)

    def run(self, model: Any, token: str, cancelled: Callable[[], bool]) -> dict | None:
        with self.lock:
            state = self._load(model)
            if not state or state["token"] != token or state["status"] != "queued":
                return None
            event = threading.Event()
            self.workers[model.id] = event
            state.update(status="downloading", stage="正在下载")
            self._save(model, state)
        try:
            self.market.install(model, preference=state["source"],
                                progress=lambda d, t, s: self._report(model, token, d, t, s),
                                cancelled=lambda: event.is_set() or cancelled())
        except DownloadCancelled:
            return self._finish(model, "paused", "已暂停")
        except Exception as exc:
            # A failed connection keeps resumable assets instead of removing the task.
            self._finish(model, "paused", "已暂停", str(exc))
            raise
        else:
            return self._finish(model, "done", "下载完成")
        finally:
            with self.lock:
                if self.workers.get(model.id) is event:
                    self.workers.pop(model.id, None)

    def _finish(self, model: Any, status: str, stage: str, error: str = "") -> dict:
        with self.lock:
            state = self.states[model.id]
            self.workers.pop(model.id, None)
            state.update(status=status, stage=stage, error=error[:2000])
            if status == "done":
                state["completed"] = state["total"]
            else:
                state["completed"] = self._bytes(model)
            return self._save(model, state)

    def delete(self, model: Any, token: str) -> dict:
        with self.lock:
            state = self._load(model)
            if not state or state["token"] != token or state["status"] != "paused":
                raise RuntimeError("只有已暂停的下载才能删除，请等待暂停完成")
            if model.id in self.workers:
                raise RuntimeError("下载正在收尾，请稍后重试")
            self._delete_installing(model)
            self._delete_assets(model)
            state.update(status="deleted", stage="", completed=0, error="")
            return self._save(model, state)

    def _delete_assets(self, model: Any) -> None:
        # Never touch model_dir/installed files, or paths obtained from session JSON.
        assets = self._assets(model)
        for path in assets:
            for suffix in ("", ".part", ".part.resume.json"):
                self._path(str(path.relative_to(self.folder)) + suffix).unlink(missing_ok=True)
        directory = self._path(model.id)
        if directory.exists():
            shutil.rmtree(directory)

    def _delete_installing(self, model: Any) -> None:
        parent = self.root / ".installing"
        path = parent / model.id
        if parent.resolve() != parent or path.resolve() != path or parent not in path.parents:
            raise ValueError("拒绝删除越界的安装暂存目录")
        if path.exists():
            shutil.rmtree(path)
