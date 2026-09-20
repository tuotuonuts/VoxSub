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

#: 基线允许的条目数上限。**新增条目必须同时改这个数字**。
#:
#: 棘轮只强制"登记"，不强制"正当性" —— 理论上新写一个复杂度 40 的函数、顺手在
#: COMPLEXITY_BASELINE 里加一行，两道棘轮测试都会绿（因为 ceiling 必须等于实测、
#: 而且没有上界）。这个上限就是那道额外的编辑动作：它不会阻止新增，但会让 review
#: 看见"有人往棘轮里加了东西"并追问为什么。**只允许调小，调大等于放宽门禁。**
COMPLEXITY_BASELINE_SIZE_CAP = 8

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


def test_complexity_baseline_count_only_shrinks() -> None:
    """基线条目数不许增长 —— 否则棘轮会变成"允许超标清单"。

    审查指出：`test_complexity_baseline_only_shrinks` 只要求 `actual >= ceiling`，
    于是新写一个超复杂函数 + 顺手加一行基线，两道测试都会绿。这条把条目数也钉住。
    """
    assert len(COMPLEXITY_BASELINE) <= COMPLEXITY_BASELINE_SIZE_CAP, (
        f"基线条目数 {len(COMPLEXITY_BASELINE)} 超过上限 {COMPLEXITY_BASELINE_SIZE_CAP} —— "
        "新增超标函数必须先把别的拆掉，或者明确说明为什么这次例外（并同步把上限**调小**，"
        "不是调大）"
    )


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


def test_adapter_scan_set_is_never_empty() -> None:
    """四条适配层门禁共用一个扫描函数 —— 它返回空集时它们会**全部静默通过**。

    这正是缺陷 #13 要防的"扫不到就以为没问题"（旧门禁只扫 `voxsub/`，适配层完全
    在检查之外，却一直是绿的）。所以单独一条断言把扫描集钉住：目录改名/移动后
    会立刻红，而不是变成四条恒绿的空壳。
    """
    scanned = _iter_project_python()
    labels = {label for _path, label in scanned}
    assert any(label.startswith("frontend/backend/") for label in labels), (
        f"适配层扫描集为空 —— 四条适配层规则其实什么都没查。实际标签示例：{sorted(labels)[:5]}"
    )
    assert any(label.startswith("voxsub/") for label in labels), (
        "核心包扫描集为空 —— 核心侧规则其实什么都没查"
    )
    # 打包产物与缓存不该被算进代码门禁
    assert not any("dist/" in label or "__pycache__" in label for label in labels), (
        "扫描集混进了 dist/__pycache__，门禁结果会被第三方代码噪声淹没"
    )


# ------------------------------------------------------------------ 原子发布

#: 只允许这些文件自己调 ``os.replace`` / ``Path.replace``。
#:
#: · ``voxsub/file_io.py`` —— 共享实现本身；
#: · ``frontend/backend/legacy_migration.py`` —— 它要支持**独立脚本运行**
#:   （``python backend/legacy_migration.py``，此时 voxsub 不在 sys.path 上），
#:   所以共享导入失败时得有一个就地兜底。除这个兜底分支外，该文件都走共享实现。
_ATOMIC_PUBLISH_OWNERS = frozenset({
    "voxsub/file_io.py",
    "frontend/backend/legacy_migration.py",
})


def _atomic_publish_offenders() -> list[str]:
    """找出绕过共享原子发布的调用点。

    覆盖**两种等价形态**：

      · ``os.replace(a, b)`` / ``os.rename(a, b)``
      · ``a.replace(b)`` / ``a.rename(b)`` —— ``Path.replace`` / ``Path.rename``

    为什么必须覆盖第二种：第一版门禁只匹配 ``os.replace``，于是仓库里
    ``downloader.py`` 的 ``self.part.replace(self.destination)``、
    ``model_catalog.py`` 的 ``download.replace(final)``、
    ``handlers/migration.py`` 的 ``source.rename(target)`` 全部**通过门禁**，
    而它们与 ``os.replace`` 走同一个 WinAPI、同样会偶发 ``[WinError 5]``。
    门禁给了"已消除"的错觉 —— 这比没有门禁更糟。

    怎么区分 ``Path.replace`` 与 ``str.replace``：``str.replace`` 必须有
    两个参数，``Path.replace`` 只有一个。所以"恰好一个位置参数、无关键字"
    就是 Path 形态，误报率极低。
    """
    offenders: list[str] = []
    for path, label in _iter_project_python():
        if label in _ATOMIC_PUBLISH_OWNERS:
            continue
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            # os.replace / os.rename
            if (func.attr in ("replace", "rename")
                    and isinstance(func.value, ast.Name) and func.value.id == "os"):
                offenders.append(f"{label}:{node.lineno} os.{func.attr}(...)")
                continue
            # Path.replace / Path.rename（恰好一个参数）
            if func.attr in ("replace", "rename") and len(node.args) == 1 and not node.keywords:
                offenders.append(f"{label}:{node.lineno} .{func.attr}(1 arg)")
    return offenders


def test_atomic_publish_goes_through_the_shared_helper() -> None:
    """禁止在共享实现之外做原子发布——覆盖 ``os.replace`` 与 ``Path.replace`` 两种形态。

    原因：Windows 上同步盘（本仓库就在 OneDrive 下）与杀毒软件会短暂持有刚写完的
    文件句柄，裸调用会零星抛 ``[WinError 5] 拒绝访问``。全量测试里表现为"跟代码无关
    的随机失败"，是**最消耗排查时间**的一类问题，本项目已经踩过四次
    （file_io 自己、llama_runtime、bootstrap_models、模型下载发布）。

    所以不再靠"记得加重试"：要原子发布就 ``from voxsub.file_io import replace_with_retry``。
    """
    offenders = _atomic_publish_offenders()
    assert not offenders, (
        "这些地方绕过了共享的原子发布实现（Windows 上会偶发 WinError 5，"
        "表现为随机失败）。请改用 voxsub.file_io.replace_with_retry：\n"
        + "\n".join(offenders)
    )


def test_atomic_publish_gate_is_not_vacuous() -> None:
    """反空转守卫：门禁必须**真的能抓到东西**，而不是因为扫不到文件而恒绿。

    做法是拿一份人造源码树验证判定逻辑 —— 如果哪天扫描根改名、或 `_iter_project_python`
    返回空集，四条适配层规则会全部静默通过（正是缺陷 #13 要防的"扫不到就以为没问题"）。
    """
    sample = (
        "import os\n"
        "def publish(a, b):\n"
        "    os.replace(a, b)\n"
        "def publish2(a, b):\n"
        "    a.replace(b)\n"
        "def publish3(a, b):\n"
        "    a.rename(b)\n"
        "def safe(text, a, b):\n"
        "    return text.replace(a, b)  # str.replace：两个参数，不该被抓\n"
    )
    calls: list[str] = []
    for node in ast.walk(ast.parse(sample)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        func = node.func
        if (func.attr in ("replace", "rename")
                and isinstance(func.value, ast.Name) and func.value.id == "os") or \
           (func.attr in ("replace", "rename") and len(node.args) == 1 and not node.keywords):
            calls.append(func.attr)
    assert calls == ["replace", "replace", "rename"], (
        f"判定逻辑漏抓或误抓：{calls} —— 门禁的形态匹配已经失效，别再信它的绿灯"
    )

    # 同时确认当前真实扫描集非空（改了目录名会让四条适配层规则一起失效）
    assert _iter_project_python(), "扫描集为空 —— 适配层的四条门禁其实什么都没查"


# ------------------------------------------------------------------ 测试卫生

def test_single_test_entry_point_and_isolated_basetemp() -> None:
    """测试临时目录只有一个固定位置，且被 gitignore 覆盖。

    历史问题：仓库根堆了 240+ 个 ``.pytest-*``（500MB+），因为大家各传各的
    ``--basetemp``。统一入口 + gitignore 把这个口子堵上 —— 靠"记得别传"
    是堵不住的。

    另一半同样重要：入口**每次运行要用自己的子目录**。曾经把它写成固定的
    ``.pytest-run``，结果并发跑两次时后启动的那次会 rmtree 掉前一次正在用的
    tmp_path，冒出一批与代码无关的 fixture 报错（实测一次全量里 14 个 ERROR，
    单独跑全绿）。
    """
    runner = REPO_ROOT / "scripts" / "run_tests.py"
    assert runner.is_file(), "缺少唯一测试入口 scripts/run_tests.py"

    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".pytest-run" in ignore, "统一 basetemp 必须在 gitignore 里"

    body = runner.read_text(encoding="utf-8")
    assert "--basetemp" in body, "入口脚本必须固定 basetemp"
    assert "shutil.rmtree" in body, "入口脚本必须能清理自己的临时目录"
    # 每次运行独占一个子目录 —— 否则并发跑会互相踩（见上面那段实测）
    assert "os.getpid()" in body and "_own_basetemp" in body, (
        "basetemp 必须带本次运行的标识（pid + 时间戳），否则并发跑两次会互相清掉"
    )


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
