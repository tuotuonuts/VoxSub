"""Small dependency rules that keep package layers from growing together.

工作单 §3.9 L1 与缺陷 #13：门禁原先只扫 ``voxsub/``，**漏掉 ``frontend/backend``
（IPC 适配层）**。适配层恰恰是历史上最容易堆积编排逻辑的地方，所以这里把
扫描范围扩到两层，并补上三条新的边界规则。

扫描范围刻意排除 ``dist/`` 与 ``__pycache__``：那里是第三方库的打包产物，
不是我们的代码，把它们算进门禁只会得到一堆无法处理的噪音。
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "voxsub"
ADAPTER_ROOT = REPO_ROOT / "frontend" / "backend"

#: 复杂度预算：函数的分支数上限。超过就该拆了。
COMPLEXITY_BUDGET = 15

#: 存量超标函数（棘轮基线）。
#:
#: 这些函数在门禁覆盖到适配层之前就已超标。本轮**不推倒重写**，但要求：
#:   · 不允许新增超标函数；
#:   · 存量函数不允许变得更差（分数只能降不能升）；
#:   · 每消化一个就把这一行删掉 —— 这张表只该变短。
COMPLEXITY_BASELINE: dict[str, int] = {
    "ipc_server.py:_apply_saved_config": 22,
    # 这一条随"按业务域拆 IPC"搬到了 handlers/migration.py —— 基线键必须跟着走，
    # 否则 test_complexity_baseline_only_shrinks 会把它当成"已消化"而报过时。
    "handlers/migration.py:_cmd_start_migration": 19,
    "legacy_migration.py:detect_legacy_install": 17,
    "legacy_migration.py:assess_storage": 24,
    "legacy_migration.py:verify_copy": 15,
    "migration_ledger.py:validate_cleanup_target": 19,
    "contract_validation.py:_collect": 21,
    "contract_validation.py:_collect_object": 17,
}

#: 核心包绝不依赖适配层与 UI 框架：依赖方向只能是"适配层 → 核心"。
FORBIDDEN_CORE_IMPORTS = frozenset({
    "ipc_server", "ipc_loop", "job_runner", "migration_ledger",
    "legacy_migration", "contract_validation",
})

#: 适配层不许把 Qt 拉回来 —— 打包时排除 Qt 是刻意的（体积与启动时间）。
FORBIDDEN_ADAPTER_IMPORTS = frozenset({
    "PySide6", "shiboken6", "qfluentwidgets",
})

_SKIP_DIRS = frozenset({"dist", "__pycache__", "build", "node_modules"})


def _iter_project_python() -> list[tuple[Path, str]]:
    """列出要检查的源文件，返回 (绝对路径, 相对标签)。"""
    found: list[tuple[Path, str]] = []
    for root, label_root in ((PACKAGE_ROOT, REPO_ROOT), (ADAPTER_ROOT, REPO_ROOT)):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(label_root)
            if any(part in _SKIP_DIRS for part in relative.parts):
                continue
            found.append((path, relative.as_posix()))
    return found


def _imports(tree: ast.AST) -> list[tuple[str, int]]:
    """收集所有 import 的顶层模块名与行号。"""
    collected: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            collected.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            collected.append((node.module, node.lineno))
    return collected


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# ------------------------------------------------------------------ 依赖方向

def test_core_modules_do_not_import_ui_layer() -> None:
    """Core services must remain usable without importing Qt or UI modules."""
    violations: list[str] = []
    for path, label in _iter_project_python():
        if not label.startswith("voxsub/"):
            continue
        relative = path.relative_to(PACKAGE_ROOT)
        if relative.parts[0] == "ui":
            continue
        for module, lineno in _imports(_parse(path)):
            if module == "voxsub.ui" or module.startswith("voxsub.ui."):
                violations.append(f"{relative}:{lineno} imports {module}")

    assert not violations, "Core -> UI dependency violations:\n" + "\n".join(violations)


def test_core_package_never_depends_on_the_adapter_layer() -> None:
    """依赖方向只能单向：``frontend/backend`` → ``voxsub``。

    反过来（核心包 import 协议适配层）会让核心无法在测试与打包里独立使用，
    也是"业务逻辑爬进适配层"的典型征兆。
    """
    violations: list[str] = []
    for path, label in _iter_project_python():
        if not label.startswith("voxsub/"):
            continue
        for module, lineno in _imports(_parse(path)):
            top = module.split(".")[0]
            if top in FORBIDDEN_CORE_IMPORTS:
                violations.append(f"{label}:{lineno} imports {module}")

    assert not violations, "Core -> adapter dependency violations:\n" + "\n".join(violations)


def test_adapter_layer_does_not_pull_qt_back_in() -> None:
    """sidecar 只做计算、不画界面；把 Qt 拉回来会让包无缘无故大一百多 MB。"""
    violations: list[str] = []
    for path, label in _iter_project_python():
        if not label.startswith("frontend/backend/"):
            continue
        for module, lineno in _imports(_parse(path)):
            top = module.split(".")[0]
            if top in FORBIDDEN_ADAPTER_IMPORTS:
                violations.append(f"{label}:{lineno} imports {module}")

    assert not violations, "Adapter -> Qt dependency violations:\n" + "\n".join(violations)


# ------------------------------------------------------------------ 复杂度棘轮

def _complexity_scores() -> dict[str, int]:
    """算出所有函数的分支复杂度，键为 ``相对路径:函数名``。"""
    branch_nodes = (
        ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try,
        ast.BoolOp, ast.IfExp, ast.Match,
    )
    scores: dict[str, int] = {}
    for path, label in _iter_project_python():
        short = label.split("frontend/backend/")[-1] if "/" in label else label
        short = short.replace("voxsub/", "") if label.startswith("voxsub/") else short
        for node in ast.walk(_parse(path)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            score = 1 + sum(
                isinstance(child, branch_nodes)
                for child in ast.walk(node)
                if child is not node
            )
            scores[f"{short}:{node.name}"] = score
    return scores


def test_production_functions_stay_below_complexity_budget() -> None:
    """Force new branch-heavy orchestration to be split before it lands.

    适配层与核心层都算 —— 原门禁只扫核心层，于是编排逻辑全堆到适配层躲过检查。
    """
    violations: list[str] = []
    for name, score in sorted(_complexity_scores().items()):
        if score < COMPLEXITY_BUDGET:
            continue
        ceiling = COMPLEXITY_BASELINE.get(name)
        if ceiling is not None and score <= ceiling:
            continue
        if ceiling is not None:
            violations.append(f"{name} score={score} 比基线 {ceiling} 更差")
        else:
            violations.append(f"{name} score={score}")

    assert not violations, "Functions exceed complexity budget:\n" + "\n".join(violations)


def test_complexity_baseline_only_shrinks() -> None:
    """基线表不该留下已经修好的条目 —— 只允许变短。"""
    scores = _complexity_scores()
    stale = [
        f"{name} (基线 {ceiling}，实际 {scores.get(name, '已不存在')})"
        for name, ceiling in COMPLEXITY_BASELINE.items()
        if scores.get(name, 0) < ceiling
    ]
    assert not stale, "棘轮基线里有已消化/过时的条目，请从 COMPLEXITY_BASELINE 删掉:\n" + "\n".join(stale)


# ------------------------------------------------------------------ 资源与队列

def test_production_queues_have_explicit_capacity() -> None:
    """An implicit Queue() is unbounded and can hide a stalled consumer."""
    violations: list[str] = []
    for path, label in _iter_project_python():
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            is_queue = (
                isinstance(function, ast.Attribute) and
                isinstance(function.value, ast.Name) and
                function.value.id == "queue" and function.attr == "Queue"
            )
            if not is_queue:
                continue
            has_capacity = bool(node.args) or any(
                keyword.arg == "maxsize" for keyword in node.keywords)
            if not has_capacity:
                violations.append(f"{label}:{node.lineno}")

    assert not violations, "Unbounded production queues:\n" + "\n".join(violations)


# ------------------------------------------------------------------ 测试卫生

def test_single_test_entry_point_and_isolated_basetemp() -> None:
    """测试临时目录只有一个固定位置，且被 gitignore 覆盖。

    历史问题：仓库根堆了 240+ 个 ``.pytest-*``（500MB+），因为大家各传各的
    ``--basetemp``。统一入口 + gitignore 把这个口子堵上 —— 靠"记得别传"
    是堵不住的。
    """
    runner = REPO_ROOT / "scripts" / "run_tests.py"
    assert runner.is_file(), "缺少唯一测试入口 scripts/run_tests.py"

    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".pytest-run" in ignore, "统一 basetemp 必须在 gitignore 里"

    body = runner.read_text(encoding="utf-8")
    assert "--basetemp" in body, "入口脚本必须固定 basetemp"
    assert "shutil.rmtree" in body, "入口脚本必须在每次运行前清空自己的临时目录"


# ------------------------------------------------------------------ 子进程解码

def test_subprocess_text_mode_always_declares_its_encoding() -> None:
    """``text=True`` 必须同时声明 ``encoding=``。

    回归背景：中文 Windows 上 ``pnputil`` / ``powershell`` / ``nvidia-smi``
    输出的是 CP936，而 ``text=True`` 让 subprocess 按 UTF-8 解码 —— 解码异常
    发生在**读取线程**里，``stdout`` 直接变成空串。表现是"机器上明明有 NPU，
    探测却什么都读不到"，主流程完全看不到异常。这类坑必须机器拦住，
    不能靠人记得。
    """
    violations: list[str] = []
    for path, label in _iter_project_python():
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not isinstance(function, ast.Attribute):
                continue
            is_subprocess = (
                isinstance(function.value, ast.Name) and function.value.id == "subprocess"
            )
            if not is_subprocess:
                continue
            keywords = {keyword.arg for keyword in node.keywords}
            wants_text = (
                any(isinstance(arg, ast.Constant) and arg.value is True
                    for arg in node.args)
                or "text" in keywords or "universal_newlines" in keywords
            )
            if wants_text and "encoding" not in keywords:
                violations.append(f"{label}:{node.lineno} {function.attr}(text=True)")
            if "errors" in keywords and "encoding" not in keywords:
                violations.append(f"{label}:{node.lineno} {function.attr}(errors=)")

    assert not violations, (
        "subprocess 文本模式缺少 encoding 声明（中文 Windows 上会静默拿到空输出）:\n"
        + "\n".join(violations)
    )
