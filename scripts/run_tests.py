#!/usr/bin/env python
"""唯一的测试入口 —— 每次都在固定的隔离临时目录里跑。

为什么需要它：

  pytest 的 ``tmp_path`` 默认落在系统临时目录，本身没问题；问题出在有人
  手工传 ``--basetemp=xxx`` 时，`xxx` 会落在**仓库根**，而且名字各异。
  历史积累下来仓库根已经堆了 240+ 个 ``.pytest-*`` 目录（500MB+），
  谁都不敢删，也没人知道哪些还有用。

  统一入口把 basetemp 固定到 ``<repo>/.pytest-run``：gitignore 覆盖、
  每次运行前清空、跑完保留现场供排查。这样"临时目录"永远只有一个，
  且位置可预期。

用法：

    ./.venv/Scripts/python.exe scripts/run_tests.py            # 全量
    ./.venv/Scripts/python.exe scripts/run_tests.py tests/test_ipc_loop.py -q
    ./.venv/Scripts/python.exe scripts/run_tests.py -m integration

任何多余参数原样透传给 pytest。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASETEMP = REPO_ROOT / ".pytest-run"


def _python() -> str:
    """优先用仓库自带的 virtualenv，保证依赖版本一致。"""
    for candidate in (REPO_ROOT / ".venv" / "Scripts" / "python.exe",
                      REPO_ROOT / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _reset_basetemp() -> None:
    """清空上一次的临时目录。只碰这个固定路径，不扫别处。"""
    if BASETEMP.exists():
        shutil.rmtree(BASETEMP, ignore_errors=True)
    BASETEMP.mkdir(parents=True, exist_ok=True)


def main(argv: list[str]) -> int:
    _reset_basetemp()
    command = [_python(), "-m", "pytest", "--basetemp", str(BASETEMP), *argv]
    print("$ " + " ".join(command), flush=True)
    try:
        return subprocess.call(command, cwd=str(REPO_ROOT))
    except KeyboardInterrupt:  # pragma: no cover - 用户中断
        return 130


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
