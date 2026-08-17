"""voxsub.models —— 模型下载/校验/清单管理器 (M8)。

把 scripts/model_fetch.py 的 fetch_file / load_manifest / save_manifest /
sha256_of 提炼进包内, 以 :class:`ModelManager` 统一暴露:

    mgr = ModelManager(models_dir)
    mgr.scan()                                   # 扫描目录生成/更新 manifest.json
    mgr.fetch("asr/tokens.txt", url, sha256=...) # 断点续传下载 + sha256 校验
    mgr.verify_all()                             # 逐条比对磁盘 (存在 + 大小)
    mgr.get_missing()                            # 缺失/损坏的相对路径列表

manifest.json 结构 (位于 models-dir 下):
    {"version": 1,
     "files": {"<相对路径>": {"size": int, "sha256": str, "mtime": str,
                               "url": str|null, "status": "ready"|"partial"|"missing"}}}

scripts/model_fetch.py 保留为薄 CLI 壳, 直接调用本模块 (不重复代码)。
仅用标准库 (urllib/hashlib/json), 无第三方依赖。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib import error as urlerror
from urllib import request as urlrequest

from voxsub.logging_setup import get_logger

logger = get_logger("models")

MANIFEST_NAME = "manifest.json"
CHUNK = 1 << 20  # 1MB 分块读写


class DownloadCancelled(RuntimeError):
    """Raised when a marketplace download is cancelled by the user."""


def _is_internal_artifact(rel: str) -> bool:
    """返回是否为模型管理器自己的锁/临时文件，而非模型资产。"""
    name = Path(rel).name
    return name in {MANIFEST_NAME, ".fetch.lock", f"{MANIFEST_NAME}.tmp"} or name.endswith(".part")


# ---------------------------------------------------------------------------
# 底层函数 (scripts/model_fetch.py 旧实现整体迁移, 供 CLI 壳与 ModelManager 共用)
# ---------------------------------------------------------------------------

def _manifest_path(models_dir: Path) -> Path:
    return models_dir / MANIFEST_NAME


def load_manifest(models_dir: Path | str) -> dict:
    """读取 manifest; 不存在或损坏时返回空清单 (不抛异常)。"""
    p = _manifest_path(Path(models_dir))
    if p.exists():
        try:
            manifest = json.loads(p.read_text(encoding="utf-8"))
            files = manifest.get("files")
            if isinstance(files, dict):
                # 旧版 scan 曾把正在使用的 .fetch.lock 登记成模型文件。读取时
                # 过滤内部产物，避免诊断误报，也避免测试/修复流程触碰锁文件。
                for rel in list(files):
                    if _is_internal_artifact(rel):
                        files.pop(rel, None)
            return manifest
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("manifest 读取/解析失败 (%s), 回退空清单: %s", p.name, exc)
            return {"version": 1, "files": {}}
    return {"version": 1, "files": {}}


def save_manifest(models_dir: Path | str, manifest: dict) -> None:
    """原子写回 manifest (先写 .tmp 再 replace, 防中断损坏清单)。"""
    p = _manifest_path(Path(models_dir))
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def sha256_of(path: Path | str) -> str:
    """计算文件 sha256 (1MB 分块, 内存友好)。"""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while True:
            block = f.read(CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def fetch_file(url: str, dest: Path | str, expected_sha: str | None = None,
               mirror: str | None = None, *,
               mirrors: Iterable[str] | None = None,
               progress: Callable[[int, int, str], None] | None = None,
               cancelled: Callable[[], bool] | None = None) -> bool:
    """下载单个文件(断点续传), sha256 校验; 主 URL 失败自动切镜像。

    续传细节:
    - 已有 ``<name>.part`` 时带 ``Range: bytes=<已有大小>-`` 请求;
    - 服务器忽略 Range 返回 200 全量时, 必须清空重写 (否则与旧 .part
      重复拼接损坏文件) —— 由 ``resp.status == 206`` 分支决定写模式;
    - HTTP 416 (Range 越界, 本地 .part 已完整) 时删掉 .part 全量重下。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    sources = [url]
    if mirror:
        sources.append(mirror)
    if mirrors:
        sources.extend(u for u in mirrors if u)
    # 保留顺序去重，自动测速可能把同一源作为回退重复传入。
    sources = list(dict.fromkeys(sources))
    logger.info("开始下载 %s (候选源 %d 个)", dest.name, len(sources))

    for src in sources:
        try:
            if cancelled and cancelled():
                raise DownloadCancelled("下载已取消")
            existing = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={existing}-"} if existing else {}
            headers["User-Agent"] = "VoxSub/0.3"
            req = urlrequest.Request(src, headers=headers)
            with urlrequest.urlopen(req, timeout=60) as resp, \
                    part.open("ab" if resp.status == 206 else "wb") as out:
                resumed = existing if resp.status == 206 else 0
                total = 0
                content_range = resp.headers.get("Content-Range", "")
                if "/" in content_range:
                    try:
                        total = int(content_range.rsplit("/", 1)[1])
                    except ValueError:
                        total = 0
                if not total:
                    try:
                        total = resumed + int(resp.headers.get("Content-Length", "0"))
                    except ValueError:
                        total = 0
                written = resumed
                if progress:
                    progress(written, total, src)
                while True:
                    if cancelled and cancelled():
                        raise DownloadCancelled("下载已取消")
                    block = resp.read(CHUNK)
                    if not block:
                        break
                    out.write(block)
                    written += len(block)
                    if progress:
                        progress(written, total, src)
            part.replace(dest)
            size_mb = dest.stat().st_size / 1e6
            print(f"  下载完成: {dest.name} ({size_mb:.1f} MB)")
            logger.info("下载完成: %s (%.1f MB)", dest.name, size_mb)
            if expected_sha:
                actual = sha256_of(dest)
                if actual != expected_sha:
                    print(f"  [错误] SHA256 不匹配: 期望 {expected_sha}, 实际 {actual}")
                    logger.error("SHA256 校验失败: %s 期望=%s 实际=%s",
                                 dest.name, expected_sha, actual)
                    return False
                print("  SHA256 校验通过")
            return True
        except DownloadCancelled:
            logger.info("下载已取消，保留断点文件: %s", part)
            raise
        except (urlerror.URLError, OSError, TimeoutError) as exc:
            print(f"  源 {src} 失败: {exc}")
            logger.warning("下载源失败: 目标=%s 源=%s 错误=%s", dest.name, src, exc)
            # 失败原因若是 HTTP 416(Range 越界), 说明本地 .part 已完整, 去掉续传重来
            if isinstance(exc, urlerror.HTTPError) and exc.code == 416:
                part.unlink(missing_ok=True)
            continue
    print("  [错误] 所有源均失败")
    logger.error("所有下载源均失败: %s", dest.name)
    return False


# ---------------------------------------------------------------------------
# ModelManager
# ---------------------------------------------------------------------------

class ModelManager:
    """模型清单管理器: 扫描 / 断点续传下载 / 完整性校验。"""

    def __init__(self, models_dir: Path | str):
        self.models_dir = Path(models_dir)

    # -- manifest 基础 -------------------------------------------------------

    @property
    def manifest_path(self) -> Path:
        return _manifest_path(self.models_dir)

    def load_manifest(self) -> dict:
        return load_manifest(self.models_dir)

    def save_manifest(self, manifest: dict) -> None:
        save_manifest(self.models_dir, manifest)

    # -- scan ----------------------------------------------------------------

    def scan(self) -> dict:
        """扫描目录全部文件(跳过 manifest 与 *.part), 生成/更新清单。

        Returns:
            {"ready": int, "total_bytes": int} 摘要。
        """
        self.models_dir.mkdir(parents=True, exist_ok=True)
        manifest = self.load_manifest()
        files = manifest.setdefault("files", {})
        total_bytes = 0

        for p in sorted(self.models_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(self.models_dir).as_posix()
            if _is_internal_artifact(rel):
                continue
            size = p.stat().st_size
            total_bytes += size
            entry = files.get(rel, {})
            if entry.get("size") != size:  # 大小变化视为新文件, 重算哈希
                sha = sha256_of(p)
                entry.update({"size": size, "sha256": sha,
                              "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())})
                entry["status"] = "ready"
                files[rel] = entry
            else:  # 大小相同但缺哈希(如手动拷贝), 补算
                entry.setdefault("sha256", sha256_of(p))
                entry.setdefault("mtime", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()))
                entry["status"] = "ready"
                files[rel] = entry

        # 从清单移除磁盘上已消失的文件, 标记为 missing(不删除记录, 便于排查)
        for rel in list(files):
            if not (self.models_dir / rel).exists():
                files[rel]["status"] = "missing"

        self.save_manifest(manifest)
        n = sum(1 for e in files.values() if e.get("status") == "ready")
        return {"ready": n, "total_bytes": total_bytes}

    # -- fetch ---------------------------------------------------------------

    def fetch(self, rel: str, url: str, sha256: str | None = None,
              mirror: str | None = None) -> bool:
        """下载模型文件并登记进 manifest (断点续传 + sha256 校验)。

        Args:
            rel: 目标相对路径 (如 "asr/tokens.txt"), 自动转 posix 分隔符。
            url: 主下载源。
            sha256: 期望哈希; 提供则下载后强制校验, 不匹配返回 False。
            mirror: 主源失败时的备用源。

        Returns:
            True = 下载+登记成功 (manifest 条目 status="ready")。

        并发安全: 获取 models/.fetch.lock 互斥锁后再下载。2026-08-17 实测踩坑:
        两个进程并发 append 同一 .part 导致文件大小超限损坏 —— 多实例(大众用户
        开两个窗口)必触发, 故锁为硬要求。非 Windows 平台降级为无锁。
        """
        self.models_dir.mkdir(parents=True, exist_ok=True)
        rel = rel.replace("\\", "/")
        dest = self.models_dir / rel

        try:
            import msvcrt
        except ImportError:  # pragma: no cover - 非 Windows
            msvcrt = None  # type: ignore[assignment]

        lock_fh = open(self.models_dir / ".fetch.lock", "w")
        locked = False
        if msvcrt is not None:
            try:
                msvcrt.locking(lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError:
                lock_fh.close()
                logger.warning("获取模型下载锁失败 (.fetch.lock 被占用), "
                               "判定另一下载进行中")
                raise RuntimeError("另一个模型下载正在进行中，请稍后重试") from None

        try:
            manifest = self.load_manifest()
            files = manifest.setdefault("files", {})

            ok = fetch_file(url, dest, expected_sha=sha256, mirror=mirror)
            if not ok:
                logger.warning("下载失败, manifest 登记为 partial: %s", rel)
                files[rel] = {"size": dest.stat().st_size if dest.exists() else 0,
                              "sha256": sha256 or "",
                              "mtime": time.strftime("%Y-%m-%dT%H:%M:%S"),
                              "url": url, "mirror": mirror, "status": "partial"}
                self.save_manifest(manifest)
                return False

            files[rel] = {"size": dest.stat().st_size, "sha256": sha256_of(dest),
                          "mtime": time.strftime("%Y-%m-%dT%H:%M:%S"),
                          "url": url, "mirror": mirror, "status": "ready"}
            self.save_manifest(manifest)
            print(f"登记完成: {rel} -> {dest}")
            logger.info("模型登记完成: %s (%.1f MB)", rel, dest.stat().st_size / 1e6)
            return True
        finally:
            if locked:
                try:
                    msvcrt.locking(lock_fh.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[union-attr]
                except OSError:
                    pass
            lock_fh.close()

    # -- verify --------------------------------------------------------------

    def verify_all(self, check_hashes: bool = False) -> list[dict]:
        """逐条校验 manifest 登记的文件与磁盘是否一致。

        Args:
            check_hashes: True 时额外比对 sha256 (慢, 全量哈希); False 只比对
                存在性 + 大小 (诊断页默认, 秒级完成)。

        Returns:
            有问题的条目列表, 每项 {rel, status, reason}; 全部正常时为空列表。
        """
        problems: list[dict[str, Any]] = []
        files = self.load_manifest().get("files", {})
        for rel, entry in sorted(files.items()):
            path = self.models_dir / rel
            if entry.get("status") != "ready":
                problems.append({"rel": rel, "status": entry.get("status"),
                                 "reason": f"清单状态非 ready: {entry.get('status')}"})
                continue
            if not path.exists():
                problems.append({"rel": rel, "status": "missing",
                                 "reason": "磁盘上文件不存在"})
                continue
            size = path.stat().st_size
            if size != entry.get("size"):
                problems.append({"rel": rel, "status": "corrupt",
                                 "reason": f"大小不一致: 清单 {entry.get('size')} vs 磁盘 {size}"})
                continue
            if check_hashes and sha256_of(path) != entry.get("sha256"):
                problems.append({"rel": rel, "status": "corrupt",
                                 "reason": "sha256 不匹配"})
        return problems

    def get_missing(self) -> list[str]:
        """缺失/损坏文件的相对路径列表 (verify_all 的轻量封装)。"""
        return [p["rel"] for p in self.verify_all()]
