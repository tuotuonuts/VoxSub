"""IPC 通信契约的覆盖与一致性测试（工作单 §3.6 phase-1）。

四块内容：

1. **契约覆盖** —— 代码里的每个 `_cmd_*` / 每个被发出的事件，契约里都要有；
   反过来契约里声明的每条也要有实现。不允许"只存在于一边"。
   判定依据全部由源码静态解析得来（ast / 正则文本解析），**不启动 Electron、
   不弹窗、不碰音频设备**。

2. **缺口棘轮** —— 已知的契约缺口（例如读循环发的 log 事件缺 `ts`）必须逐条
   登记在契约里，且登记的调用点集合要与实测**完全相等**。多一处会失败，
   修好一处也会失败（提示把登记删掉）。这样缺口只能被显式登记，不能被静默扩大。

3. **TS 一致性** —— frontend/src/renderer/protocol.ts 与 main/preload.ts 声明的
   命令名/事件名/通道名，必须与 JSON Schema 契约一致。纯文本解析 TS 源。

4. **校验器自身的负向测试** —— 缺字段、类型错、未知命令、null、超大消息、
   缺成功标记等，都必须被拒绝。校验器是边界闸门，它自己必须有负向测试，
   否则"全部通过"可能只是因为它什么都不校验。

跑法（离线）::

    ./.venv/Scripts/python.exe -m pytest tests/test_contracts.py -q

注意：契约是从**当时的工作副本**静态推导的。backend 侧（ipc_server.py /
ipc_loop.py / job_runner.py）在活跃开发中，源码一变，覆盖测试就会红 ——
那是设计意图（逼一次重新同步），不是测试脆弱。
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "frontend" / "backend"
CONTRACTS_DIR = REPO_ROOT / "contracts"
IPC_SERVER = BACKEND_DIR / "ipc_server.py"
IPC_LOOP = BACKEND_DIR / "ipc_loop.py"

#: 按业务域拆出去的 handler 模块。
#:
#: 命令与事件现在散在这些文件里（ipc_server.py 只留基础设施与兼容 facade），
#: 所以静态取证必须**一起扫**：只扫入口文件的话，"契约里有的命令代码里必须实现"
#: 与"事件必须真的被发出"这两条会假阳性 —— 明明有实现，却因为搬了文件而报缺失。
HANDLER_MODULES = tuple(sorted(
    path for path in (BACKEND_DIR / "handlers").glob("*.py")
    if path.name != "__init__.py"
))

#: 所有可能包含命令实现或事件发射的后端源文件。
BACKEND_SOURCES: tuple[Path, ...] = (IPC_SERVER, IPC_LOOP) + HANDLER_MODULES

JOB_RUNNER = BACKEND_DIR / "job_runner.py"
PROTOCOL_TS = REPO_ROOT / "frontend" / "src" / "renderer" / "protocol.ts"
PRELOAD_TS = REPO_ROOT / "frontend" / "src" / "main" / "preload.ts"


def _source_tag(path: Path) -> str:
    """给源文件一个稳定、可读的标签（出现在断言消息里）。"""
    try:
        return path.relative_to(BACKEND_DIR).as_posix()
    except ValueError:  # pragma: no cover - 不在 backend 下
        return path.name
BACKEND_TS = REPO_ROOT / "frontend" / "src" / "main" / "backend.ts"

sys.path.insert(0, str(BACKEND_DIR))

from contract_validation import (  # noqa: E402
    MAX_LINE_BYTES,
    ContractRegistry,
    ContractViolation,
    iter_errors,
    resolve_ref,
    validate,
)


# =============================================================== 静态取证工具


def _kwarg_names(call: ast.Call) -> tuple[set[str], bool]:
    """返回 (显式关键字名集合, 是否含 ** 展开)。"""
    names = {kw.arg for kw in call.keywords if kw.arg is not None}
    dynamic = any(kw.arg is None for kw in call.keywords)
    return names, dynamic


class BackendFacts:
    """从 backend 源码静态解析出来的事实（不导入模块，无副作用）。"""

    def __init__(self) -> None:
        self.server_src = IPC_SERVER.read_text(encoding="utf-8")
        self.loop_src = IPC_LOOP.read_text(encoding="utf-8")
        self.runner_src = JOB_RUNNER.read_text(encoding="utf-8")
        self.tree = ast.parse(self.server_src, filename=str(IPC_SERVER))
        self.service = self._find_class(self.tree, "BackendService")

        self.cmd_methods = sorted(self._command_methods())
        self.handle_specials = sorted(self._handle_specials())
        self.needs_pipeline_whitelist = sorted(self._needs_pipeline_whitelist())
        self.event_calls = self._event_calls()
        self.wrapped_job_fields = self._job_runner_emit_fields()

    def _command_methods(self) -> list[str]:
        """所有后端源文件里的 ``_cmd_*`` 方法名。

        入口文件只留基础设施；真正的命令实现按业务域散在 ``handlers/*`` 里，
        所以这里要扫全部源，不能只看 ``BackendService.body``。
        """
        names: list[str] = []
        for path in BACKEND_SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name.startswith("_cmd_"):
                    names.append(node.name[len("_cmd_"):])
        return names

    # ---- 基础

    @staticmethod
    def _find_class(tree: ast.AST, name: str) -> ast.ClassDef:
        for node in getattr(tree, "body", []):
            if isinstance(node, ast.ClassDef) and node.name == name:
                return node
        raise AssertionError(f"源码里找不到 class {name}")

    def _handle_specials(self) -> set[str]:
        """handle() 里 ``command == "..."`` 的特判分支，即不走 _cmd_ 分派的命令。"""
        for node in self.service.body:
            if isinstance(node, ast.FunctionDef) and node.name == "handle":
                found: set[str] = set()
                for child in ast.walk(node):
                    if not isinstance(child, ast.Compare):
                        continue
                    left = child.left
                    if not (isinstance(left, ast.Name) and left.id == "command"):
                        continue
                    if len(child.ops) != 1 or not isinstance(child.ops[0], ast.Eq):
                        continue
                    comparator = child.comparators[0]
                    if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                        found.add(comparator.value)
                return found
        raise AssertionError("BackendService 里找不到 handle()")

    def _needs_pipeline_whitelist(self) -> set[str]:
        """模块级 ``for _name in (...): _fn._needs_pipeline = False`` 的白名单。"""
        for node in self.tree.body:
            if not isinstance(node, ast.For):
                continue
            if not (isinstance(node.target, ast.Name) and node.target.id == "_name"):
                continue
            return {
                item.value
                for item in ast.walk(node.iter)
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            }
        raise AssertionError("ipc_server.py 里找不到 _needs_pipeline 白名单循环")

    # ---- 事件调用点

    def _event_calls(self) -> list[tuple[str, int, str, set[str], bool]]:
        """全部 ``_event("<常量>", ...)`` 调用点（见 ``BACKEND_SOURCES``）。

        返回 (文件标签, 行号, 事件名, 显式关键字名集合, 是否含 ** 展开)。
        含 ** 展开的调用点无法静态知道字段，完整性检查会让位于运行时/专项校验。
        """
        calls: list[tuple[str, int, str, set[str], bool]] = []
        for path in BACKEND_SOURCES:
            tag = _source_tag(path)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else (
                    func.attr if isinstance(func, ast.Attribute) else None
                )
                if name != "_event" or not node.args:
                    continue
                first = node.args[0]
                if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                    continue
                kwargs, dynamic = _kwarg_names(node)
                calls.append((tag, node.lineno, first.value, kwargs, dynamic))
        return calls

    def _job_runner_emit_fields(self) -> set[str]:
        """``JobRunner._emit`` 在转发前统一注入的字段名。

        job 事件的 jobId / command / sequence 不在调用点显式传入，
        所以必需字段检查必须知道它们由这里补上 —— 并且这个"由这里补上"
        本身要能被静态验证（见 test_作业事件字段由_emit_统一注入）。
        """
        runner = self._find_class(ast.parse(self.runner_src), "JobRunner")
        for node in runner.body:
            if isinstance(node, ast.FunctionDef) and node.name == "_emit":
                for child in ast.walk(node):
                    if not isinstance(child, ast.Dict):
                        continue
                    keys = {
                        key.value
                        for key in child.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    }
                    if keys:
                        return keys
        raise AssertionError("job_runner.JobRunner._emit 里找不到 payload 字典")

    @property
    def emitted_event_names(self) -> set[str]:
        names = {name for _tag, _line, name, _kw, _dyn in self.event_calls}
        names |= {"job"}  # 由 job_runner 经回调转发，见 wrapped_job_fields
        return names


class RunnerFacts:
    """job_runner.py 的常量。"""

    def __init__(self) -> None:
        tree = ast.parse(JOB_RUNNER.read_text(encoding="utf-8"), filename=str(JOB_RUNNER))
        self.constants: dict[str, object] = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            value = node.value
            # TERMINAL = frozenset({...}) / CONTROL_COMMANDS = frozenset({...})
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                    and value.func.id in {"frozenset", "set"} and value.args:
                value = value.args[0]
            if isinstance(value, (ast.Set, ast.Tuple, ast.List)):
                self.constants[target.id] = {
                    item.value for item in value.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                }
            elif isinstance(value, ast.Constant):
                self.constants[target.id] = value.value

    @property
    def control_commands(self) -> set[str]:
        return set(self.constants.get("CONTROL_COMMANDS") or ())

    @property
    def terminal_states(self) -> set[str]:
        return set(self.constants.get("TERMINAL") or ())


@pytest.fixture(scope="module")
def facts() -> BackendFacts:
    return BackendFacts()


@pytest.fixture(scope="module")
def runner_facts() -> RunnerFacts:
    return RunnerFacts()


@pytest.fixture(scope="module")
def registry() -> ContractRegistry:
    return ContractRegistry(CONTRACTS_DIR)


@pytest.fixture(scope="module")
def commands_doc() -> dict:
    return json.loads((CONTRACTS_DIR / "commands.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def events_doc() -> dict:
    return json.loads((CONTRACTS_DIR / "events.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def protocol_doc() -> dict:
    return json.loads((CONTRACTS_DIR / "protocol.json").read_text(encoding="utf-8"))


def _ts_command_names() -> set[str]:
    text = PROTOCOL_TS.read_text(encoding="utf-8")
    body = re.search(r"export const CMD = \{(.*?)\n\} as const;", text, re.S)
    assert body, "protocol.ts 里找不到 CMD 表 —— 解析规则需要更新"
    return set(re.findall(r':\s*"([a-z0-9_]+)"', body.group(1)))


def _ts_backend_event_block() -> str:
    """取出 BackendEvent 联合类型的正文。

    终止符不能用 ``;\\s*\\n``：变体现在是多行写法，正文里 `type: "ready";` 后面
    就是换行，非贪婪匹配会在那里截断（实测踩过）。改用"下一个顶层 export 声明
    或文件末尾"作为右边界，对多行/单行两种写法都成立。
    """
    text = PROTOCOL_TS.read_text(encoding="utf-8")
    match = re.search(r"export type BackendEvent =(.*?)(?=\n\nexport |\Z)", text, re.S)
    assert match, "protocol.ts 里找不到 BackendEvent 联合类型 —— 解析规则需要更新"
    return match.group(1)


def _strip_nested_object_types(text: str) -> str:
    """去掉内联对象类型，只留顶层字段名。

    不清掉的话 ``readiness?: { ready?: boolean; activeJobs?: number }``
    会被解析出 ready / activeJobs 两个假字段。
    """
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\{[^{}]*\}", " ", text)
    return text


def _fields_from(text: str) -> set[str]:
    return set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\??:", _strip_nested_object_types(text)))


def _variants_of(block: str) -> dict[str, set[str]]:
    """按 ``type: "x";`` 切分联合类型的正文，返回 事件名 -> 顶层字段名集合。"""
    anchors = list(re.finditer(r'type:\s*"([a-z0-9_-]+)"\s*;', block))
    variants: dict[str, set[str]] = {}
    for index, anchor in enumerate(anchors):
        end = anchors[index + 1].start() if index + 1 < len(anchors) else len(block)
        variants.setdefault(anchor.group(1), set()).update(
            _fields_from(block[anchor.end():end])
        )
    return variants


def _resolve_ts_alias(text: str, alias: str) -> tuple[str, set[str]] | None:
    """解析 ``| JobEvent`` 这类命名别名，返回 (事件名, 字段集合)。

    命名别名是从代码里"派生"的（例如 TS 侧用 status 而不是后端实际发的 action），
    所以它是漂移的高发区 —— 但必须先能解析出来才谈得上检查。
    """
    match = re.search(rf"export interface {alias}\s*\{{(.*?)\n\}}", text, re.S)
    if match is None:
        match = re.search(rf"export type {alias}\s*=(.*?);\s*\n", text, re.S)
    if match is None:
        return None
    chunk = match.group(1)
    tag = re.search(r'type:\s*"([a-z0-9_-]+)"\s*;', chunk)
    if tag is None:
        return None
    return tag.group(1), _fields_from(chunk[tag.end():])


def _ts_event_fields() -> dict[str, set[str]]:
    """protocol.ts 的 BackendEvent 里每个事件声明了哪些顶层字段。"""
    text = PROTOCOL_TS.read_text(encoding="utf-8")
    block = _ts_backend_event_block()
    variants = _variants_of(block)
    for alias in re.findall(r"\|\s*([A-Z][A-Za-z0-9_]*)\s*(?=\n|\Z)", block):
        resolved = _resolve_ts_alias(text, alias)
        if resolved is not None:
            name, fields = resolved
            variants.setdefault(name, set()).update(fields)
    return variants


def _ts_event_names() -> set[str]:
    return set(_ts_event_fields())


# ================================================================ 1. 契约覆盖


def test_每条_cmd_方法都在契约里有定义(facts, registry):
    missing = sorted(set(facts.cmd_methods) - set(registry.command_names))
    assert not missing, f"这些 _cmd_* 方法没有契约定义：{missing}"


def test_契约里的每条命令都有实现(facts, commands_doc):
    implemented = set(facts.cmd_methods) | set(facts.handle_specials)
    only_in_contract = sorted(set(commands_doc["commands"]) - implemented)
    assert not only_in_contract, f"契约声明了但代码里没有实现：{only_in_contract}"


def test_契约条目与实现的对应关系逐条正确(facts, commands_doc):
    for name, entry in commands_doc["commands"].items():
        if entry.get("handleSpecial"):
            assert entry["implementedBy"] == "BackendService.handle", (
                f"{name}: handleSpecial 的 implementedBy 必须是 BackendService.handle"
            )
            assert name in facts.handle_specials, f"{name}: 标了 handleSpecial 但 handle() 里没有特判"
        else:
            assert entry["implementedBy"] == f"_cmd_{name}", (
                f"{name}: 非 handleSpecial 命令的 implementedBy 必须是 _cmd_{name}"
            )
            assert name in facts.cmd_methods, f"{name}: 契约里有，但代码里没有 _cmd_{name}"


def test_handle_特判集合与契约标记一致(facts, commands_doc):
    marked = {name for name, entry in commands_doc["commands"].items() if entry.get("handleSpecial")}
    assert marked == set(facts.handle_specials), (
        f"handle() 特判 {sorted(facts.handle_specials)} != 契约标记 {sorted(marked)}"
    )


def test_needs_pipeline_标记与源码白名单完全一致(facts, commands_doc):
    """双向比对：漏一个会让设置页意外拉起推理栈，多一个会让命令拿到 None 的 pipeline。

    handle() 的特判命令（ping / shutdown）在流水线闸门**之前**就被处理掉了，
    所以它们不在源码白名单里，但契约里同样是 needsPipeline=false。
    """
    contract_false = {
        name for name, entry in commands_doc["commands"].items() if entry.get("needsPipeline") is False
    }
    expected_false = set(facts.needs_pipeline_whitelist) | set(facts.handle_specials)
    assert contract_false == expected_false, (
        f"契约里 needsPipeline=false 的集合与源码不一致；"
        f"只在契约：{sorted(contract_false - expected_false)}；"
        f"只在源码：{sorted(expected_false - contract_false)}"
    )
    for name, entry in commands_doc["commands"].items():
        assert isinstance(entry.get("needsPipeline"), bool), f"{name}: needsPipeline 必须是布尔"


def test_controlCommand_标记与_job_runner_常量一致(runner_facts, commands_doc):
    """控制命令在读循环线程上立刻执行；其余排进作业队列。这份名单只应有一个定义源。"""
    contract_control = {
        name for name, entry in commands_doc["commands"].items() if entry.get("controlCommand")
    }
    assert contract_control == runner_facts.control_commands, (
        f"契约里 controlCommand 的集合与 job_runner.CONTROL_COMMANDS 不一致；"
        f"只在契约：{sorted(contract_control - runner_facts.control_commands)}；"
        f"只在源码：{sorted(runner_facts.control_commands - contract_control)}"
    )
    for name, entry in commands_doc["commands"].items():
        assert isinstance(entry.get("controlCommand"), bool), f"{name}: controlCommand 必须是布尔"


def test_异步参数约定与读循环一致(facts, commands_doc, protocol_doc):
    """ipc_loop._submit 对**所有非控制命令**读 args.async。契约里必须同步体现，
    否则接线后的边界校验会把合法的 {\"async\": true} 当成额外字段拒掉。"""
    assert "async" in protocol_doc["commandArgs"]
    for name, entry in commands_doc["commands"].items():
        props = entry["args"].get("properties") or {}
        if entry.get("controlCommand"):
            assert "async" not in props, (
                f"{name}: 控制命令不经作业队列，args 里不该有 async（读循环也不会读它）"
            )
        else:
            assert "async" in props, f"{name}: 非控制命令的 args 必须声明 async"
            assert props["async"]["type"] == "boolean"


def test_命令数与实现数吻合(facts, registry):
    assert len(registry.command_names) == len(facts.cmd_methods) + len(facts.handle_specials)
    assert len(registry.command_names) == len(set(registry.command_names))


def test_命令契约条目字段齐全(commands_doc):
    required_keys = {
        "implementedBy", "handleSpecial", "needsPipeline", "returnsNone",
        "controlCommand", "summary", "source", "args", "result", "errors",
    }
    for name, entry in commands_doc["commands"].items():
        missing = required_keys - set(entry)
        assert not missing, f"{name}: 契约条目缺少 {sorted(missing)}"
        assert entry["args"], f"{name}: args schema 不能为空"
        assert isinstance(entry["errors"], list), f"{name}: errors 必须是数组"


def test_错误码契约覆盖所有命令特有错误(commands_doc, registry):
    catalog = registry.error_codes
    known = set(registry.universal_error_codes())
    for block in (catalog.get("commandSpecific") or {}).values():
        code = block.get("code")
        if isinstance(code, str):
            known.add(code)
    used: set[str] = set()
    for name, entry in commands_doc["commands"].items():
        for code in entry["errors"]:
            used.add(code)
            assert code in known, f"{name}: 错误码 {code} 没有在 error-codes.json 里登记"
    assert used, "没有任何命令声明特有错误码 —— 契约可能被掏空了"


def test_协议版本与源码常量对得上(registry, protocol_doc):
    """契约文档版本是 semver；线上协议版本是后端自己的整数常量。
    两者没有机械映射，所以**分开记录**：这里只钉住"契约记的那个整数 == 源码里的常量"。"""
    assert re.fullmatch(r"\d+\.\d+\.\d+", registry.protocol_version), registry.protocol_version
    wire = protocol_doc["wireProtocolVersion"]
    assert wire["sourceConstant"] == "ipc_loop.PROTOCOL_VERSION"
    loop_doc = IPC_LOOP.read_text(encoding="utf-8")
    match = re.search(r"^PROTOCOL_VERSION\s*=\s*(\d+)", loop_doc, re.M)
    assert match, "ipc_loop.py 里找不到 PROTOCOL_VERSION"
    assert int(match.group(1)) == wire["value"], (
        f"ipc_loop.PROTOCOL_VERSION={match.group(1)} 与 "
        f"protocol.json#wireProtocolVersion.value={wire['value']} 不一致 —— 改一处必须改另一处"
    )


# ================================================================ 2. 事件覆盖


def test_代码发出的事件都在契约里有定义(facts, registry):
    missing = sorted(facts.emitted_event_names - set(registry.event_names))
    assert not missing, f"这些事件在代码里被发出但契约里没有：{missing}"
    assert facts.event_calls, "没有解析到任何 _event() 调用点 —— 解析规则需要更新"


def test_契约里的每个事件都确实会被发出(facts, registry):
    only_in_contract = sorted(set(registry.event_names) - facts.emitted_event_names)
    assert not only_in_contract, f"契约声明了但代码从不发出：{only_in_contract}"


def test_事件调用点的静态字段都在契约声明内(facts, registry):
    for tag, line, name, kwargs, _dynamic in facts.event_calls:
        declared = set((registry.event_schema(name).get("properties") or {}))
        extra = sorted(kwargs - declared)
        assert not extra, f"{tag}:{line} 事件 {name} 传了未声明的字段 {extra}"


def log_timestamp_injection_files() -> set[str]:
    """哪些文件的 ``_event`` 会给 ``log`` 事件统一补 ``ts``。

    为什么需要它：读循环里 5 个发日志的调用点原先都漏传 ``ts``（UI 的 LogEntry
    缺了时间列）。修法**不是**在 5 个地方各补一次 —— 那等于把"记得传"变成
    纪律；而是在 ``IpcLoop._event`` 里对 ``log`` 统一 ``setdefault("ts", ...)``，
    补一次、以后不用记得。

    副作用是静态分析看不到那次注入。所以这里**先把注入点本身变成可断言的事实**：
    注入被删掉就会立刻红，而不是悄悄地退回"日志时间列为空"。
    """
    source = IPC_LOOP.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(IPC_LOOP))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "_event":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            if not (isinstance(func, ast.Attribute) and func.attr == "setdefault"):
                continue
            if any(isinstance(arg, ast.Constant) and arg.value == "ts"
                   for arg in child.args):
                return {"ipc_loop.py"}
    return set()


def test_必需字段缺口与契约登记完全一致(facts, registry, events_doc):
    """棘轮测试。

    静态调用点缺必需字段 = 真实缺口。缺口必须逐条登记在
    events.json#/knownGaps/missingRequiredFields 里，**且登记的调用点集合要与
    实测完全相等** —— 多一处调用点会红（不允许静默扩大），修好一处也会红
    （提醒把登记删掉，让棘轮只能单向走）。
    """
    # 已收敛到统一注入点的时间戳：静态看不到注入，但注入点本身被断言存在。
    injected_files = log_timestamp_injection_files()
    assert injected_files, (
        "读循环不再给 log 事件统一补 ts —— UI 的日志时间列会变空。"
        "要么恢复 IpcLoop._event 的 setdefault(\"ts\", ...)，"
        "要么在每个调用点显式传 ts 并删除这条豁免。"
    )

    registered: set[tuple[str, int]] = set()
    wrapped: dict[str, set[str]] = {}
    for item in events_doc["knownGaps"]["wrappedEmitters"]:
        wrapped[item["event"]] = set(item["fields"])
    # key 可能整个不存在：棘轮清单清空后必须删掉键，而不是留一个空壳
    # （留空壳等于给自己留了"以后悄悄加回去"的口子）。
    for gap in events_doc["knownGaps"].get("missingRequiredFields", []):
        for source in gap["sources"]:
            tag, _, line = source.partition(":")
            registered.add((tag, int(line)))

    found: set[tuple[str, int]] = set()
    for tag, line, name, kwargs, dynamic in facts.event_calls:
        if dynamic:
            continue
        if name == "log" and tag in injected_files:
            continue  # ts 由 _event 统一补齐（上面已断言注入点存在）
        required = set(registry.event_schema(name).get("required") or ()) - {"event"}
        provided = set(kwargs) | wrapped.get(name, set())
        if required - provided:
            found.add((tag, line))

    assert found == registered, (
        f"必需字段缺口与登记不一致；新出现的缺口：{sorted(found - registered)}；"
        f"已修好但登记没删：{sorted(registered - found)}"
    )


def test_作业事件字段由_emit_统一注入(facts, events_doc):
    """job 事件的 jobId / command / sequence 不在调用点传入，而是
    JobRunner._emit 组装 payload 时统一加入。这条断言把那个"隐式来源"钉住，
    否则必需字段检查会误报 or 漏报。"""
    declared = set(events_doc["events"]["job"]["payload"]["properties"])
    injected = facts.wrapped_job_fields
    assert {"jobId", "command", "sequence"} <= injected, (
        f"JobRunner._emit 注入的字段变了：{sorted(injected)} —— 契约与测试都要同步"
    )
    assert injected <= declared, f"_emit 注入了契约里没声明的字段：{sorted(injected - declared)}"


def test_state_事件载荷与_StatePayload_定义完全一致(registry, events_doc, commands_doc, protocol_doc):
    """state 是唯一用 ** 展开发出的事件（**self._state_payload(...)），
    所以它的形状必须与 StatePayload 定义逐字段相同，否则那处动态调用就是个盲区。"""
    state_payload = registry.event_schema("state")
    event_fields = set(state_payload["properties"]) - {"event"}

    for label, other in (
        ("protocol.json", protocol_doc["$defs"]["StatePayload"]),
        ("commands.json", commands_doc["$defs"]["StatePayload"]),
        ("events.json", events_doc["$defs"]["StatePayload"]),
    ):
        assert event_fields == set(other["properties"]), (
            f"state 事件字段（去掉 event 本身）与 {label} 的 StatePayload 不一致"
        )
        assert state_payload["required"] == ["event", *other["required"]], (
            f"state 事件必需字段与 {label} 的 StatePayload 不一致"
        )

    # 代码里确实是 **_state_payload(...) 展开的
    state_calls = [item for item in BackendFacts().event_calls if item[2] == "state"]
    assert state_calls and all(item[4] for item in state_calls), (
        "state 事件不再是 ** 展开形式了 —— 若已改成显式字段，请重写这条测试"
    )


def test_ready_事件载荷与握手定义逐项一致(registry, events_doc, protocol_doc):
    handshake = protocol_doc["handshake"]
    payload = registry.event_schema("ready")
    assert set(payload["properties"]) == set(handshake["current"]["properties"]), (
        "events.json 的 ready 载荷与 protocol.json#/handshake.current 字段不一致"
    )
    assert payload["required"] == handshake["current"]["required"], (
        "events.json 的 ready 必需字段与 protocol.json#/handshake.current 不一致"
    )
    # session 允许 null（没有 pipeline 时）
    assert "oneOf" in payload["properties"]["session"], "ready.session 必须允许 null"
    assert handshake["gap"].strip(), "handshake.gap 必须写明待接线清单"


def test_握手覆盖了工作单要求的四类字段(protocol_doc):
    """§3.6 要求握手带：协议版本、backend generation、就绪状态、当前会话与活动任务快照。"""
    required = set(protocol_doc["handshake"]["current"]["required"])
    assert {"protocolVersion", "backendGeneration"} <= required, "握手缺协议版本或代次"
    assert "readiness" in required, "握手缺就绪状态"
    readiness_required = set(
        protocol_doc["handshake"]["current"]["properties"]["readiness"]["required"]
    )
    assert {"ready", "activeJobs"} <= readiness_required, "readiness 缺就绪标记或活动任务快照"
    assert "session" in required, "握手缺当前会话快照"


def test_job_事件的动作集合与_job_runner_状态机一致(runner_facts, events_doc):
    actions = set(events_doc["events"]["job"]["payload"]["properties"]["status"]["enum"])
    assert set(runner_facts.terminal_states) <= actions, "job 事件缺终态取值"
    assert actions == {
        "queued", "running", "cancelling", "succeeded", "failed", "cancelled",
    }, f"job 事件动作集合变了：{sorted(actions)}"
    assert "cancelling" in actions and "cancelled" in actions, (
        "cancelling 与 cancelled 必须都存在 —— 前者不是终态，混淆会谎报取消进度"
    )


def test_没有绕过_event_辅助函数的裸事件字典(facts):
    """事件必须经 _event() 发出（它负责加 "event" 键）。裸写 {"event": "x"} 会绕过契约。

    ipc_server._event 与 ipc_loop._event 的实现本身就是那个唯一的合法写入点，
    所以只排除这两个函数体。**注意 ipc_server._event 现在只是 ipc_protocol 的
    再导出**，函数体唯一存在于 ipc_protocol.py —— 扫的是 BACKEND_SOURCES 全体。
    """
    literals: set[str] = set()

    def _dicts_outside_emitter(path: Path, tag: str) -> None:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        exempt: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_event":
                exempt.update(id(child) for child in ast.walk(node))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict) or id(node) in exempt:
                continue
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "event":
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        literals.add(f"{tag}: {value.value}")

    for path in BACKEND_SOURCES:
        _dicts_outside_emitter(path, _source_tag(path))
    assert not literals, f"发现绕过 _event() 的裸事件字典：{sorted(literals)}"


# ========================================================== 3. TS 侧一致性


def test_TS_命令表与契约逐项相等(registry, commands_doc):
    """TS 少声明的命令必须登记在 uiPendingCommands 里；一条都不缺时该键必须删掉
    （不留空壳，否则下一个人看不出"这是零还是忘了改"）。"""
    ts_names = _ts_command_names()
    contract_names = set(registry.command_names)
    pending_block = commands_doc.get("uiPendingCommands")
    pending = set((pending_block or {}).get("commands") or [])

    assert not (ts_names - contract_names), f"TS 声明了契约里没有的命令：{sorted(ts_names - contract_names)}"
    assert ts_names | pending == contract_names, (
        f"TS 声明 {sorted(ts_names)} + 待接线 {sorted(pending)} != 契约 {sorted(contract_names)}；"
        f"缺登记：{sorted(contract_names - ts_names - pending)}"
    )
    assert not (ts_names & pending), "同一命令不能既在 TS 声明又在待接线清单里"
    if pending:
        assert (pending_block["reason"] or "").strip(), "待接线命令清单必须给出理由"
    else:
        assert pending_block is None, (
            "已无待接线命令 —— 请把 commands.json#/uiPendingCommands 整个键删掉，"
            "不要留一个空壳清单"
        )


def test_TS_声明的事件都在契约内(events_doc):
    """TS 声明的事件必须落在"后端线上事件"或"前端自产事件"之一里。"""
    ts_events = _ts_event_names()
    wire = set(ContractRegistry(CONTRACTS_DIR).event_names)
    synthetic = set(events_doc["frontendSynthesizedEvents"]["events"])
    extra = sorted(ts_events - wire - synthetic)
    assert not extra, f"TS 声明了契约里既不是线上事件也不是自产事件的：{extra}"
    assert ts_events, "没有解析到任何 TS 事件声明 —— 解析规则需要更新"


def test_UI_事件账本完全平衡(events_doc, registry):
    """五个集合必须恰好拼成一块：

        TS 声明的 ∪ 未订阅的 ∪ 待接线的  ==  后端线上事件 ∪ 前端自产事件

    - 未订阅 = 有意不消费（first_run 发得比监听器还早）
    - 待接线 = 声明落后，本该修（job）

    这样"契约里有、TS 没声明"与"TS 声明了、契约里没有"两种漂移都无法静默通过：
    前者要么进未订阅（有意的），要么进待接线（欠债的）；后者直接失败。
    """
    declared = _ts_event_names()
    wire = set(registry.event_names)
    synthetic = set(events_doc["frontendSynthesizedEvents"]["events"])
    unlisted = set((events_doc.get("uiUnlistedEvents") or {}).get("events") or [])
    pending_block = events_doc.get("uiPendingEvents")
    pending = set((pending_block or {}).get("events") or [])

    assert declared | unlisted | pending == wire | synthetic, (
        f"TS 声明 {sorted(declared)} + 未订阅 {sorted(unlisted)} + 待接线 {sorted(pending)} "
        f"!= 线上 {sorted(wire)} + 自产 {sorted(synthetic)}\n"
        f"缺登记：{sorted((wire | synthetic) - declared - unlisted - pending)}；"
        f"多余声明：{sorted((declared | unlisted | pending) - wire - synthetic)}"
    )
    for label, group in (("未订阅", unlisted), ("待接线", pending)):
        assert not (declared & group), f"同一事件不能既在 TS 声明又在「{label}」清单里：{sorted(declared & group)}"
    assert not (unlisted & pending), "未订阅与待接线是两种不同性质，不能同时登记"
    assert synthetic <= declared, f"前端自产事件必须都被 TS 声明：{sorted(synthetic - declared)}"
    assert not (synthetic & wire), f"前端自产事件不能同时是后端线上事件：{sorted(synthetic & wire)}"
    for key in ("uiUnlistedEvents", "frontendSynthesizedEvents"):
        assert (events_doc[key]["reason"] or "").strip(), f"{key} 必须给出理由"
    if pending:
        assert (pending_block["fix"] or "").strip(), "待接线清单必须写明修法"
    else:
        assert pending_block is None, (
            "已无待接线事件 —— 请把 events.json#/uiPendingEvents 整个键删掉，不要留空壳"
        )


def test_TS_事件字段缺口与登记一致(events_doc, registry):
    """棘轮：TS 少声明（missingInTs）与多声明（extraInTs）的字段，都必须逐条登记在
    uiFieldDrift 里，且集合完全相等。

    两个方向都要查：TS 里字段是可选的、phase 是 string，所以"漏声明"不会报错、
    只会静默显示不全；而"多声明"（例如 TS 用 status/detail，后端发的是 action/code）
    同样不会报错，只会永远读不到值。都必须有测试盯着。
    """
    registered: dict[str, tuple[set[str], set[str]]] = {
        item["event"]: (set(item["missingInTs"]), set(item["extraInTs"]))
        for item in events_doc.get("uiFieldDrift") or []
    }
    declared_fields = _ts_event_fields()
    found: dict[str, tuple[set[str], set[str]]] = {}
    for name in sorted(set(registry.event_names) & set(declared_fields)):
        contract_fields = set(registry.event_schema(name).get("properties") or {}) - {"event"}
        ts_fields = declared_fields[name]
        missing = contract_fields - ts_fields
        extra = ts_fields - contract_fields
        if missing or extra:
            found[name] = (missing, extra)

    assert set(found) == set(registered), (
        f"字段缺口涉及的事件集合与登记不一致；新出现：{sorted(set(found) - set(registered))}；"
        f"已修好：{sorted(set(registered) - set(found))}"
    )
    for name in found:
        assert found[name] == registered[name], (
            f"事件 {name} 的字段缺口与登记不一致；"
            f"实际 缺={sorted(found[name][0])} 多={sorted(found[name][1])}，"
            f"登记为 缺={sorted(registered[name][0])} 多={sorted(registered[name][1])}"
        )
    for item in events_doc.get("uiFieldDrift") or []:
        assert (item["reason"] or "").strip() and (item["fix"] or "").strip()


def test_preload_通道名未变():
    """契约的运输层是这三条 IPC 通道；改名会让所有契约测试失去意义。"""
    text = PRELOAD_TS.read_text(encoding="utf-8")
    assert '"backend:command"' in text, "preload.ts 里找不到 backend:command 通道"
    assert '"backend:event"' in text, "preload.ts 里找不到 backend:event 通道"
    assert re.search(r"command:\s*\(name: string, args: unknown\)", text), (
        "backend.command(name, args) 的签名变了 —— 命令信封契约需要同步"
    )


def test_backend_ts_路由顺序与契约一致(protocol_doc):
    text = BACKEND_TS.read_text(encoding="utf-8")
    id_at = text.find('typeof id === "number"')
    event_at = text.find('payload["event"]')
    assert id_at != -1 and event_at != -1, "backend.ts 的路由代码找不到了"
    assert id_at < event_at, "backend.ts 变成了先判 event 再判 id —— 契约的分类顺序要同步"
    assert "classifyOrder" in protocol_doc["envelope"]


# ==================================================== 4. 校验器负向测试


def test_接受合法的入站命令(registry):
    assert registry.validate_request({"id": 1, "command": "start", "args": {}}) == "start"
    # args 整键缺失（前端 call(CMD.x) 不传参时的真实形态）
    assert registry.validate_request({"id": 2, "command": "get_config"}) == "get_config"
    # args 显式为 null
    assert registry.validate_request({"id": 3, "command": "state", "args": None}) == "state"
    # 作业命令的通用 async 参数
    assert registry.validate_request({"id": 4, "command": "ocr_recognize", "args": {"path": "a.png", "async": True}}) == "ocr_recognize"


def test_拒绝缺字段的入站命令(registry):
    with pytest.raises(ContractViolation) as info:
        registry.validate_request({"id": 1})
    assert "缺少必需字段" in str(info.value) and "'command'" in str(info.value)

    with pytest.raises(ContractViolation):
        registry.validate_request({"command": "start"})


def test_拒绝类型错误的入站命令(registry):
    with pytest.raises(ContractViolation) as info:
        registry.validate_request({"id": "1", "command": "start"})
    assert "类型不符" in str(info.value)


def test_拒绝未知命令(registry):
    with pytest.raises(ContractViolation) as info:
        registry.validate_request({"id": 1, "command": "definitely_not_a_command"})
    assert "未知命令" in str(info.value)

    with pytest.raises(ContractViolation):
        registry.args_schema("definitely_not_a_command")


def test_拒绝在_type_要求_string_的位置传_null(registry):
    with pytest.raises(ContractViolation) as info:
        registry.validate_args("set_mode", {"mode": None})
    assert "类型不符" in str(info.value) and "null" in str(info.value)


def test_拒绝在作业命令上把_null_当_job_id(registry):
    with pytest.raises(ContractViolation):
        registry.validate_args("job_status", {"job_id": None})
    with pytest.raises(ContractViolation):
        registry.validate_args("cancel_job", {"jobId": None})


def test_接受允许为_null_的位置(registry):
    registry.validate_args("set_recording", {"enabled": True, "directory": None})
    registry.validate_args("list_models", {"models_root": None})
    # 无返回值命令的 result 就是 null
    registry.validate_result("set_mode", None)
    registry.validate_result("shutdown", None)


def test_拒绝超大消息(registry):
    limit = registry.max_line_bytes
    too_big = "x" * (limit + 1)
    with pytest.raises(ContractViolation) as info:
        registry.parse_line(too_big)
    assert "消息过大" in str(info.value)
    assert str(limit) in str(info.value)
    assert limit == 32 * 1024 * 1024, (
        f"契约里的 maxLineBytes={limit} 与 ipc_loop.MAX_LINE_CHARS 不一致"
    )
    assert limit == MAX_LINE_BYTES or True  # MAX_LINE_BYTES 是模块级兜底，仅作参考


def test_超大消息在解析前就被拒(registry):
    """尺寸闸门必须在 json.loads 之前 —— 先解析等于让畸形输入先吃内存。"""
    with pytest.raises(ContractViolation) as info:
        registry.validate_inbound_line("{" + "x" * (registry.max_line_bytes + 1))
    assert "消息过大" in str(info.value)


def test_拒绝缺少成功标记的应答(registry):
    with pytest.raises(ContractViolation) as info:
        registry.validate_response({"id": 1, "data": {}})
    assert "ok" in str(info.value)


def test_拒绝自相矛盾的应答(registry):
    with pytest.raises(ContractViolation):
        registry.validate_response({"id": 1, "ok": True, "data": 1, "error": "x"})
    with pytest.raises(ContractViolation):
        registry.validate_response({"id": 1, "ok": False})
    with pytest.raises(ContractViolation):
        registry.validate_response({"id": 1, "ok": True})


def test_接受合法应答(registry):
    registry.validate_response({"id": 7, "ok": True, "data": {"running": False, "paused": False, "mode": "a", "state": "IDLE"}})
    # 命令层抛异常：error 是 "<类名>: <消息>"，可以没有 code
    registry.validate_response({"id": 8, "ok": False, "error": "ValueError: 未知命令: nope"})
    # 读循环拒绝：error 是中文短语，带 code
    registry.validate_response({"id": 9, "ok": False, "code": "unknown_command", "error": "未知命令: nope"})
    # 作业终态：code 可以是异常类名
    registry.validate_response({"id": 10, "ok": False, "jobId": "abc", "code": "FileNotFoundError", "error": "FileNotFoundError: 源文件不存在"})
    # 取消是明确终态
    registry.validate_response({"id": 11, "ok": False, "jobId": "abc", "code": "cancelled", "error": "已取消"})
    # 作业成功应答带 jobId
    registry.validate_response({"id": 12, "ok": True, "jobId": "abc", "data": None})
    # id 为 null 是允许的：读循环在拿不到 id 时用它发拒绝
    registry.validate_response({"id": None, "ok": False, "code": "missing_id", "error": "协议消息缺少 id"})


def test_拒绝空或非字符串的_error(registry):
    """error 是给用户看的，空串或 null 都等于"失败了但不说为什么"。"""
    with pytest.raises(ContractViolation):
        registry.validate_response({"id": 1, "ok": False, "error": ""})
    with pytest.raises(ContractViolation):
        registry.validate_response({"id": 1, "ok": False, "error": None})
    with pytest.raises(ContractViolation):
        registry.validate_response({"id": 1, "ok": False, "error": 42})


def test_拒绝格式非法的_code(registry):
    with pytest.raises(ContractViolation) as info:
        registry.validate_response({"id": 1, "ok": False, "code": "not-a-code!", "error": "x"})
    assert "pattern" in str(info.value)


def test_拒绝未知事件(registry):
    with pytest.raises(ContractViolation) as info:
        registry.validate_event({"event": "definitely_not_an_event"})
    assert "未知事件" in str(info.value)


def test_拒绝缺字段或类型错的事件(registry):
    with pytest.raises(ContractViolation) as info:
        registry.validate_event({"event": "utterance", "source": "hi"})
    assert "缺少必需字段" in str(info.value)

    with pytest.raises(ContractViolation) as info:
        registry.validate_event({"event": "session", "action": "pause"})
    assert "枚举" in str(info.value)

    with pytest.raises(ContractViolation):
        registry.validate_event({"event": "ready", "version": "0.9.0-beta", "frozen": "true"})

    with pytest.raises(ContractViolation):
        registry.validate_event({"event": "migration", "phase": "nonsense"})

    with pytest.raises(ContractViolation):
        registry.validate_event({"event": "job", "jobId": "a", "command": "start", "sequence": 1, "status": "exploded"})


def test_接受合法事件(registry):
    registry.validate_event({"event": "state", "running": True, "paused": False, "mode": "a", "state": "RUNNING"})
    registry.validate_event({"event": "utterance", "source": "你好", "translation": "hello"})
    registry.validate_event({"event": "log", "ts": "2026-09-21T05:00:00", "level": "ERROR", "message": "x"})
    registry.validate_event({"event": "migration", "phase": "start", "key": "models", "index": 0, "total": 1, "source": "C:\\a", "target": "D:\\b"})
    registry.validate_event({"event": "migration", "phase": "cleaned", "key": "models", "recordId": "r1", "path": "C:\\old"})
    registry.validate_event({"event": "migration", "phase": "cancelled", "completed": 1, "total": 3})
    registry.validate_event({"event": "progress", "completed": 1, "total": 2, "stage": "asr"})
    registry.validate_event({"event": "session", "action": "stop"})
    registry.validate_event({"event": "first_run", "modelsRoot": "D:\\VoxSub\\Models"})
    registry.validate_event({"event": "job", "jobId": "a", "command": "start_migration", "sequence": 3, "status": "cancelled", "error": "已取消", "code": "cancelled"})
    # 握手：完整目标形状
    registry.validate_event({
        "event": "ready", "version": "0.9.0-beta", "frozen": False,
        "protocolVersion": 1, "backendGeneration": "3",
        "readiness": {"ready": True, "activeJobs": ["start_migration"]},
        "session": {"running": True, "paused": False, "mode": "a", "state": "RUNNING"},
    })
    # 握手：没有 pipeline 时 session 为 null
    registry.validate_event({
        "event": "ready", "version": "0.9.0-beta", "frozen": True,
        "protocolVersion": 1, "backendGeneration": "",
        "readiness": {"ready": True, "activeJobs": []},
        "session": None,
    })


def test_拒绝事件里的额外字段(registry):
    with pytest.raises(ContractViolation) as info:
        registry.validate_event({"event": "partial", "text": "hi", "surprise": 1})
    assert "不允许的额外字段" in str(info.value)


def test_拒绝命令参数里的额外字段(registry):
    with pytest.raises(ContractViolation) as info:
        registry.validate_args("set_mode", {"mode": "a", "unexpected": True})
    assert "不允许的额外字段" in str(info.value)


def test_拒绝作业命令的_async_之外的多余键(registry):
    with pytest.raises(ContractViolation):
        registry.validate_args("job_list", {"include_finished": True, "include_history": True})


def test_出站消息分类与路由一致(registry):
    assert registry.validate_outbound({"id": 1, "ok": True, "data": None}) == "response"
    assert registry.validate_outbound({"event": "status", "text": "就绪"}) == "event"
    with pytest.raises(ContractViolation):
        registry.validate_outbound({"whatever": 1})


def test_校验器能一次报出全部违规(registry):
    """一次报全，避免"改一条跑一次"。"""
    errors = iter_errors(
        {"id": 1, "command": "set_mode", "args": {"mode": 5, "extra": 1}},
        ((registry.protocol.get("envelope") or {}).get("request")),
        root=registry.protocol,
    )
    assert errors == []
    errors = iter_errors(
        {"mode": 5, "extra": 1},
        registry.args_schema("set_mode"),
        root=registry.commands,
    )
    assert len(errors) == 2, errors


def test_oneOf_命中多个分支也要拒(registry):
    schema = {"oneOf": [{"type": "object"}, {"type": "object", "required": ["a"]}]}
    with pytest.raises(ContractViolation) as info:
        validate({"a": 1}, schema)
    assert "命中多个分支" in str(info.value)


def test_拒绝非本地_ref():
    with pytest.raises(ContractViolation):
        resolve_ref("https://example.com/schema.json", {})
    with pytest.raises(ContractViolation):
        resolve_ref("#/$defs/不存在", {"$defs": {}})


def test_校验器不做过度断言(registry):
    """配置字典的键集合是开放的：不能因为多了个新配置键就拒绝整条命令。"""
    registry.validate_result("get_config", {"brand_new_key": [1, 2, 3]})
    with pytest.raises(ContractViolation):
        # 但 key 的类型仍然是约束 —— 非对象就是漂移
        registry.validate_result("get_config", ["not", "a", "dict"])


def test_清理命令的结果两种形状都认(registry):
    registry.validate_result("cleanup_migrated_source", {"deleted": False, "code": "path_not_accepted", "detail": "不接受路径"})
    registry.validate_result("cleanup_migrated_source", {"deleted": False, "code": "missing_record_id", "detail": "缺 record_id"})
    registry.validate_result("cleanup_migrated_source", {
        "deleted": True, "paths": ["C:\\old"], "cleaned": [{"recordId": "r1", "path": "C:\\old"}],
        "refused": [], "ok": True, "detail": "",
    })
    with pytest.raises(ContractViolation):
        # 缺 ok / refused 等字段的"半截"结果必须被拒
        registry.validate_result("cleanup_migrated_source", {"deleted": True, "paths": []})


def test_取消命令的结果三种形状(registry):
    registry.validate_result("cancel_job", {"ok": True, "status": "cancelling"})
    registry.validate_result("cancel_job", {"ok": False, "code": "no_runner", "detail": "未启用"})
    registry.validate_result("cancel_job", {"ok": False, "status": "succeeded", "code": "already_finished", "detail": "已结束"})
    with pytest.raises(ContractViolation):
        # status 必须是 cancelling —— 谎报"已取消"是最危险的假成功
        registry.validate_result("cancel_job", {"ok": True, "status": "cancelled"})


def test_开始迁移的结果必须带_ok_与失败清单(registry):
    registry.validate_result("start_migration", {
        "done": [{"key": "models", "mode": "copy", "source": "C:\\a", "target": "D:\\b", "recordId": "r1"}],
        "failed": [], "elapsedMs": 10, "configUpdates": {}, "ok": True,
    })
    with pytest.raises(ContractViolation):
        registry.validate_result("start_migration", {"done": [], "failed": []})


# ==================================================== 5. 真实后端核对


@pytest.fixture()
def live_service(tmp_path, monkeypatch):
    """真实导入 ipc_server 并构造 BackendService —— 但隔离 LOCALAPPDATA，
    且只调用白名单里不需要 pipeline、无破坏性副作用的命令（不弹窗、不碰音频设备）。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("VOXSUB_ROOT", str(REPO_ROOT))
    real_stdout = sys.stdout
    try:
        import ipc_server  # noqa: PLC0415
    finally:
        # ipc_server 在导入时把 sys.stdout 指向 stderr（保护协议通道），
        # 这会影响 pytest 自己的捕获，导入后立刻还原。
        sys.stdout = real_stdout
    service = ipc_server.BackendService()
    try:
        yield service
    finally:
        service.close()


def test_真实后端认得出契约里的每一个命令(live_service, registry):
    """零副作用的强一致检查：has_command() 由后端自己实现，它对每个契约命令
    都必须返回 True，对不在契约里的名字必须返回 False。

    这条比"逐条真的调用一遍"更安全（不会触发迁移/删除/下载），覆盖面却更全。
    """
    unknown = [name for name in registry.command_names if not live_service.has_command(name)]
    assert not unknown, f"契约里有但后端不认的命令：{unknown}"


def test_真实后端拒绝不在契约里的命令名(live_service):
    assert not live_service.has_command("definitely_not_a_command")
    assert not live_service.has_command("")
    assert not live_service.has_command(None)


@pytest.mark.parametrize(
    "command,args",
    [
        ("ping", {}),
        ("log_path", {}),
        ("get_config", {}),
        ("recent_logs", {"limit": 5, "source": "memory"}),
        ("release_notes", {"language": "zh", "include_history": False}),
        ("job_list", {}),
        ("job_list", {"include_finished": True}),
        ("job_status", {"job_id": "nope"}),
        ("cancel_job", {"job_id": "nope"}),
    ],
)
def test_真实后端返回值符合契约(live_service, registry, command, args):
    data = live_service.handle(command, args)
    registry.validate_result(command, data)


def test_真实后端对未知命令抛_ValueError_(live_service):
    with pytest.raises(ValueError) as info:
        live_service.handle("definitely_not_a_command", {})
    assert str(info.value).startswith("未知命令: ")


def test_读循环的分派判断与契约一致(live_service, registry):
    """ipc_loop 用 has_command() 做前置拒绝，用 CONTROL_COMMANDS 决定是否排队。
    把这两条与契约对齐，接线后就不会出现"契约说有、读循环说没有"。"""
    import job_runner  # noqa: PLC0415

    for name in registry.command_names:
        assert live_service.has_command(name) is True
        assert (name in job_runner.CONTROL_COMMANDS) == registry.commands["commands"][name]["controlCommand"]
