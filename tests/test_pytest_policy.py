"""Regression checks for the default non-intrusive test policy."""
from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _hardware_audio_tests(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Attribute)
            and decorator.attr == "hardware_audio"
            for decorator in node.decorator_list
        )
    }


def test_default_pytest_run_excludes_physical_audio_tests() -> None:
    config = (ROOT / "pytest.ini").read_text(encoding="utf-8")

    assert 'addopts = -m "not hardware_audio"' in config
    assert "hardware_audio:" in config


def test_tests_that_access_physical_audio_devices_are_marked() -> None:
    assert _hardware_audio_tests(ROOT / "tests" / "test_audio.py") == {
        "test_list_microphones_real_nonempty",
        "test_list_loopbacks_real_paired_with_speakers",
        "test_loopback_closure_sine",
        "test_loopback_chunk_format",
        "test_mic_source_smoke",
    }
    assert _hardware_audio_tests(ROOT / "tests" / "test_process_audio.py") == {
        "test_process_loopback_captures_target_tone",
        "test_process_loopback_excludes_other_process_tone",
    }


# ------------------------------------------------------------------ 选择器组合
#
# 回归背景（真的踩过）：`hardware_audio` 只在**默认**运行里被 addopts 排除。
# 一旦有人显式写 `-m integration`，那就成了"按标记覆盖 addopts"，
# `hardware_audio` 的测试会被**一起选中** —— 其中
# `test_loopback_closure_sine` 会真的从默认扬声器播 2 秒 440Hz 正弦。
# 在别人正在用电脑的时候跑一次集成测试，就等于突然响一声。
#
# 所以规则是：**任何显式点名 integration 的地方，都必须同时排除 hardware_audio。**

#: 需要检查的"会被人照抄"的文件：CI、文档、脚本。
_GUARDED_FILES = (
    ".github/workflows/quality.yml",
    ".github/workflows/npu-hardware.yml",
    "docs/HANDOVER.md",
    "docs/MAINTAINABILITY_REPORT.md",
    "AGENTS.md",
    "README.md",
)


def _looks_like_prose(line: str) -> bool:
    """注释、引用、表格行、行内代码 —— 这些是**说明**，不是能照抄的命令。"""
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.startswith(("#", ">", "|", "*", "- ")):
        return True
    return "`" in stripped  # 反引号 = 文档里的行内代码


#: 匹配各种写法的 ``-m`` 选择器里出现 integration：``-m integration``、
#: ``-m "integration and not hardware_audio"`` 都算。
_INTEGRATION_SELECTOR = re.compile(r"-m\s+\S*integration")


def _integration_selections() -> list[tuple[Path, int, str]]:
    """找出所有**真的会被照着执行**的 ``-m integration`` 调用点。

    刻意跳过注释与文档散文：规则要管的是"有人复制粘贴去跑"的那种行，
    而不是"我们提到过这个词"。把散文也算进来只会让门禁变成噪音，
    最后被人关掉。
    """
    found: list[tuple[Path, int, str]] = []
    for relative in _GUARDED_FILES:
        path = ROOT / relative
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not _INTEGRATION_SELECTOR.search(line):
                continue
            if _looks_like_prose(line):
                continue
            found.append((path, number, line.strip()))
    return found


def test_explicit_integration_runs_always_exclude_audio_tests() -> None:
    selections = _integration_selections()
    # 既要防"漏排除"，也要防"规则被自己的过滤条件静默架空"。
    assert selections, (
        "没有找到任何 integration 调用点 —— 检查规则本身可能失效了"
        "（比如文件改名或过滤条件过宽）。请更新 _GUARDED_FILES 或 _looks_like_prose。"
    )
    offenders = [
        f"{path.relative_to(ROOT)}:{number}: {line}"
        for path, number, line in selections
        if "not hardware_audio" not in line
    ]
    assert not offenders, (
        "这些地方显式按 integration 选测试，却没有排除 hardware_audio —— "
        "会真的从扬声器放声音出来：\n" + "\n".join(offenders)
    )


def test_audio_playing_tests_are_always_marked_as_hardware() -> None:
    """凡是会出声（调用 ``.play(``）的测试，必须挂 hardware_audio 标记。

    这条比"人工记得加标记"可靠：新写一个会播声音的集成测试，忘了挂标记
    也会红。
    """
    unmarked: list[str] = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        marked = _hardware_audio_tests(path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            plays = any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "play"
                for child in ast.walk(node)
            )
            if plays and node.name not in marked:
                unmarked.append(f"{path.relative_to(ROOT)}::{node.name}")

    assert not unmarked, (
        "这些测试会播放声音但没有 hardware_audio 标记，默认运行会突然出声：\n"
        + "\n".join(unmarked)
    )
