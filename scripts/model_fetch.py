#!/usr/bin/env python
"""语幕 VoxSub - 模型下载/校验/清单工具 CLI 壳 (M8)。

实现已迁移到 voxsub.models.ModelManager, 本文件只保留参数解析与调用,
不重复任何下载/校验逻辑。用法不变:

  scan  <models-dir>         扫描目录生成 manifest.json
  fetch <name> --url <url>   断点续传下载到 models-dir/<name> 并 sha256 校验
        [--sha256 <hash>]    (可选) 下载后强制校验
        [--mirror <url>]     (可选) 主 URL 失败时的备用源
        [--dest <name>]      目标相对路径(默认=name)

注意: 本机 Hermes 注入 PYTHONPATH, 用项目 venv 跑时前缀 `unset PYTHONPATH PYTHONHOME`。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 允许 scripts/ 下的脚本直接 import 项目根包 (voxsub)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voxsub.models import ModelManager  # noqa: E402


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
    mgr = ModelManager(Path(args.models_dir))
    if args.cmd == "scan":
        summary = mgr.scan()
        print(f"scan 完成: {summary['ready']} 个就绪文件, "
              f"共 {summary['total_bytes'] / 1e6:.1f} MB, 清单: {mgr.manifest_path}")
        return 0
    if args.cmd == "fetch":
        # ModelManager.fetch(rel, url, sha256, mirror) 无 dest 参数:
        # 目标路径即 rel, --dest 为兼容旧 CLI 的同义词
        rel = args.dest or args.name
        return 0 if mgr.fetch(rel, args.url, args.sha256, args.mirror) else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
