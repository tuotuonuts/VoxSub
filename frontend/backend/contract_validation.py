"""VoxSub IPC 契约的轻量运行时校验器 —— 纯标准库实现。

职责：在协议边界上把"契约漂移"变成一条可读的错误，而不是让它下游炸成
AttributeError / KeyError。生产边界负责校验：

  · 入站命令  —— ``IpcLoop._parse_line`` 做结构闸门，``_admissible`` 调用 ``validate_args``；``validate_request`` 是独立完整入口
  · 出站应答  —— ``IpcLoop._send`` 校验信封、命令结果与异步受理回执
  · 出站事件  —— ``IpcLoop._event`` 校验事件载荷；迁移终态报告再复用命令结果契约
  · 契约位置  —— source mode 用仓库 ``contracts/``；PyInstaller sidecar 用 ``sys._MEIPASS/contracts``

实现的是 **JSON Schema draft 2020-12 的子集**，不是完整实现。已支持：

  type  const  enum  required  properties  additionalProperties  patternProperties
  items  minItems  maxItems  uniqueItems  minLength  maxLength
  minimum  maximum  exclusiveMinimum  exclusiveMaximum  multipleOf  pattern
  oneOf  anyOf  allOf  not  $ref（仅本地 ``#/`` JSON 指针）  $defs

刻意忽略的键：``$schema`` ``$id`` ``title`` ``description`` ``default``
``examples`` ``$comment``，以及**任何未列出的键**。忽略未知关键字比猜测其
语义更安全 —— 契约文件里的 protocolVersion / generatedFrom / transport 之类
是给人和测试读的元数据，不该被当成校验规则。

刻意不做的：远程 $ref、$dynamicRef/$dynamicAnchor、unevaluatedProperties、
if/then/else、format 语义校验（只当成注释）、contentEncoding/contentMediaType。

为什么自己写而不用 jsonschema 库：本仓库 requirements.txt 里没有它，为一个
边界校验引入新依赖会同时影响 .venv 与 PyInstaller 打包体积。这里的子集只用了
re/json 两个标准库模块。

依赖：仅 json / re / sys / pathlib / typing。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator

__all__ = [
    "MAX_LINE_BYTES",
    "MAX_DEPTH",
    "ContractViolation",
    "ContractNotFound",
    "iter_errors",
    "validate",
    "is_valid",
    "resolve_ref",
    "ContractRegistry",
    "DEFAULT_CONTRACTS_DIR",
]

# --------------------------------------------------------------------- 常量

def default_contracts_dir() -> Path:
    """Source mode uses the repository tree; PyInstaller uses its collected data root."""
    if bool(getattr(sys, "frozen", False)):
        bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        return bundle_root / "contracts"
    return Path(__file__).resolve().parents[2] / "contracts"


#: Default directory remains exported for older callers; each registry resolves dynamically.
DEFAULT_CONTRACTS_DIR = default_contracts_dir()

#: 单条线上消息的**兜底**上限（字节）。仅当协议文件读不到 `transport.maxLineBytes`
#: 时生效 —— 正常运行时以 `contracts/protocol.json` 的 32 MiB 为准（读循环真正的
#: 上限是 `ipc_loop.MAX_LINE_BYTES`，测试断言两者一致）。
#:
#: 为什么兜底值比正式值小：兜底只在"契约缺失"这种异常态生效，此时宁可更保守 ——
#: 拒掉一条超大消息，也好过在没有任何契约约束的情况下把内存读爆。两个数字不同是
#: **有意**的，但审查指出它会让"上限到底是多少"变得可争议，所以在这里写明关系。
MAX_LINE_BYTES = 8 * 1024 * 1024

#: 递归深度上限，防止畸形契约或自引用 $ref 打爆栈。
MAX_DEPTH = 64

_JSON_TYPE_NAMES = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)

_UNKNOWN = object()  # 属性缺省的哨兵


# --------------------------------------------------------------------- 异常


class ContractViolation(Exception):
    """契约违规。``errors`` 是全部违规点的可读描述（不是只报第一条）。

    一次性报全比逐条报好：接线阶段一次就能看清一条消息违反了哪几处，
    不用"改一条跑一次"。
    """

    def __init__(self, errors: Iterable[str], summary: str = "IPC 契约校验失败") -> None:
        self.errors: list[str] = [str(item) for item in errors]
        super().__init__(summary + "：\n  - " + "\n  - ".join(self.errors))


class ContractNotFound(ContractViolation):
    """契约文件缺失或不可解析 —— 部署问题，不是消息问题。"""


# ----------------------------------------------------------------- 类型判定


def _type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "integer" if value.is_integer() else "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _matches_type(value: Any, expected: str) -> bool:
    actual = _type_of(value)
    if expected == "number":
        # JSON Schema：integer 也是 number
        return actual in ("number", "integer")
    if expected == "integer":
        # 1.0 是整数（draft 2020-12 的 number 语义），True/False 不是
        return actual == "integer"
    return actual == expected


# -------------------------------------------------------------------- $ref


def resolve_ref(ref: str, root: Any) -> Any:
    """解析本地 ``#/`` JSON 指针。非本地 ref（含 ://）一律拒绝。

    只支持本地指针是有意的：跨文件 ref 需要 $id 解析与网络/相对路径规则，
    而那正是本实现刻意不碰的部分。契约文件之间因此不允许互相 $ref ——
    共享形状在各文件内各自 $defs 声明一份，并由测试断言它们一致。
    """
    if not isinstance(ref, str) or not ref.startswith("#"):
        raise ContractViolation([f"不支持的 $ref（只支持本地 #/ 指针）：{ref!r}"])
    pointer = ref[1:]
    if pointer in ("", "/"):
        return root
    if not pointer.startswith("/"):
        raise ContractViolation([f"$ref 不是合法 JSON 指针：{ref!r}"])

    node = root
    for raw_token in pointer.lstrip("/").split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if token not in node:
                raise ContractViolation([f"$ref 指向不存在的节点：{ref!r}"])
            node = node[token]
        elif isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                raise ContractViolation([f"$ref 指向不存在的数组元素：{ref!r}"]) from None
        else:
            raise ContractViolation([f"$ref 穿过非容器节点：{ref!r}"])
    return node


# ------------------------------------------------------------------ 核心校验


def iter_errors(
    instance: Any,
    schema: Any,
    *,
    root: Any = None,
    path: str = "$",
    _depth: int = 0,
) -> list[str]:
    """返回全部违规描述。空列表表示通过。

    ``root`` 是 $ref 解析的基准文档；缺省等于 ``schema``（用于自包含 schema）。
    """
    if root is None:
        root = schema
    errors: list[str] = []
    _collect(instance, schema, root, path, errors, _depth)
    return errors


def validate(
    instance: Any,
    schema: Any,
    *,
    root: Any = None,
    path: str = "$",
) -> None:
    """通过则返回 None，否则抛 :class:`ContractViolation`。"""
    errors = iter_errors(instance, schema, root=root, path=path)
    if errors:
        raise ContractViolation(errors)


def is_valid(instance: Any, schema: Any, *, root: Any = None) -> bool:
    return not iter_errors(instance, schema, root=root)


def _collect(
    instance: Any,
    schema: Any,
    root: Any,
    path: str,
    errors: list[str],
    depth: int,
) -> None:
    if depth > MAX_DEPTH:
        errors.append(f"{path}: 校验递归超过 {MAX_DEPTH} 层（契约可能有自引用 $ref）")
        return

    # true/false 作为 schema 的简写（draft 2020-12 允许）：true 全过，false 全拒
    if schema is True:
        return
    if schema is False:
        errors.append(f"{path}: 该位置按契约为 false，任何值都不合法")
        return
    if not isinstance(schema, dict):
        errors.append(f"{path}: 契约片段不是对象（{type(schema).__name__}），契约文件本身有问题")
        return

    # ---- $ref 优先；draft 2020-12 里 $ref 与其它关键字可并存，这里选择
    #      同时生效（先解析 ref 再继续跑同级关键字），比"$ref 独占"更宽容。
    if "$ref" in schema:
        try:
            target = resolve_ref(schema["$ref"], root)
        except ContractViolation as exc:
            errors.extend(exc.errors)
            return
        _collect(instance, target, root, path, errors, depth + 1)
        if len(schema) == 1:
            return

    # ---- type
    if "type" in schema:
        expected = schema["type"]
        allowed = [expected] if isinstance(expected, str) else list(expected)
        unknown = [item for item in allowed if item not in _JSON_TYPE_NAMES]
        if unknown:
            errors.append(f"{path}: 契约里的 type 含未知类型名 {unknown}")
        elif not any(_matches_type(instance, item) for item in allowed):
            errors.append(
                f"{path}: 类型不符，期望 {'/'.join(allowed)}，实际 {_type_of(instance)}"
                f"（值：{_preview(instance)}）"
            )

    # ---- const / enum
    if "const" in schema and instance != schema["const"]:
        errors.append(
            f"{path}: 常量不符，期望 {_preview(schema['const'])}，实际 {_preview(instance)}"
        )
    if "enum" in schema and instance not in list(schema["enum"]):
        errors.append(
            f"{path}: 取值不在枚举内；允许 {_preview(schema['enum'])}，实际 {_preview(instance)}"
        )

    # ---- 组合关键字
    _collect_combinators(instance, schema, root, path, errors, depth)

    # ---- 对象
    if isinstance(instance, dict):
        _collect_object(instance, schema, root, path, errors, depth)

    # ---- 数组
    if isinstance(instance, (list, tuple)):
        _collect_array(instance, schema, root, path, errors, depth)

    # ---- 字符串
    if isinstance(instance, str):
        _collect_string(instance, schema, path, errors)

    # ---- 数值
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        _collect_number(instance, schema, path, errors)


def _collect_combinators(
    instance: Any, schema: dict, root: Any, path: str, errors: list[str], depth: int
) -> None:
    if "oneOf" in schema:
        matched: list[int] = []
        branch_errors: list[list[str]] = []
        for index, sub in enumerate(schema["oneOf"]):
            branch = iter_errors(instance, sub, root=root, path=path, _depth=depth + 1)
            branch_errors.append(branch)
            if not branch:
                matched.append(index)
        if not matched:
            errors.append(
                f"{path}: oneOf 无任何分支匹配；"
                + " | ".join(
                    f"分支{index}: {'; '.join(items[:2])}"
                    for index, items in enumerate(branch_errors)
                )
            )
        elif len(matched) > 1:
            errors.append(
                f"{path}: oneOf 命中多个分支 {matched}（契约要求恰好一个）"
            )

    if "anyOf" in schema:
        if all(
            iter_errors(instance, sub, root=root, path=path, _depth=depth + 1)
            for sub in schema["anyOf"]
        ):
            errors.append(f"{path}: anyOf 无任何分支匹配")

    if "allOf" in schema:
        for index, sub in enumerate(schema["allOf"]):
            for item in iter_errors(instance, sub, root=root, path=path, _depth=depth + 1):
                errors.append(f"allOf[{index}] {item}")

    if "not" in schema:
        if not iter_errors(instance, schema["not"], root=root, path=path, _depth=depth + 1):
            errors.append(f"{path}: 命中了 not 分支（该形状被契约明确禁止）")


def _collect_object(
    instance: dict, schema: dict, root: Any, path: str, errors: list[str], depth: int
) -> None:
    required = schema.get("required")
    if required is not None:
        for key in required:
            if key not in instance:
                errors.append(f"{path}: 缺少必需字段 {key!r}")

    properties = schema.get("properties") or {}
    pattern_properties = schema.get("patternProperties") or {}
    additional = schema.get("additionalProperties", _UNKNOWN)

    for key, value in instance.items():
        child = f"{path}.{key}"
        handled = False

        if key in properties:
            handled = True
            _collect(value, properties[key], root, child, errors, depth + 1)

        for pattern, sub in pattern_properties.items():
            try:
                if re.search(pattern, key):
                    handled = True
                    _collect(value, sub, root, child, errors, depth + 1)
            except re.error as exc:
                errors.append(f"{path}: 契约里的 patternProperties 正则非法 {pattern!r}: {exc}")

        if not handled:
            if additional is False:
                errors.append(
                    f"{path}: 不允许的额外字段 {key!r}"
                    f"（允许：{sorted(properties) or '无'}）"
                )
            elif additional is True or additional is _UNKNOWN:
                pass
            elif isinstance(additional, dict):
                _collect(value, additional, root, child, errors, depth + 1)
            else:
                errors.append(f"{path}: 契约里 additionalProperties 取值非法，契约文件有问题")


def _collect_array(
    instance: list | tuple, schema: dict, root: Any, path: str, errors: list[str], depth: int
) -> None:
    if "minItems" in schema and len(instance) < schema["minItems"]:
        errors.append(f"{path}: 元素太少，最少 {schema['minItems']}，实际 {len(instance)}")
    if "maxItems" in schema and len(instance) > schema["maxItems"]:
        errors.append(f"{path}: 元素太多，最多 {schema['maxItems']}，实际 {len(instance)}")
    if schema.get("uniqueItems"):
        seen: list[Any] = []
        for item in instance:
            if item in seen:
                errors.append(f"{path}: uniqueItems 要求元素互不相同，出现重复 {_preview(item)}")
                break
            seen.append(item)

    items = schema.get("items")
    if items is None:
        return
    if isinstance(items, list):
        # 元组式：逐位置套用；额外元素由 additionalItems 管（本子集不支持，忽略）
        for index, item in enumerate(instance):
            if index < len(items):
                _collect(item, items[index], root, f"{path}[{index}]", errors, depth + 1)
    else:
        for index, item in enumerate(instance):
            _collect(item, items, root, f"{path}[{index}]", errors, depth + 1)


def _collect_string(instance: str, schema: dict, path: str, errors: list[str]) -> None:
    if "minLength" in schema and len(instance) < schema["minLength"]:
        errors.append(
            f"{path}: 字符串太短，最少 {schema['minLength']}，实际 {len(instance)}（值：{_preview(instance)}）"
        )
    if "maxLength" in schema and len(instance) > schema["maxLength"]:
        errors.append(f"{path}: 字符串太长，最多 {schema['maxLength']}，实际 {len(instance)}")
    if "pattern" in schema:
        try:
            if not re.search(schema["pattern"], instance):
                errors.append(
                    f"{path}: 不匹配 pattern {schema['pattern']!r}（值：{_preview(instance)}）"
                )
        except re.error as exc:
            errors.append(f"{path}: 契约里的 pattern 正则非法：{exc}")


def _collect_number(instance: float, schema: dict, path: str, errors: list[str]) -> None:
    def _bound(key: str, exclusive: bool, compare) -> None:
        if key not in schema:
            return
        limit = schema[key]
        if not isinstance(limit, (int, float)) or isinstance(limit, bool):
            errors.append(f"{path}: 契约里 {key} 不是数值，契约文件有问题")
            return
        if not compare(instance, limit):
            word = "必须大于" if exclusive else "必须不小于"
            errors.append(f"{path}: {key} 违规，{word} {limit}，实际 {instance}")

    _bound("minimum", False, lambda a, b: a >= b)
    _bound("exclusiveMinimum", True, lambda a, b: a > b)
    _bound("maximum", False, lambda a, b: a <= b)
    _bound("exclusiveMaximum", True, lambda a, b: a < b)

    if "multipleOf" in schema:
        step = schema["multipleOf"]
        if isinstance(step, (int, float)) and not isinstance(step, bool) and step > 0:
            quotient = instance / step
            if abs(quotient - round(quotient)) > 1e-9:
                errors.append(f"{path}: multipleOf {step} 违规，实际 {instance}")


def _preview(value: Any, limit: int = 120) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


# ------------------------------------------------------------------ 契约注册表


class ContractRegistry:
    """加载 contracts/ 下的 JSON Schema 契约，并对外暴露边界校验入口。

    不做全局缓存：registry 是轻量对象（一次 json.load 四个文件），测试里会
    故意改文件内容再重载，缓存反而会掩盖问题。要在热路径上用就自己持有一个
    实例，别每条消息都 new。
    """

    PROTOCOL = "protocol.json"
    COMMANDS = "commands.json"
    EVENTS = "events.json"
    ERROR_CODES = "error-codes.json"

    def __init__(self, contracts_dir: str | Path | None = None) -> None:
        self.dir = Path(contracts_dir) if contracts_dir else default_contracts_dir()
        self.protocol = self._load(self.PROTOCOL)
        self.commands = self._load(self.COMMANDS)
        self.events = self._load(self.EVENTS)
        self.error_codes = self._load(self.ERROR_CODES)

        self.protocol_version: str = str(self.protocol.get("protocolVersion", ""))
        self._command_table: dict[str, Any] = self.commands.get("commands") or {}
        self._event_table: dict[str, Any] = self.events.get("events") or {}

        max_line = (self.protocol.get("transport") or {}).get("maxLineBytes")
        self.max_line_bytes: int = int(max_line) if isinstance(max_line, int) else MAX_LINE_BYTES

    # ---- 加载

    def _load(self, name: str) -> dict[str, Any]:
        path = self.dir / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ContractNotFound([f"契约文件读取失败 {path}: {exc}"], "契约不可用") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ContractNotFound(
                [f"契约文件 JSON 非法 {path}:{exc.lineno}:{exc.colno}: {exc.msg}"], "契约不可用"
            ) from exc
        if not isinstance(data, dict):
            raise ContractNotFound([f"契约文件顶层必须是对象：{path}"], "契约不可用")
        return data

    # ---- 枚举

    @property
    def command_names(self) -> list[str]:
        return sorted(self._command_table)

    @property
    def event_names(self) -> list[str]:
        return sorted(self._event_table)

    def command(self, name: str) -> dict[str, Any]:
        try:
            return self._command_table[name]
        except KeyError:
            raise ContractViolation(
                [f"未知命令 {name!r}；契约内的命令共 {len(self._command_table)} 条"],
                "命令不在契约内",
            ) from None

    def event(self, name: str) -> dict[str, Any]:
        try:
            return self._event_table[name]
        except KeyError:
            raise ContractViolation(
                [f"未知事件 {name!r}；契约内的事件共 {len(self._event_table)} 条"],
                "事件不在契约内",
            ) from None

    def needs_pipeline(self, name: str) -> bool:
        return bool(self.command(name).get("needsPipeline", True))

    def returns_none(self, name: str) -> bool:
        return bool(self.command(name).get("returnsNone", False))

    def universal_error_codes(self) -> list[str]:
        block = self.error_codes.get("universal") or {}
        return [str(item.get("code")) for item in (block.get("codes") or [])]

    # ---- 单段校验

    def args_schema(self, name: str) -> dict[str, Any]:
        self.command(name)
        return self._command_table[name].get("args") or {"type": ["object", "null"]}

    def result_schema(self, name: str) -> dict[str, Any]:
        self.command(name)
        return self._command_table[name].get("result") or {"type": "null"}

    def event_schema(self, name: str) -> dict[str, Any]:
        self.event(name)
        return self._event_table[name].get("payload") or {"type": "object"}

    def validate_args(self, name: str, args: Any) -> None:
        """校验某命令的 args。args 允许为 None（后端按 {} 处理）。"""
        schema = self.args_schema(name)
        errors = iter_errors(args, schema, root=self.commands, path=f"$.commands.{name}.args")
        if errors:
            raise ContractViolation(errors, f"命令 {name!r} 的参数不合法")

    def validate_result(self, name: str, data: Any) -> None:
        """校验某命令在 ok:true 时应答里 data 的形状。"""
        schema = self.result_schema(name)
        errors = iter_errors(data, schema, root=self.commands, path=f"$.commands.{name}.result")
        if errors:
            raise ContractViolation(errors, f"命令 {name!r} 的返回值不合法")

    def validate_async_accepted(self, data: Any) -> None:
        """Validate the transport acknowledgement returned for ``args.async=true``."""
        schema = (self.protocol.get("$defs") or {}).get("AsyncAccepted")
        if not isinstance(schema, dict):
            raise ContractNotFound(["protocol.json 缺少 $defs.AsyncAccepted"], "契约不可用")
        errors = iter_errors(data, schema, root=self.protocol, path="$.protocol.AsyncAccepted")
        if errors:
            raise ContractViolation(errors, "异步受理回执不合法")

    def validate_event(self, event: Any) -> None:
        """校验一条出站事件：必须是对象、带已知 event 名、载荷形状正确。"""
        if not isinstance(event, dict):
            raise ContractViolation(
                [f"$: 事件必须是对象，实际 {_type_of(event)}"], "事件不合法"
            )
        name = event.get("event")
        if not isinstance(name, str) or not name:
            raise ContractViolation(
                ["$.event: 缺少事件名（必须是字符串）"], "事件不合法"
            )
        self.event(name)  # 未知事件名在这里报错
        schema = self.event_schema(name)
        errors = iter_errors(event, schema, root=self.events, path=f"$.events.{name}")
        if errors:
            raise ContractViolation(errors, f"事件 {name!r} 的载荷不合法")
        if name == "job" and "result" in event:
            if event.get("command") != "start_migration" or event.get("status") != "succeeded":
                raise ContractViolation(
                    ["$.events.job.result: 只有 start_migration 成功终态可以携带任务结果"],
                    "任务事件结果不合法",
                )
            self.validate_result("start_migration", event["result"])

    # ---- 信封校验

    def validate_request_envelope(self, message: Any) -> None:
        """校验入站命令的通用信封形状，不检查命令存在性或具体 args。"""
        envelope = ((self.protocol.get("envelope") or {}).get("request")) or {}
        errors = iter_errors(message, envelope, root=self.protocol, path="$")
        if errors:
            raise ContractViolation(errors, "入站命令信封不合法")

    def validate_request(self, message: Any) -> str:
        """入站命令的完整边界校验，返回命令名。

        三层全查：信封形状 → 命令名在契约内 → args 合法。任何一层失败都抛
        :class:`ContractViolation`（不是逐个返回值），接线处只要 try/except 一次。
        """
        self.validate_request_envelope(message)
        name = str(message["command"])
        self.command(name)
        self.validate_args(name, message.get("args"))
        return name

    def validate_response(
        self,
        message: Any,
        *,
        command: str | None = None,
        async_accepted: bool = False,
    ) -> None:
        """Validate a response envelope and, when supplied, its actual command result."""
        envelope = ((self.protocol.get("envelope") or {}).get("response")) or {}
        errors = iter_errors(message, envelope, root=self.protocol, path="$")
        if errors:
            raise ContractViolation(errors, "出站应答信封不合法")
        if not isinstance(message, dict) or message.get("ok") is not True:
            return
        data = message.get("data")
        if async_accepted:
            self.validate_async_accepted(data)
        elif command is not None:
            self.validate_result(command, data)

    def validate_outbound(
        self,
        message: Any,
        *,
        command: str | None = None,
        async_accepted: bool = False,
    ) -> str:
        """Validate an outbound response/event, optionally including command result data.

        判定顺序与 BackendBridge._route 一致：先看 id 是不是数字 → 应答，
        否则看 event 是不是字符串 → 事件。
        """
        if not isinstance(message, dict):
            raise ContractViolation([f"$: 出站消息必须是对象，实际 {_type_of(message)}"], "出站消息不合法")
        if "id" in message and (
            message.get("id") is None
            or (isinstance(message.get("id"), int) and not isinstance(message.get("id"), bool))
        ):
            self.validate_response(message, command=command, async_accepted=async_accepted)
            return "response"
        if isinstance(message.get("event"), str):
            self.validate_event(message)
            return "event"
        raise ContractViolation(
            ["$: 既没有数字 id，也没有字符串 event —— 无法归类"], "出站消息不合法"
        )

    # ---- 行级入口（NDJSON）

    def parse_line(self, line: str, *, max_bytes: int | None = None) -> Any:
        """把一条 NDJSON 行解析成对象，先做尺寸闸门再做 JSON 解析。

        尺寸闸门放在解析**之前**：先 json.loads 再判大小等于让畸形输入先吃掉
        内存，闸门就白设了。
        """
        limit = self.max_line_bytes if max_bytes is None else int(max_bytes)
        if not isinstance(line, str):
            raise ContractViolation([f"$: 行必须是字符串，实际 {_type_of(line)}"], "行不合法")
        size = len(line.encode("utf-8"))
        if size > limit:
            raise ContractViolation(
                [f"$: 消息过大，{size} 字节超过上限 {limit} 字节"],
                "消息过大",
            )
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise ContractViolation(
                [f"$: 不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}）"],
                "行不合法",
            ) from exc

    def validate_inbound_line(self, line: str, *, max_bytes: int | None = None) -> str:
        return self.validate_request(self.parse_line(line, max_bytes=max_bytes))

    def validate_outbound_line(self, line: str, *, max_bytes: int | None = None) -> str:
        return self.validate_outbound(self.parse_line(line, max_bytes=max_bytes))


def iter_command_names(registry: ContractRegistry) -> Iterator[str]:
    """按契约声明顺序（而非字典序）遍历命令名。"""
    yield from (registry.commands.get("commands") or {})
