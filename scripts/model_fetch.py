#!/usr/bin/env python
"""语幕 VoxSub - 模型下载/校验/清单工具 (M8 自愈机制地基)。

功能:
  scan  <models-dir>            扫描目录, 计算每个文件的 sha256/大小, 生成 manifest.json
  fetch <name> --url <url>     断点续传下载到 models-dir/<name> 并 sha256 校验
        [--sha256 <hash>]      (可选) 下载后强制校验
        [--mirror <url>]      (可选) 主 URL 失败时的备用源
        [--dest <name>]       目标相对路径(默认=name)

manifest.json 结构 (位于 models-dir 下):
  {"version": 1,
   "files": {"<相对路径>": {"size": int, "sha256": str, "mtime": str,
                             "url": str|null, "status": "ready"|"partial"|"missing"}}}

注意: 本机 Hermes 注入 PYTHONPATH, 用项目 venv 跑时前缀 `unset PYTHONPATH PYTHONHOME`。
仅用标准库 (urllib/hashlib/json), 无第三方依赖。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

MANIFEST_NAME = "manifest.json"
CHUNK = 1 << 20  # 1MB 分块读写


# ---------- manifest ----------

def _manifest_path(models_dir: Path) -> Path:
    return models_dir / MANIFEST_NAME


def load_manifest(models_dir: Path) -> dict:
    p = _manifest_path(models_dir)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"version": 1, "files": {}}
    return {"version": 1, "files": {}}


def save_manifest(models_dir: Path, manifest: dict) -> None:
    # 先写临时文件再原子替换, 防止下载/扫描中断损坏清单
    tmp = _manifest_path(models_dir).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_manifest_path(models_dir))


# ---------- scan ----------

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def cmd_scan(models_dir: Path) -> int:
    """扫描目录全部文件(跳过 manifest 本身), 生成/更新清单。"""
    models_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(models_dir)
    files = manifest.setdefault("files", {})
    total_bytes = 0

    for p in sorted(models_dir.rglob("*")):
        if not p.is_file() or p.name == MANIFEST_NAME or p.suffix == ".part":
            continue
        rel = p.relative_to(models_dir).as_posix()
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
        if not (models_dir / rel).exists():
            files[rel]["status"] = "missing"

    save_manifest(models_dir, manifest)
    n = sum(1 for e in files.values() if e.get("status") == "ready")
    print(f"scan 完成: {n} 个就绪文件, 共 {total_bytes / 1e6:.1f} MB, 清单: {_manifest_path(models_dir)}")
    return 0


# ---------- fetch (断点续传 + 校验) ----------

def fetch_file(url: str, dest: Path, expected_sha: str | None = None,
               mirror: str | None = None) -> bool:
    """下载单个文件(断点续传), sha256 校验; 主 URL 失败自动切镜像。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    sources = [u for u in (url, mirror) if u]

    for src in sources:
        try:
            existing = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={existing}-"} if existing else {}
            req = urlrequest.Request(src, headers=headers)
            with urlrequest.urlopen(req, timeout=60) as resp, part.open("ab") as out:
                while True:
                    block = resp.read(CHUNK)
                    if not block:
                        break
                    out.write(block)
            part.replace(dest)
            print(f"  下载完成: {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
            if expected_sha:
                actual = sha256_of(dest)
                if actual != expected_sha:
                    print(f"  [错误] SHA256 不匹配: 期望 {expected_sha}, 实际 {actual}")
                    return False
                print("  SHA256 校验通过")
            return True
        except (urlerror.URLError, OSError, TimeoutError) as exc:
            print(f"  源 {src} 失败: {exc}")
            # 失败原因若是 HTTP 416(Range 越界), 说明本地 .part 已完整, 去掉续传重来
            if isinstance(exc, urlerror.HTTPError) and exc.code == 416:
                part.unlink(missing_ok=True)
            continue
    print("  [错误] 所有源均失败")
    return False


def cmd_fetch(models_dir: Path, name: str, url: str, sha256: str | None,
              mirror: str | None, dest_rel: str | None) -> int:
    """下载模型文件并登记进 manifest。"""
    models_dir.mkdir(parents=True, exist_ok=True)
    rel = (dest_rel or name).replace("\\", "/")
    dest = models_dir / rel
    manifest = load_manifest(models_dir)
    files = manifest.setdefault("files", {})

    ok = fetch_file(url, dest, expected_sha=sha256, mirror=mirror)
    if not ok:
        files[rel] = {"size": dest.stat().st_size if dest.exists() else 0,
                      "sha256": sha256 or "", "mtime": time.strftime("%Y-%m-%dT%H:%M:%S"),
                      "url": url, "mirror": mirror, "status": "partial"}
        save_manifest(models_dir, manifest)
        return 1

    files[rel] = {"size": dest.stat().st_size, "sha256": sha256_of(dest),
                  "mtime": time.strftime("%Y-%m-%dT%H:%M:%S"),
                  "url": url, "mirror": mirror, "status": "ready"}
    save_manifest(models_dir, manifest)
    print(f"登记完成: {rel} -> {dest}")
    return 0


# ---------- CLI ----------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="model_fetch", description="语幕模型下载/校验/清单工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="扫描目录生成 manifest.json")
    p_scan.add_argument("--models-dir", default=os.environ.get("LOCALAPPDATA", ".") + "/VoxSub/models")

    p_fetch = sub.add_parser("fetch", help="断点续传下载模型并登记")
    p_fetch.add_argument("name")
    p_fetch.add_argument("--url", required=True)
    p_fetch.add_argument("--mirror", default=None)
    p_fetch.add_argument("--sha256", default=None)
    p_fetch.add_argument("--dest", default=None)
    p_fetch.add_argument("--models-dir", default=os.environ.get("LOCALAPPDATA", ".") + "/VoxSub/models")

    args = ap.parse_args(argv)
    models_dir = Path(args.models_dir)
    if args.cmd == "scan":
        return cmd_scan(models_dir)
    if args.cmd == "fetch":
        return cmd_fetch(models_dir, args.name, args.url, args.sha256, args.mirror, args.dest)
    return 2


if __name__ == "__main__":
    sys.exit(main())