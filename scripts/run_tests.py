#!/usr/bin/env python
"""唯一的测试入口 —— 每次都在**自己的**隔离临时目录里跑。

为什么需要它：

  pytest 的 ``tmp_path`` 默认落在系统临时目录，本身没问题；问题出在有人
  手工传 ``--basetemp=xxx`` 时，`xxx` 会落在**仓库根**，而且名字各异。
  历史积累下来仓库根已经堆了 240+ 个 ``.pytest-*`` 目录（500MB+），
  谁都不敢删，也没人知道哪些还有用。

  统一入口把 basetemp 收到 ``<repo>/.pytest-run/<本次运行 id>``：gitignore
  覆盖、每次运行清掉自己的、跑完保留现场供排查。

**为什么要带运行 id，而不是一个固定的 `.pytest-run`：**

  固定路径在**并发跑两次**时会互相踩 —— 后启动的那次会 rmtree 掉前一次正在
  用的 tmp_path，表现是一批与代码无关的 fixture 报错。实测踩过一次：某次全量
  里 ``test_apply_saved_config.py`` 冒出 14 个 ERROR，单独跑、换顺序跑都是绿的；
  根因就是当时另有一个后台进程在跑同一套。所以每次运行用独立的子目录。

  陈旧的子目录在每次启动时清理：**只动本根的直接子目录**、且只清超过
  STALE_HOURS 没动过的（正在跑的 mtime 是新的，不会被误删）。

用法：

    ./.venv/Scripts/python.exe scripts/run_tests.py            # 全量
    ./.venv/Scripts/python.exe scripts/run_tests.py tests/test_ipc_loop.py -q
    ./.venv/Scripts/python.exe scripts/run_tests.py -m integration

任何多余参数原样透传给 pytest。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 所有运行共用的根。gitignore 覆盖它；每个 run 在里面占一个自己的子目录。
BASETEMP_ROOT = REPO_ROOT / ".pytest-run"

#: 多久没动过的子目录算陈旧、可以清掉。全量跑约 45 秒，2 小时足够宽松。
STALE_HOURS = 2


def _python() -> str:
    """优先用仓库自带的 virtualenv，保证依赖版本一致。"""
    for candidate in (REPO_ROOT / ".venv" / "Scripts" / "python.exe",
                      REPO_ROOT / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _own_basetemp() -> Path:
    """本次运行专属的 basetemp。

    用 pid + 启动时间做标识：pid 防同机并发，时间戳防 pid 被回收后撞上前一次的残留。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return BASETEMP_ROOT / f"run-{os.getpid()}-{stamp}"


def _prune_stale_runs() -> list[str]:
    """清掉本根下陈旧的子目录，返回被清掉的名单（供日志）。

    只动 ``BASETEMP_ROOT`` 的**直接**子目录，不递归扫别处；mtime 新的一律留着
    （那可能是另一个正在跑的 run）。

    为什么连非 ``run-*`` 的也清：这个根在改成"每次一个子目录"之前，本身就是
    basetemp 本身 —— 于是历史运行的 ``test_*`` 目录（pytest 的 per-test tmp）
    直接躺在这一层。只清 ``run-*`` 的话，这些会永久留着，等于换个地方重新堆积。
    """
    removed: list[str] = []
    if not BASETEMP_ROOT.is_dir():
        return removed
    cutoff = time.time() - STALE_HOURS * 3600
    for entry in BASETEMP_ROOT.iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
        except OSError:
            continue
    return removed


def main(argv: list[str]) -> int:
    BASETEMP_ROOT.mkdir(parents=True, exist_ok=True)
    pruned = _prune_stale_runs()
    if pruned:
        print(f"[run_tests] 清掉 {len(pruned)} 个陈旧运行目录（>{STALE_HOURS}h 未动）")

    basetemp = _own_basetemp()
    basetemp.mkdir(parents=True, exist_ok=True)
    command = [_python(), "-m", "pytest", "--basetemp", str(basetemp), *argv]
    print("$ " + " ".join(command), flush=True)
    try:
        return subprocess.call(command, cwd=str(REPO_ROOT))
    except KeyboardInterrupt:  # pragma: no cover - 用户中断
        return 130


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
