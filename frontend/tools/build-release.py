#!/usr/bin/env python
"""发布构建 —— 一条命令产出可安装的 Windows 安装包。

流程（任一步失败即中断，不留半成品）：

    0. 版本门禁      六处版本位点必须一致（单一权威来源 = frontend/package.json）
    1. 环境检查      venv / node_modules / 二进制是否就位
    2. 类型与契约     tsc --noEmit + 设计令牌契约
    3. 前端构建       esbuild（构建前后做源码漂移检测）
    4. Python 测试    全量 pytest（发布门禁）
    5. sidecar 打包   PyInstaller（excludes 掉 Qt）
    6. sidecar 验收   独立跑 IPC 握手 + 关键命令，确认不带 venv 也能工作
    7. 安装包         electron-builder → Release/electron/
    8. 校验产物       安装包（按当前版本过滤）存在 + 主程序必须存在 + SHA256
    9. 构建清单       写 build/release-manifest-<版本>.json

为什么第 6 步不能省：sidecar 是"打包成功但功能缺失"最容易发生的环节 ——
PyInstaller 的 hiddenimports 漏一个，运行到某个命令才炸，而那时已经装到
用户机器上了。所以打完必须**脱离 venv** 真跑一遍。

为什么要有版本门禁（第 0 步）和产物按版本过滤（第 8 步）：
此前版本号散落在至少六处（package.json / electron-builder 配置 / Inno Setup /
Python 包 / IPC 握手 / README 与发布说明），没有任何自动检查，且产物校验用
`glob("*.exe")` 会把上一版的安装包也当成"本次产物"通过 —— 于是"版本没同步"
和"其实只产出了陈旧安装包"都能发布出去。现在两件事都是硬失败。

为什么第 3 步要做源码漂移检测：
`npm run build` 会先跑 `prebuild` → `frontend/scripts/sanitize.mjs`，它**就地清写
源码**再退出 1。构建前置步骤只允许检查、不允许偷偷改工作区源码，所以这里在构建
前后各取一次源码摘要，发现差异就明确报出被改写的文件（默认硬失败）。

用法：
    python frontend/tools/build-release.py                  完整流程
    python frontend/tools/build-release.py --check-only     只跑可离线执行的发布门禁
    python frontend/tools/build-release.py --skip-tests     跳过 pytest（快速迭代用）
    python frontend/tools/build-release.py --dir-only       只出免安装目录（不生成安装包）
    python frontend/tools/build-release.py --allow-source-rewrite
                                                            源码被 prebuild 清洗改写时只告警
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRONTEND = HERE.parent
REPO = FRONTEND.parent
VENV_PY = REPO / ".venv" / "Scripts" / "python.exe"
RELEASE_DIR = REPO.parent / "Release" / "electron"
BACKEND_DIST = FRONTEND / "backend" / "dist" / "VoxSubBackend"
SIDECAR = BACKEND_DIST / "VoxSubBackend.exe"

# 前端构建会写入的目录 / 不该参与漂移检测的目录
SOURCE_SKIP_DIRS = frozenset({
    "node_modules", "dist", ".git", "release", "__pycache__",
    ".pytest_cache", "build", ".venv",
})
SOURCE_EXTENSIONS = frozenset({
    ".ts", ".tsx", ".css", ".html", ".mjs", ".js", ".json", ".py", ".md",
})

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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(args: list[str], default: str = "") -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(REPO), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return default
    if result.returncode != 0:
        return default
    return (result.stdout or "").strip()


# ====================================================================== 版本门禁
#
# 单一权威来源 = frontend/package.json 的 "version"。
# 其他位点都必须与它一致，或者**派生**自它（例如 electron-builder 的
# artifactName 用 ${version}、IPC 握手用 voxsub.__version__）。


@dataclasses.dataclass(frozen=True)
class VersionSite:
    """一个版本位点。

    pattern 用正则从纯文本里抓版本号；没有捕获组时取 group=0。
    required=True 表示文件/匹配缺失本身就是门禁失败（位点被删掉 = 契约被绕过）。
    derived=True 表示该位点不含字面版本号，而是引用权威来源。
    """

    name: str
    relpath: str
    pattern: str
    group: int = 1
    required: bool = True
    derived: bool = False


VERSION_SITES: tuple[VersionSite, ...] = (
    VersionSite(
        "package.json（权威来源）", "frontend/package.json",
        r'"version"\s*:\s*"([^"]+)"',
    ),
    VersionSite(
        "Python 包版本", "voxsub/__init__.py",
        r'^__version__\s*=\s*"([^"]+)"',
    ),
    VersionSite(
        "Inno Setup 版本宏", "scripts/installer.iss",
        r'^#define\s+MyAppVersion\s+"([^"]+)"',
    ),
    VersionSite(
        "Inno Setup 输出文件名", "scripts/installer.iss",
        r'^OutputBaseFilename=VoxSub-Setup-([^.\s]+(?:\.[^.\s]+)*)$',
    ),
    VersionSite(
        "Inno Setup 构建注释", "scripts/installer.iss",
        r'^;\s*Build:.*->\s*VoxSub-Setup-(.+?)\.exe\s*$',
    ),
    VersionSite(
        "electron-builder 输出模板", "frontend/electron-builder.config.cjs",
        r'artifactName:\s*"[^"]*\$\{version\}([^"]*)"',
        derived=True, required=True,
    ),
    VersionSite(
        "IPC 握手版本（派生）", "frontend/backend/ipc_server.py",
        r'from voxsub import __version__',
        group=0, derived=True,
    ),
    VersionSite(
        "发布说明历史条目", "voxsub/release_notes.py",
        r'RELEASE_HISTORY[\s\S]*?\n\s*"([^"]+)"',
    ),
    VersionSite(
        "README 候选版本声明", "README.md",
        r"当前源码候选版本：`([^`]+)`",
    ),
    VersionSite(
        "README_EN 候选版本声明", "README_EN.md",
        r"Current source candidate: `([^`]+)`",
    ),
    VersionSite(
        "RELEASE_NOTES 版本标题", "RELEASE_NOTES.md",
        r"^##\s*版本:\s*v?([^\s（(]+)",
    ),
    # IPC 契约文档里的"撰写时的应用版本"。该文件由契约接线工作产出，
    # 是可选位点：存在就硬校验，尚未产出时跳过而不是误判失败。
    VersionSite(
        "IPC 契约 appVersionAtAuthoring", "contracts/protocol.json",
        r'"appVersionAtAuthoring"\s*:\s*"([^"]+)"',
        required=False,
    ),
)


@dataclasses.dataclass(frozen=True)
class VersionHit:
    name: str
    file: str
    line: int
    value: str
    derived: bool = False

    def as_dict(self) -> dict:
        return {"site": self.name, "file": self.file, "line": self.line,
                "value": self.value, "derived": self.derived}


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def read_product_version(root: Path = REPO) -> str:
    """权威来源：frontend/package.json 的 version。读不到就抛错（不猜）。"""
    pkg = root / "frontend" / "package.json"
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法从 {pkg} 读取产品版本：{exc}") from exc
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError(f"{pkg} 缺少非空 version 字段")
    return version.strip()


def collect_version_sites(
    root: Path = REPO, sites: tuple[VersionSite, ...] = VERSION_SITES,
) -> tuple[list[VersionHit], list[str]]:
    """纯文本解析所有版本位点。

    返回 (命中的位点列表, 错误列表)。不做任何文件写入。
    """
    hits: list[VersionHit] = []
    errors: list[str] = []
    for site in sites:
        path = root / site.relpath
        if not path.is_file():
            if site.required and not site.derived:
                errors.append(f"{site.relpath}: 找不到对应文件（版本位点缺失）")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append(f"{site.relpath}: 读取失败 {exc}")
            continue
        match = re.search(site.pattern, text, re.MULTILINE)
        if match is None:
            if site.required:
                errors.append(f"{site.relpath}: 未匹配到版本位点「{site.name}」")
            continue
        value = match.group(site.group) if (match.groups() and site.group) else match.group(0)
        anchor = match.start(site.group) if (match.groups() and site.group) else match.start()
        hits.append(VersionHit(
            name=site.name, file=site.relpath, line=_line_of(text, anchor),
            value=value.strip(), derived=site.derived,
        ))
    return hits, errors


def optional_version_sites_status(
    root: Path = REPO, sites: tuple[VersionSite, ...] = VERSION_SITES,
) -> list[str]:
    """可选版本位点的存在性说明。

    可选位点缺失不算失败（例如契约文件尚未产出），但**必须被明确报出来** ——
    "可选"不等于"静默通过"，否则就复刻了本工作单要修的静默缺陷。
    """
    notes: list[str] = []
    for site in sites:
        if site.required:
            continue
        path = root / site.relpath
        if not path.is_file():
            notes.append(f"可选位点未产出：{site.relpath}（{site.name}）")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(site.pattern, text, re.MULTILINE) is None:
            notes.append(f"可选位点存在但未匹配：{site.relpath}（{site.name}）")
    return notes


def compare_versions(
    hits: list[VersionHit], errors: list[str], product_version: str,
) -> list[str]:
    """把位点列表和权威版本比对，返回所有不一致的描述。纯函数，不读盘。"""
    problems = list(errors)
    for hit in hits:
        if hit.derived:
            continue
        if hit.value != product_version:
            problems.append(
                f"{hit.file}:{hit.line} {hit.name} = {hit.value!r}，"
                f"权威版本为 {product_version!r}"
            )
    return problems


def check_versions(root: Path = REPO) -> tuple[str, list[VersionHit]]:
    """版本一致性门禁 —— 不一致直接退出非 0。"""
    step(0, "版本一致性门禁")
    try:
        product_version = read_product_version(root)
    except RuntimeError as exc:
        die(str(exc))

    print(f"    权威版本（frontend/package.json）：{product_version}")

    hits, errors = collect_version_sites(root)
    for hit in hits:
        marker = "（派生）" if hit.derived else ""
        print(f"    · {hit.file}:{hit.line}  {hit.name} = {hit.value}{marker}")

    for note in optional_version_sites_status(root):
        print(f"    --   {note}")

    problems = compare_versions(hits, errors, product_version)
    if problems:
        for problem in problems:
            fail(problem)
        die(
            f"版本不一致：{len(problems)} 处位点与权威版本 {product_version} 不匹配。"
            "先统一版本号再发布（不要靠放松检查通过）"
        )
    ok(f"版本一致：{len(hits)} 个位点全部等于 {product_version}")
    return product_version, hits


# ============================================================== 源码漂移检测
#
# 构建前置步骤（frontend/scripts/sanitize.mjs）会就地清写源码。构建只允许
# 检查、不允许偷偷改工作区，所以这里做前后摘要比对。


def snapshot_sources(root: Path = FRONTEND) -> dict[str, str]:
    """抓取前端源码树的内容摘要。跳过 node_modules/dist 等产物目录。"""
    snapshot: dict[str, str] = {}
    if not root.is_dir():
        return snapshot
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if any(part in SOURCE_SKIP_DIRS for part in parts):
            continue
        if path.suffix not in SOURCE_EXTENSIONS:
            continue
        try:
            snapshot[str(path.relative_to(root)).replace("\\", "/")] = sha256_file(path)
        except OSError:
            continue
    return snapshot


def source_drift(
    before: dict[str, str], after: dict[str, str],
) -> list[str]:
    """返回被改写/新增/删除的源码文件，格式 "路径（已改写|已新增|已删除）"。纯函数。"""
    drift: list[str] = []
    for name, digest in sorted(after.items()):
        if name not in before:
            drift.append(f"{name}（已新增）")
        elif before[name] != digest:
            drift.append(f"{name}（已改写）")
    for name in sorted(before):
        if name not in after:
            drift.append(f"{name}（已删除）")
    return drift


def report_source_drift(drift: list[str], *, allowed: bool) -> None:
    """把源码漂移报成明确警告；默认同时记为硬失败。"""
    print(f"\n    !! 构建前置步骤改写了 {len(drift)} 个源码文件（工作区不再干净）：")
    for item in drift:
        print(f"       - {item}")
    print("    !! 构建前置检查只应检查，不应改写源码。")
    print("       frontend/scripts/sanitize.mjs 由 npm 的 prebuild 钩子触发，会就地清除")
    print("       零宽字符并退出 1。请先人工处理这些文件并提交，再重新构建；")
    print("       确需放行时用 --allow-source-rewrite（只告警，不再判定失败）。")
    if allowed:
        print("       --allow-source-rewrite 已开启：仅告警。")
    else:
        fail(f"构建前置步骤改写了源码（{len(drift)} 个文件），构建不可信")


# ==================================================================== 各步骤

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


def build_frontend(allow_source_rewrite: bool = False) -> None:
    step(3, "前端构建")
    before = snapshot_sources()

    result = run(["npm", "run", "build"], FRONTEND, timeout=600)

    after = snapshot_sources()
    drift = source_drift(before, after)
    if drift:
        report_source_drift(drift, allowed=allow_source_rewrite)
        if not allow_source_rewrite:
            die("构建前置步骤改写了工作区源码 —— 先清理并提交这些文件再发布")

    if result.returncode != 0:
        print(result.stdout[-2000:])
        print(result.stderr[-1000:])
        die("前端构建失败")
    ok("esbuild 构建完成（零宽字符已在源码层通过门禁，构建未改写工作区）")


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

    build_speech_runtime()
    size = dir_size(BACKEND_DIST)
    ok(f"sidecar 就位：{human(size)}（耗时 {elapsed}s）")

    # Qt 必须被排除 —— 否则白省 115MB，而且说明 excludes 没生效
    for name in ("PySide6", "shiboken6", "qfluentwidgets"):
        found = list(BACKEND_DIST.rglob(f"*{name}*"))
        if found:
            fail(f"{name} 未被排除（{len(found)} 个文件）—— 检查 spec 的 EXCLUDES")
        else:
            ok(f"{name} 已排除")


def build_speech_runtime() -> None:
    """Distribute the independently killable CPU worker alongside the light sidecar."""
    import shutil
    python = REPO / ".venv-speech" / "Scripts" / "python.exe"
    if not python.is_file():
        die("缺少语音翻译运行环境，请先运行 scripts/setup-speech-runtime.ps1")
    result = run([str(python), "-m", "PyInstaller", str(FRONTEND / "backend" / "speech_worker.spec"),
                  "--noconfirm", "--distpath", str(FRONTEND / "backend" / "dist"),
                  "--workpath", str(FRONTEND / "backend" / "build-speech")], REPO, timeout=2400)
    source = FRONTEND / "backend" / "dist" / "VoxSubSpeechWorker"
    if result.returncode or not (source / "VoxSubSpeechWorker.exe").is_file():
        die("语音翻译组件打包失败；不发布缺少组件的模型入口")
    shutil.copytree(source, BACKEND_DIST / "speech-runtime", dirs_exist_ok=True)


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


# ======================================================== 产物校验（纯函数部分）


@dataclasses.dataclass
class ArtifactReport:
    """产物校验结论。所有字段都由 inspect_artifacts 纯逻辑填充，便于离线测试。"""

    current: list[str] = dataclasses.field(default_factory=list)
    stale: list[str] = dataclasses.field(default_factory=list)
    ok_notes: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors


def _name_matches_version(name: str, version: str) -> bool:
    """安装包文件名里是否恰好带有这个版本号。

    不能用简单的 `version in name`：`"1.2.3-beta" in "…Setup-11.2.3-beta.exe"`
    也是 True，会把别的版本当成本次产物。这里要求版本号前后都不是版本字符，
    并且先剥掉扩展名（否则 "1.2.3-beta.exe" 的 ".e" 会挡住尾边界）。
    """
    stem = Path(name).stem
    pattern = r"(?<![0-9A-Za-z.])" + re.escape(version) + r"(?![0-9A-Za-z.])"
    return re.search(pattern, stem) is not None


def select_installers(release_dir: Path, version: str) -> tuple[list[Path], list[Path]]:
    """按当前版本拆分安装包：返回 (当前版本, 陈旧版本)。

    旧实现是 sorted(RELEASE_DIR.glob('*.exe')) —— 不做任何版本过滤，
    上一版的安装包会被当成本次产物打上 SHA256 并"通过"。
    """
    if not release_dir.is_dir():
        return [], []
    current: list[Path] = []
    stale: list[Path] = []
    for candidate in sorted(release_dir.glob("*.exe")):
        (current if _name_matches_version(candidate.name, version) else stale).append(candidate)
    return current, stale


def inspect_artifacts(
    version: str, release_dir: Path = RELEASE_DIR, *, dir_only: bool = False,
) -> ArtifactReport:
    """离线可测的产物门禁。

    硬失败项（任一命中即 errors 非空）：
      · 输出目录不存在
      · 免安装目录存在但主程序 VoxSub.exe 缺失
      · 免安装目录里没有随包 sidecar
      · 不是 --dir-only 却没有任何当前版本的安装包
      · 只有陈旧版本的安装包（把上一版当本次产物）
      · 既没有安装包也没有免安装目录
    """
    report = ArtifactReport()

    if not release_dir.is_dir():
        report.errors.append(f"输出目录不存在：{release_dir}")
        return report

    unpacked = release_dir / "win-unpacked"
    if unpacked.is_dir():
        report.ok_notes.append(f"免安装目录：{unpacked}（{human(dir_size(unpacked))}）")

        # 主程序缺失必须是硬失败 —— 旧实现只有一个 if，没有 else，静默通过
        main_exe = unpacked / "VoxSub.exe"
        if main_exe.is_file():
            report.ok_notes.append(f"主程序存在：{main_exe.name}")
        else:
            report.errors.append(
                f"免安装目录里找不到主程序 {main_exe.name}：{unpacked}"
                "（electron-builder 的 --dir 输出不完整，不可发布）"
            )

        sidecar_in_pack = unpacked / "resources" / "backend" / "VoxSubBackend.exe"
        if sidecar_in_pack.is_file():
            report.ok_notes.append(f"sidecar 已随包：{human(dir_size(sidecar_in_pack.parent))}")
        else:
            report.errors.append("安装目录里找不到 sidecar —— extraResources 没生效")

    current, stale = select_installers(release_dir, version)
    report.current = [p.name for p in current]
    report.stale = [p.name for p in stale]

    if not current:
        if dir_only:
            if stale:
                report.errors.append(
                    "只存在陈旧版本的安装包（当前版本 " + version + " 无产物）："
                    + ", ".join(report.stale)
                )
            else:
                report.ok_notes.append("--dir-only 模式：未生成安装包（符合预期）")
        elif stale:
            report.errors.append(
                f"只有陈旧版本的安装包，当前版本 {version} 没有产物："
                + ", ".join(report.stale)
                + " —— 不要把这个 Release 目录当成本次构建结果"
            )
        elif not unpacked.is_dir():
            report.errors.append("既没有安装包也没有免安装目录")
        else:
            report.errors.append(
                f"没有匹配当前版本 {version} 的安装包 —— 可能 electron-builder 未执行或产物被改名"
            )
    else:
        for name in report.current:
            report.ok_notes.append(f"当前版本安装包：{name}")
        if report.stale:
            report.ok_notes.append(
                "同时存在历史安装包（已忽略，不入本次清单）：" + ", ".join(report.stale)
            )

    return report


def verify_artifacts(version: str, *, dir_only: bool = False) -> ArtifactReport:
    step(8, "校验产物")
    report = inspect_artifacts(version, RELEASE_DIR, dir_only=dir_only)

    for note in report.ok_notes:
        ok(note)
    for error in report.errors:
        fail(error)

    if not report.passed:
        die("产物校验未通过 —— 不要发布这个产物")

    for name in report.current:
        installer = RELEASE_DIR / name
        size = installer.stat().st_size
        digest = sha256_file(installer)
        sha_file = installer.with_suffix(installer.suffix + ".sha256")
        sha_file.write_text(f"{digest}  {installer.name}\n", encoding="utf-8")
        print(f"         {installer.name}  体积 {human(size)}  SHA256 {digest}…")
        print(f"         已写: {sha_file.name}")

    return report


# ================================================================ 构建清单

def toolchain_versions() -> dict[str, str]:
    """采集工具链版本。采集不到的写 "unknown"，不猜。"""
    out: dict[str, str] = {}
    try:
        result = subprocess.run(
            [str(VENV_PY), "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out["python"] = (result.stdout or result.stderr or "").strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        out["python"] = "unknown"

    for key, cmd in (("node", ["node", "--version"]), ("npm", ["npm", "--version"])):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=60, shell=(sys.platform == "win32"),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            out[key] = (result.stdout or "").strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            out[key] = "unknown"

    pkg_path = FRONTEND / "package.json"
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        deps = pkg.get("devDependencies", {})
        out["electron-pinned"] = deps.get("electron", "unknown")
        out["electron-builder-pinned"] = deps.get("electron-builder", "unknown")
    except (OSError, json.JSONDecodeError):
        out["electron-pinned"] = "unknown"
        out["electron-builder-pinned"] = "unknown"
    return out


def lockfile_digests(root: Path = REPO) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in (root / "requirements.lock", root / "requirements.txt",
                 root / "frontend" / "package-lock.json"):
        if path.is_file():
            digests[str(path.relative_to(root)).replace("\\", "/")] = sha256_file(path)
        else:
            digests[str(path.relative_to(root)).replace("\\", "/")] = "missing"
    return digests


def build_manifest(
    *,
    product_version: str,
    protocol_version: str,
    version_sites: list[VersionHit],
    tests_executed: list[str],
    artifacts: list[dict],
    result: str,
    root: Path = REPO,
    extra: dict | None = None,
) -> dict:
    """组装构建清单。纯逻辑，不写盘，便于测试。"""
    manifest = {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit": _git(["rev-parse", "HEAD"]) or "unknown",
        "commit_short": _git(["rev-parse", "--short", "HEAD"]) or "unknown",
        "dirty": bool(_git(["status", "--porcelain"])),
        "product_version": product_version,
        "protocol_version": protocol_version,
        "toolchain": toolchain_versions(),
        "lockfiles_sha256": lockfile_digests(root),
        "version_sites": [hit.as_dict() for hit in version_sites],
        "tests_executed": list(tests_executed),
        "artifacts": list(artifacts),
        "result": result,
    }
    if extra:
        manifest.update(extra)
    return manifest


def manifest_path(product_version: str, root: Path = REPO) -> Path:
    """清单写到仓库内 build/ 下 —— 不碰正式 Release 目录。

    版本号会被清洗，确保清理后仍严格落在 build/ 之内（`../` 之类不能逃逸）。
    """
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", product_version)
    safe = re.sub(r"\.{2,}", ".", safe).strip("._-")
    return root / "build" / f"release-manifest-{safe or 'unknown'}.json"


def write_manifest(manifest: dict, version: str, root: Path = REPO) -> Path:
    path = manifest_path(version, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path


def collect_artifacts(version: str) -> list[dict]:
    """列出本次版本的产物（按版本过滤，陈旧产物不入清单）。"""
    current, _stale = select_installers(RELEASE_DIR, version)
    entries: list[dict] = []
    for installer in current:
        entries.append({
            "kind": "installer",
            "path": str(installer),
            "bytes": installer.stat().st_size,
            "sha256": sha256_file(installer),
        })
    unpacked = RELEASE_DIR / "win-unpacked"
    if unpacked.is_dir():
        entries.append({
            "kind": "unpacked-dir",
            "path": str(unpacked),
            "bytes": dir_size(unpacked),
            "sha256": None,
        })
    return entries


def protocol_version_axis(root: Path = REPO) -> dict:
    """协议版本存在两条独立的轴，必须分清而不是硬凑成一个数。

    实测现状（2026-09-21，`contracts/` 契约与本轮接线之后）：

        contracts/protocol.json → protocolVersion = "2.0.0"（**契约文档**的演进版本）
                                  wireProtocolVersion.value = 1（**线上整数**）
                                  wireProtocolVersion.sourceConstant = "ipc_loop.PROTOCOL_VERSION"
        frontend/backend/ipc_loop.py → PROTOCOL_VERSION = 1

    两条轴不是同一个序列，也不该机械换算 —— 契约里显式这么写了，且
    `tests/test_contracts.py` 断言线上整数与 `PROTOCOL_VERSION` 相等。
    所以这里**只比较线上整数**：那才是"前后端能不能对上"的判断依据。

    线上整数不一致 = 前后端握手会错，属于**硬失败**；拿不到值时只报告
    （可能是契约还没落地，那时候不该把发布卡住）。
    """
    result: dict = {
        "handshake_integer": None,
        "contract_integer": None,
        "contract_semver": None,
        "consistent": None,
        "findings": [],
    }

    loop = root / "frontend" / "backend" / "ipc_loop.py"
    if loop.is_file():
        text = loop.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"^PROTOCOL_VERSION\s*=\s*(\d+)", text, re.MULTILINE)
        if match:
            result["handshake_integer"] = int(match.group(1))
        else:
            result["findings"].append("frontend/backend/ipc_loop.py 里找不到 PROTOCOL_VERSION")

    contract = root / "contracts" / "protocol.json"
    if contract.is_file():
        try:
            data = json.loads(contract.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            result["findings"].append(f"contracts/protocol.json 解析失败：{exc}")
            data = {}
        result["contract_semver"] = data.get("protocolVersion")
        # 线上整数的出处随契约演进换过字段名，两种形状都认：
        #   · wireProtocolVersion.value（当前形状，附带 sourceConstant 指向权威常量）
        #   · protocolVersionInteger（旧形状）
        wire = data.get("wireProtocolVersion")
        if isinstance(wire, dict):
            result["contract_integer"] = wire.get("value")
        else:
            result["contract_integer"] = data.get("protocolVersionInteger")

    handshake, declared = result["handshake_integer"], result["contract_integer"]
    if handshake is None or declared is None:
        result["findings"].append(
            "协议版本轴未被完整声明（缺少 ipc_loop.PROTOCOL_VERSION 或 "
            "contracts/protocol.json 的线上整数），本次不做一致性判断"
        )
        return result

    result["consistent"] = handshake == declared
    if not result["consistent"]:
        result["findings"].append(
            f"协议线上整数不一致：ipc_loop.PROTOCOL_VERSION = {handshake}，"
            f"但 contracts/protocol.json 声明 {declared}（契约文档版本 {result['contract_semver']}）"
            " —— 前后端握手会对不上，必须统一后再发布"
        )
    return result


def protocol_version_from_source(root: Path = REPO) -> str:
    """IPC 握手版本 = voxsub.__version__（ipc_server 的 ping 直接返回它）。"""
    init = root / "voxsub" / "__init__.py"
    text = init.read_text(encoding="utf-8", errors="replace")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "unknown"


# ======================================================================== 主流程

def emit_manifest(
    product_version: str,
    hits: list[VersionHit],
    *,
    mode: str,
    tests_executed: list[str],
    artifacts: list[dict],
    result: str,
    root: Path = REPO,
    extra: dict | None = None,
) -> Path:
    manifest = build_manifest(
        product_version=product_version,
        protocol_version=protocol_version_from_source(root),
        version_sites=hits,
        tests_executed=tests_executed,
        artifacts=artifacts,
        result=result,
        root=root,
        extra={"mode": mode, "protocol_version_axis": protocol_version_axis(root),
               **(extra or {})},
    )
    return write_manifest(manifest, product_version, root)


def check_only(root: Path = REPO) -> int:
    """只跑可离线执行的发布门禁（不打包、不写 Release）。测试用它当入口。"""
    print("=" * 70)
    print("  语幕 VoxSub — 发布门禁（--check-only）")
    print("=" * 70)

    version, hits = check_versions(root)

    axis = protocol_version_axis(root)
    print(f"\n[check] 协议版本（IPC 握手语义版本）：{protocol_version_from_source(root)}")
    print(f"[check] 源码树摘要文件数：{len(snapshot_sources())}")
    print(f"[check] 协议版本轴：{axis}")
    print("[check] 未执行打包：electron-builder / PyInstaller / Release 目录均未被触碰")

    if failed:
        print(f"\n{passed} 项通过，{failed} 项失败")
        return 1
    print(f"\n{passed} 项通过，0 项失败")
    return 0


def manifest_only(root: Path = REPO) -> int:
    """只写构建清单（不打包）。用于验证清单生成路径本身是通的。"""
    print("=" * 70)
    print("  语幕 VoxSub — 构建清单（--manifest-only，不打包）")
    print("=" * 70)

    version, hits = check_versions(root)
    if failed:
        print(f"\n{passed} 项通过，{failed} 项失败")
        return 1

    step(9, "写构建清单")
    # 不打包就没有本次产物：绝不把 Release 目录里别人（或上一轮）留下的东西
    # 写成本次构建产物。artifacts 必须诚实地为空。
    path = emit_manifest(
        version, hits,
        mode="manifest-only",
        tests_executed=[],
        artifacts=[],
        result="pass",
        root=root,
        extra={"packaged": False},
    )
    ok(f"构建清单：{path}")
    print(f"\n{passed} 项通过，0 项失败")
    return 0


def _configure_console() -> None:
    """CLI pipes must support Chinese even on a Windows CP1252 runner.

    Only called by main: importing the build helpers must not mutate the host's
    capture streams. Preserve streams without reconfigure (e.g. StringIO).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")


def main() -> int:
    _configure_console()
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-tests", action="store_true", help="跳过 pytest")
    parser.add_argument("--dir-only", action="store_true", help="只出免安装目录")
    parser.add_argument("--check-only", action="store_true",
                        help="只跑版本/产物门禁检查，不打包")
    parser.add_argument("--manifest-only", action="store_true",
                        help="只做版本门禁并写构建清单，不打包")
    parser.add_argument("--allow-source-rewrite", action="store_true",
                        help="构建前置步骤改写源码时只告警（默认硬失败）")
    args = parser.parse_args()

    if args.check_only:
        return check_only()
    if args.manifest_only:
        return manifest_only()

    print("=" * 70)
    print("  语幕 VoxSub — 发布构建")
    print("=" * 70)

    product_version, hits = check_versions()
    check_env()
    check_quality()
    build_frontend(allow_source_rewrite=args.allow_source_rewrite)
    tests_executed = ["pytest tests/ (全量)"]
    if args.skip_tests:
        print("\n[4] Python 全量测试 —— 已按 --skip-tests 跳过")
        tests_executed = ["pytest tests/ (全量) —— 本次 SKIPPED"]
    else:
        run_tests()
    build_sidecar()
    verify_sidecar()
    build_installer(args.dir_only)
    verify_artifacts(product_version, dir_only=args.dir_only)

    step(9, "写构建清单")
    path = emit_manifest(
        product_version, hits,
        mode="dir-only" if args.dir_only else "full",
        tests_executed=tests_executed,
        artifacts=collect_artifacts(product_version),
        result="pass" if failed == 0 else "fail",
        extra={"dir_only": args.dir_only, "skip_tests": args.skip_tests,
               "packaged": not args.dir_only},
    )
    ok(f"构建清单：{path}")

    print("\n" + "=" * 70)
    print(f"  完成：{passed} 项通过，{failed} 项失败")
    print("=" * 70)
    if failed:
        print("\n有未通过项 —— 不要发布这个产物。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
