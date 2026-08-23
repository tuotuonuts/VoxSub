"""Small dependency rules that keep package layers from growing together."""
from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "voxsub"


def test_core_modules_do_not_import_ui_layer() -> None:
    """Core services must remain usable without importing Qt or UI modules."""
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        relative = path.relative_to(PACKAGE_ROOT)
        if relative.parts[0] == "ui":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            for module in imported:
                if module == "voxsub.ui" or module.startswith("voxsub.ui."):
                    violations.append(f"{relative}:{node.lineno} imports {module}")

    assert not violations, "Core -> UI dependency violations:\n" + "\n".join(violations)


def test_production_functions_stay_below_complexity_budget() -> None:
    """Force new branch-heavy orchestration to be split before it lands."""
    branch_nodes = (
        ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try,
        ast.BoolOp, ast.IfExp, ast.Match,
    )
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            score = 1 + sum(
                isinstance(child, branch_nodes)
                for child in ast.walk(node)
                if child is not node
            )
            if score >= 15:
                relative = path.relative_to(PACKAGE_ROOT)
                violations.append(f"{relative}:{node.lineno} {node.name} score={score}")
    assert not violations, "Functions exceed complexity budget:\n" + "\n".join(violations)


def test_production_queues_have_explicit_capacity() -> None:
    """An implicit Queue() is unbounded and can hide a stalled consumer."""
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
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
                relative = path.relative_to(PACKAGE_ROOT)
                violations.append(f"{relative}:{node.lineno}")
    assert not violations, "Unbounded production queues:\n" + "\n".join(violations)
