#!/usr/bin/env python
"""发布构建 —— 一条命令产出可安装的 Windows 安装包。

流程（任一步失败即中断，不留半成品）：

    1. 环境检查      venv / node_modules / 二进制是否就位
    2. 类型与契约     tsc --noEmit + 设计令牌契约
    3. 前端构建       esbuild（含零宽字符清洗）
    4. Python 测试    全量 pytest（发布门禁）
    5. sidecar 打包   PyInstaller（excludes 掉 Qt）
    6. sidecar 验收   独立跑 IPC 握手 + 关键命令，确认不带 venv 也能工作
    7. 安装包         electron-builder → Release/electron/
    8. 校验产物       安装包存在 + 计算 SHA256

为什么第 6 步不能省：sidecar 是"打包成功但功能缺失"最容易发生的环节 ——
PyInstaller 的 hiddenimports 漏一个，运行到某个命令才炸，而那时已经装到
用户机器上了。所以打完必须**脱离 venv** 真跑一遍。

用法：
    python frontend/tools/build-release.py                  完整流程
    python frontend/tools/build-release.py --skip-tests     跳过 pytest（快速迭代用）
    python frontend/tools/build-release.py --dir-only       只出免安装目录（不生成安装包）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRONTEND = HERE.parent
REPO = FRONTEND.parent
VENV_PY = REPO / ".venv" / "Scripts" / "python.exe"
RELEASE_DIR = REPO.parent / "Release" / "electron"
BACKEND_DIST = FRONTEND / "backend" / "dist" / "VoxSubBackend"
SIDECAR = BACKEND_DIST / "VoxSubBackend.exe"

passed = 0
failed = 0


def step(n: int, text: str) -> None:
    print(f"\n{'=' * 70}")
    print(f"[{n}] {text}")
    print("=" * 70)


def ok(text: str) -> None:
    global passed
    passed += 1
    print(f"    OK   {text}")


def fail(text: str) -> None:
    global failed
    failed += 1
    print(f"    FAIL {text}")


def die(text: str) -> None:
    print(f"\n构建中断：{text}")
    sys.exit(1)


def run(cmd: list[str], cwd: Path, *, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    """运行外部命令。

    Windows 上 npm/npx/electron-builder 都是 .cmd 脚本，不带 shell=True 时
    subprocess 找不到它们（报 WinError 2「系统找不到指定的文件」）。
    这个坑实测踩过：build-release.py 因此在第二步就崩，从没跑完过。

    统一在这里补 .cmd 后缀 + shell=True，调用点不必各自处理。
    同时清掉 PYTHONPATH/PYTHONHOME —— 宿主环境注入的那两个会让 Python 侧
    import 到错位的包（AGENTS.md 记录过）。
    """
    is_windows = sys.platform == "win32"
    resolved = list(cmd)
    if is_windows and resolved:
        head = resolved[0]
        # npx / npm / electron-builder 这类 shim 在 Windows 上是 .cmd
        if head in ("npm", "npx", "pnpm", "yarn", "electron-builder"):
            resolved[0] = f"{head}.cmd"

    return subprocess.run(
        resolved,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        shell=is_windows,  # .cmd 需要 shell 才能解析
        env={**os.environ, "PYTHONPATH": "", "PYTHONHOME": ""},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def dir_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def human(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.2f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    return f"{n / (1 << 10):.0f} KB"


# ---------------------------------------------------------------- 各步骤

def check_env() -> None:
    step(1, "环境检查")
    if not VENV_PY.is_file():
        die(f"找不到 venv 解释器：{VENV_PY}")
    ok(f"Python: {VENV_PY}")

    if not (FRONTEND / "node_modules" / "electron").is_dir():
        die("node_modules 不完整，先跑 npm install 并补二进制")
    ok("node_modules 就位")

    for name, path in (
        ("electron.exe", FRONTEND / "node_modules" / "electron" / "dist" / "electron.exe"),
        ("esbuild.exe", FRONTEND / "node_modules" / "@esbuild" / "win32-x64" / "esbuild.exe"),
    ):
        if not path.is_file():
            die(f"{name} 缺失（npm 拦了 postinstall）→ node node_modules/electron/install.js")
        ok(f"{name}: {human(path.stat().st_size)}")

    if not (FRONTEND / "node_modules" / "electron-builder").is_dir():
        die("electron-builder 未安装 → npm install --save-dev electron-builder")
    ok("electron-builder 就位")


def check_quality() -> None:
    step(2, "类型检查与设计令牌契约")
    result = run(["npx", "tsc", "--noEmit"], FRONTEND, timeout=300)
    if result.returncode != 0:
        print(result.stdout[-2000:])
        die("TypeScript 类型检查失败")
    ok("tsc --noEmit 通过")

    result = run(["node", "tools/verify-palette.mjs"], FRONTEND, timeout=120)
    if result.returncode != 0:
        print(result.stdout[-1500:])
        die("设计令牌契约未通过")
    ok("设计令牌契约通过（对比度 / 色相 / 层级分离）")


def build_frontend() -> None:
    step(3, "前端构建")
    result = run(["npm", "run", "build"], FRONTEND, timeout=600)
    if result.returncode != 0:
        print(result.stdout[-2000:])
        print(result.stderr[-1000:])
        die("前端构建失败")
    ok("esbuild 构建完成（含零宽字符清洗）")


def run_tests() -> None:
    step(4, "Python 全量测试（发布门禁）")
    result = run([str(VENV_PY), "-m", "pytest", "tests/", "-q", "--no-header",
                  "-p", "no:cacheprovider"], REPO, timeout=1200)
    tail = (result.stdout or "")[-800:]
    print(tail)
    if result.returncode != 0:
        die("测试未全部通过 —— 发布门禁不通过")
    ok("pytest 全绿")


def build_sidecar() -> None:
    step(5, "打包 Python sidecar（PyInstaller，不含 Qt）")
    spec = FRONTEND / "backend" / "ipc_server.spec"
    if not spec.is_file():
        die(f"找不到 spec：{spec}")

    if BACKEND_DIST.exists():
        import shutil
        shutil.rmtree(BACKEND_DIST, ignore_errors=True)

    started = time.monotonic()
    result = run([str(VENV_PY), "-m", "PyInstaller", str(spec), "--noconfirm", "--clean",
                  "--distpath", "dist", "--workpath", "build"],
                 FRONTEND / "backend", timeout=2400)
    elapsed = int(time.monotonic() - started)

    if result.returncode != 0 or not SIDECAR.is_file():
        print((result.stdout or "")[-2000:])
        print((result.stderr or "")[-1500:])
        die("sidecar 打包失败")

    size = dir_size(BACKEND_DIST)
    ok(f"sidecar 就位：{human(size)}（耗时 {elapsed}s）")

    # Qt 必须被排除 —— 否则白省 115MB，而且说明 excludes 没生效
    for name in ("PySide6", "shiboken6", "qfluentwidgets"):
        found = list(BACKEND_DIST.rglob(f"*{name}*"))
        if found:
            fail(f"{name} 未被排除（{len(found)} 个文件）—— 检查 spec 的 EXCLUDES")
        else:
            ok(f"{name} 已排除")


def verify_sidecar() -> None:
    """脱离 venv 真跑一遍 sidecar —— 这是"打包没坏"的唯一证据。"""
    step(6, "sidecar 独立验收（不依赖 venv 与源码）")

    commands = [
        {"id": 1, "command": "ping", "args": None},
        {"id": 2, "command": "get_config", "args": None},
        {"id": 3, "command": "list_models", "args": None},
        {"id": 4, "command": "list_devices", "args": None},
        {"id": 5, "command": "release_notes", "args": None},
        {"id": 6, "command": "list_capture_targets", "args": None},
        {"id": 7, "command": "shutdown", "args": None},
    ]
    payload = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in commands)

    try:
        result = subprocess.run(
            [str(SIDECAR)], input=payload.encode("utf-8"),
            capture_output=True, timeout=300,
            env={**os.environ, "PYTHONPATH": "", "PYTHONHOME": ""},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        die("sidecar 超时未响应 —— 可能某个命令挂起")

    answers: dict[int, dict] = {}
    for raw in (result.stdout or b"").decode("utf-8", "replace").splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item.get("id"), int):
            answers[item["id"]] = item

    for spec in commands:
        if spec["command"] == "shutdown":
            continue
        answer = answers.get(spec["id"])
        if answer is None:
            fail(f"{spec['command']}: 无应答（sidecar 里可能缺 hiddenimport）")
        elif answer.get("ok") is True:
            data = answer.get("data")
            detail = ""
            if isinstance(data, dict):
                detail = f"{{{', '.join(list(data)[:4])}}}"
            ok(f"{spec['command']}: {detail}")
        else:
            fail(f"{spec['command']}: {answer.get('error')}")

    # 中文编码：stderr 里不该出现替换字符（GBK 乱码的痕迹）
    stderr_text = (result.stderr or b"").decode("utf-8", "replace")
    if "\ufffd" in stderr_text:
        fail("stderr 出现替换字符 —— 编码未统一为 UTF-8")
    else:
        ok("UTF-8 编码正常（无乱码替换字符）")


def build_installer(dir_only: bool) -> None:
    step(7, "生成安装包" if not dir_only else "生成免安装目录")
    args = ["npx", "electron-builder", "--config", "electron-builder.config.cjs",
            "--win", "--x64"]
    if dir_only:
        args.append("--dir")

    result = run(args, FRONTEND, timeout=2400)
    if result.returncode != 0:
        print((result.stdout or "")[-3000:])
        print((result.stderr or "")[-2000:])
        die("electron-builder 失败")
    ok("electron-builder 完成")


def verify_artifacts() -> None:
    step(8, "校验产物")
    if not RELEASE_DIR.is_dir():
        die(f"输出目录不存在：{RELEASE_DIR}")

    installers = sorted(RELEASE_DIR.glob("*.exe"))
    unpacked = RELEASE_DIR / "win-unpacked"

    if unpacked.is_dir():
        ok(f"免安装目录：{unpacked} （{human(dir_size(unpacked))}）")
        main_exe = unpacked / "VoxSub.exe"
        if main_exe.is_file():
            ok(f"主程序存在：{main_exe.name}")
        sidecar_in_pack = unpacked / "resources" / "backend" / "VoxSubBackend.exe"
        if sidecar_in_pack.is_file():
            ok(f"sidecar 已随包：{human(dir_size(sidecar_in_pack.parent))}")
        else:
            fail("安装目录里找不到 sidecar —— extraResources 没生效")

    if not installers:
        if not unpacked.is_dir():
            die("既没有安装包也没有免安装目录")
        print("\n    （--dir-only 模式：未生成安装包）")
        return

    for installer in installers:
        size = installer.stat().st_size
        digest = hashlib.sha256(installer.read_bytes()).hexdigest()
        sha_file = installer.with_suffix(installer.suffix + ".sha256")
        sha_file.write_text(f"{digest}  {installer.name}\n", encoding="utf-8")
        ok(f"{installer.name}")
        print(f"         体积: {human(size)}")
        print(f"         SHA256: {digest[:32]}…")
        print(f"         已写: {sha_file.name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-tests", action="store_true", help="跳过 pytest")
    parser.add_argument("--dir-only", action="store_true", help="只出免安装目录")
    args = parser.parse_args()

    print("=" * 70)
    print("  语幕 VoxSub — 发布构建")
    print("=" * 70)

    check_env()
    check_quality()
    build_frontend()
    if args.skip_tests:
        print("\n[4] Python 全量测试 —— 已按 --skip-tests 跳过")
    else:
        run_tests()
    build_sidecar()
    verify_sidecar()
    build_installer(args.dir_only)
    verify_artifacts()

    print("\n" + "=" * 70)
    print(f"  完成：{passed} 项通过，{failed} 项失败")
    print("=" * 70)
    if failed:
        print("\n有未通过项 —— 不要发布这个产物。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
