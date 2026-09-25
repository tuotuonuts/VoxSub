# VoxSub IPC 契约（contracts/）

Electron 前端与 Python 后端之间**只有一条耦合面**：stdin/stdout 上的 NDJSON。
这个目录是那条耦合面的**单一来源**。

```
contracts/
├── protocol.json     信封格式、握手字段、作业语义、版本策略
├── commands.json     55 条命令：args / result / errors / needsPipeline / controlCommand
├── events.json       14 个线上事件的载荷 + 前端自产事件 + 缺口登记
├── error-codes.json  错误码（信封的 code 字段 + 例外类型 + 通用兜底）
└── README.md         本文件

frontend/backend/contract_validation.py   轻量校验器（纯标准库）
tests/test_contracts.py                   覆盖测试 + 棘轮测试 + 校验器负向测试
```

## 为什么要有它

命令名散落在多处时，后端改名不会有任何编译期错误：

- `frontend/src/renderer/protocol.ts` 的 `CMD` 表有 52 项
- `ipc_server.py` 有 53 个 `_cmd_*` 方法，外加 `handle()` 里的 2 个特判
- `ipc_loop.py` 用 `has_command()` 做前置校验，`job_runner.CONTROL_COMMANDS` 决定谁排队

这三处谁对不上，表现都是"点了没反应"或"弹一个看不懂的错"，而不是启动失败。
契约 + 覆盖测试把这类错误变成**一次 pytest 失败**。

另一个动机是**诚实地记录缺口**。这个仓库里"声明的形状"和"实际发的东西"本来就不完全一致
（TS 声明落后、读循环发的 log 事件缺 `ts`）。与其假装一致，不如逐条登记，
再用棘轮测试保证缺口只能缩小、不能悄悄扩大。

## 三个层次的关系

| 层 | 位置 | 权威性 |
|---|---|---|
| **契约（本目录）** | JSON Schema draft 2020-12 子集 | 单一来源。测试断言代码与它一致 |
| **校验器** | `contract_validation.py` | 运行时执行契约。只用了 `json` / `re`，无新依赖 |
| **测试** | `tests/test_contracts.py` | 离线静态解析源码 + 运行时负向测试 |

契约是**从代码实测推导**的（`ast` 解析 `_cmd_*` / `_event(...)`，文本解析 TS），
不是手写愿望清单。改动顺序永远是：先改代码，再跑测试看它指出哪些契约条目要同步。

```bash
./.venv/Scripts/python.exe -m pytest tests/test_contracts.py -q
```

> 注意：本机 shell 是 git-bash，且 Hermes 会注入 `PYTHONPATH`。
> 调 `.venv/Scripts/python.exe` 前先 `unset PYTHONPATH PYTHONHOME`。

---

## 如何新增一条命令

以新增 `foo_bar` 为例。

### 1. 后端实现

在 `ipc_server.py` 的 `BackendService` 上加方法：

```python
def _cmd_foo_bar(self, args: dict[str, Any]) -> dict[str, Any]:
    ...
```

- 需要 pipeline 就按现有签名写 `def _cmd_foo_bar(self, pipeline: Any, args)`；
  **不需要** pipeline 的话，必须把它加进文件末尾那个 `for _name in (...)` 白名单，
  否则打开设置页之类的动作会把整个推理栈拉起来。
- 会被耗时操作阻塞、且调用方希望取消 → 不要放进 `job_runner.CONTROL_COMMANDS`，
  让它走作业队列；同时在命令实现里用 `_cancel_requested()` 在安全边界检查取消。

### 2. 契约登记

在 `contracts/commands.json` 的 `commands` 下加一条，**字段一个都不能少**：

```json
"foo_bar": {
  "implementedBy": "_cmd_foo_bar",
  "handleSpecial": false,
  "needsPipeline": false,
  "returnsNone": false,
  "controlCommand": false,
  "summary": "一句话说明它做什么，以及为什么这么设计。",
  "source": "ipc_server.py::_cmd_foo_bar",
  "args": { "type": ["object", "null"], "additionalProperties": false, "properties": {
    "some_arg": { "type": "string" }
  } },
  "result": { "type": "object", "required": ["path"], "additionalProperties": false,
              "properties": { "path": { "type": "string" } } },
  "errors": []
}
```

- 非控制命令的 `args.properties` **必须**包含 `async`
  （读循环对每条非控制命令都读它）。
- `errors` 只列该命令**特有**的例外类型；通用兜底见 `error-codes.json#/universal`。
- 无返回值就写 `"result": {"type": "null"}` 并把 `returnsNone` 设 `true`。

### 3. 前端声明

在 `frontend/src/renderer/protocol.ts` 的 `CMD` 表加一项。

**如果你这一轮不加**（例如前端还没接线），就把命令名加进
`commands.json#/uiPendingCommands.commands` 并写明理由 —— 否则
`test_TS_命令表与契约逐项相等` 会红。这是刻意的：不允许前端声明静默落后。

### 4. 跑测试

```bash
./.venv/Scripts/python.exe -m pytest tests/test_contracts.py -q
```

四个覆盖测试会双向检查：
`_cmd_*` ↔ 契约条目、`needsPipeline` ↔ 源码白名单、
`controlCommand` ↔ `job_runner.CONTROL_COMMANDS`、`async` 约定。

---

## 如何新增一个事件

### 1. 后端发出

只能通过 `_event(kind, **fields)`（或 `ipc_loop.self._event`）。
**不要**写裸的 `{"event": "x"}` 字典 —— `test_没有绕过_event_辅助函数的裸事件字典`
会扫源码拦住它，因为那样会跳过契约登记。

### 2. 契约登记

在 `contracts/events.json` 的 `events` 下加一条。`payload` 是**整条事件对象**的 schema，
必须带 `"event": {"const": "<名字>"}`：

```json
"foo_done": {
  "summary": "什么情况下发，谁消费。",
  "emittedBy": "ipc_server.py::_cmd_foo_bar",
  "payload": {
    "type": "object",
    "required": ["event", "path"],
    "additionalProperties": false,
    "properties": {
      "event": { "const": "foo_done" },
      "path": { "type": "string" }
    }
  }
}
```

`additionalProperties: false` 是**必须的**：它让"多塞一个字段"变成测试失败。
确实需要扩展时，改契约 + 在前端声明 + 按下面的规则递增版本。

### 3. 前端声明

在 `protocol.ts` 的 `BackendEvent` 里加一条分支。若这一轮不加：

- **有意不消费**（例如发得比监听器还早）→ 加进 `events.json#/uiUnlistedEvents`
- **声明落后、本该修** → 加进 `events.json#/uiPendingEvents`，并写明 `fix`

不登记就会红。两个清单的区别是"设计如此"和"欠债"，别混用。

### job 事件的结果字段

`start_migration.args` 可选 `clientMigrationId`（1–128 字符），由异步 Renderer 请求生成并与 `async: true` 配合使用。`job` 事件仅在携带该 ID 的异步 `start_migration` 成功终态带 `result`（迁移报告）；关联 ID 随该迁移 job 状态事件回传，用于受理回执超时后的终态重关联。其它 job 的任意结果不放到事件总线上，避免扩大数据暴露面。前端须先订阅 job 事件，再提交请求并以关联 ID 等真实终态；若成功终态先于 failed/unavailable 回执到达，缓存结果优先于随后断连。

### 4. 前端自产事件

主进程自己合成的（`disconnected`、`request-timeout` 这类）登记在
`events.json#/frontendSynthesizedEvents`，**不要**放进 `events` ——
`events` 只放后端真的写到 stdout 上的东西，否则"后端事件全覆盖"这条检查会被污染。

---

## 协议版本如何递增

有两个版本号，**不是同一个序列**，别互相换算：

| 名字 | 位置 | 含义 |
|---|---|---|
| `protocolVersion` | `contracts/protocol.json`（semver）+ `ipc_loop.PROTOCOL_VERSION`（整数） | 契约文档 / 线上协议 |
| `__version__` | `voxsub/__init__.py` | 应用版本，与协议无关 |

`protocol.json#/wireProtocolVersion` 把两者成对登记，测试断言
`wireProtocolVersion.value == ipc_loop.PROTOCOL_VERSION`。改一处必须改另一处。

递增规则：

- **major** —— 信封/握手/已有字段语义的破坏性变更。例：应答新增必填字段、
  `cleanup_migrated_source` 从 `path` 改成 `record_id`（旧参数被显式拒绝）。
  前端遇到不认识的 major 应当拒绝启动并给出可读提示，而不是硬跑。
- **minor** —— 新增命令、新增事件、新增**可选**字段。旧对端可以忽略新字段
  继续工作（所以 `ready` 的 `additionalProperties` 是 `true`）。
- **patch** —— 只改 `summary` / `description` / 错误码说明，线上形状不变。

改完在 `protocol.json#/versioning.currentRevision` 里补一句"相对上一版改了什么、
为什么是 major/minor"，让下一个人不用 diff 就能看懂。

---

## 校验器怎么用（接线留给主代理）

```python
from contract_validation import ContractRegistry, ContractViolation

REGISTRY = ContractRegistry()          # 默认读仓库根的 contracts/

# ---- 入站（ipc_loop.handle_line 解析 JSON 之后、分派之前）
try:
    command = REGISTRY.validate_request(message)   # 信封 + 命令名已知 + args 合法
except ContractViolation as bad:
    emit({"id": message.get("id"), "ok": False,
          "code": "bad_request", "error": str(bad)})
    return
```

```python
# ---- 出站结果（就在 _emit({"id":..., "ok":True, "data": data}) 之前）
try:
    REGISTRY.validate_result(command, data)
except ContractViolation as bad:
    log.warning("命令 %s 的返回值不符合契约：%s", command, bad)
```

```python
# ---- 出站事件（ipc_loop._event / ipc_server._event 里）
event = {"event": kind, **fields}
try:
    REGISTRY.validate_event(event)
except ContractViolation as bad:
    log.error("事件不符合契约：%s", bad)
```

接线建议：

1. **失败要留痕，不要吞**。契约违规走 log 事件（`level=ERROR`），
   带命令名与违规点。生产环境不要直接杀死进程 —— 但也不要静默放过。
2. **出站方向先只记不拦**（warn-only）跑一版，确认没有误报再改成拒绝。
   契约是从代码推导的，第一版必然有没覆盖到的角落。
3. `REGISTRY` 别每条消息都 new —— 构造会读四个 JSON 文件。进程内持有一个实例。
4. 尺寸闸门用 `registry.max_line_bytes`（与 `ipc_loop.MAX_LINE_CHARS` 对齐），
   不要用模块级的 `MAX_LINE_BYTES`（那只是兜底默认值）。

### 校验器的能力边界

已支持：`type` `const` `enum` `required` `properties` `additionalProperties`
`patternProperties` `items` `minItems` `maxItems` `uniqueItems` `minLength`
`maxLength` `minimum` `maximum` `exclusiveMinimum` `exclusiveMaximum` `multipleOf`
`pattern` `oneOf` `anyOf` `allOf` `not` `$ref`（仅本地 `#/`）`$defs`

刻意忽略：`$schema` `$id` `title` `description` `default` `examples` `$comment`，
以及**任何未列出的键** —— 忽略未知关键字比猜测其语义更安全。

刻意不做：远程 `$ref`、`$dynamicRef`、`unevaluatedProperties`、`if/then/else`、
`format` 语义校验。**因此契约文件之间不允许互相 `$ref`**：
共享形状在各文件内各自 `$defs` 声明一份，并由测试断言它们逐字段相同
（见 `test_state_事件载荷与_StatePayload_定义完全一致`）。

### 为什么不用 jsonschema / ajv / zod

需要一个能跑在后端进程里的**边界校验**，而 `requirements.txt` 里没有这类库。
新增依赖会同时影响 `.venv` 与 PyInstaller 打包体积，且这不是核心功能。
本实现只用 `json` + `re`。如果将来确实需要完整 JSON Schema 支持
（远程 `$ref`、`format` 校验等），再评估引入 `jsonschema`——
那需要单独论证必要性、许可证与打包影响，不要顺手加。

---

## 测试红了怎么办

| 失败信息 | 说明 | 怎么办 |
|---|---|---|
| `这些 _cmd_* 方法没有契约定义` | 加了后端命令没登记 | 按"如何新增一条命令"补 `commands.json` |
| `契约声明了但代码里没有实现` | 删了实现没删契约 | 删契约条目，或补回实现 |
| `needsPipeline=false 的集合与源码不一致` | 白名单与契约对不上 | 两边改一致 |
| `controlCommand 的集合与 job_runner.CONTROL_COMMANDS 不一致` | 谁排队改了 | 两边改一致 |
| `TS 声明 + 待接线 != 契约` | 前端声明落后且未登记 | 加进 `uiPendingCommands`，或补 TS |
| `必需字段缺口与登记不一致` | 事件少发字段了 | 补字段；若是有意为之，登记进 `knownGaps` |
| `字段缺口涉及的事件集合与登记不一致` | TS 少声明字段 | 补 TS，或调 `uiFieldDrift` |
| 负向测试失败 | **校验器变松了** | 这不是契约问题，是校验器被削弱 —— 优先修校验器 |

最后一条最重要：校验器的负向测试红了，意味着"边界不再拦得住"，
比任何契约漂移都严重。

---

## 已知待接线 / 待修清单（截至本版）

**接线（主代理）**

1. 把 `ContractRegistry` 接进 `ipc_loop.handle_line`（入站）与 `_emit`（出站）。
2. 主进程注入 `backendGeneration`（当前 `ready.backendGeneration` 恒为空串，
   "区分代次"因此还没有实际判别力）。
3. 前端在 `ready` 分支校验协议主版本，不匹配就拒绝启动并给可读提示。
4. 归一协议版本：线上是整数 1，契约文档是 semver 2.0.0，二选一统一
   （见 `protocol.json#/wireProtocolVersion`）。

**声明不一致（前端）—— 全部登记在 `events.json#/uiFieldDrift`**

| 事件 | TS 缺 | TS 多 |
|---|---|---|
| `ready` | — | — |
| `migration` | `source` / `completed` / `keys` / `recordId` / `path` | — |
| `job` | `code` | `status` / `detail` |

`job` 那条最值得先修：后端发的是 `action`（`queued`/`running`/`cancelling`/
`succeeded`/`failed`/`cancelled`）+ `code`，TS 的 `JobEvent` 却用了 `status` 并多声明
`detail`。结果是 UI 读 `event.status` / `event.detail` 永远 `undefined`，
而真正区分「取消中 vs 已取消」的 `action` 枚举没人消费。

`readiness.activeJobs` 现在在 `protocol.ts` 中声明为 `string[]`，与后端发出的命令名数组一致。

**后端缺口 —— `events.json#/knownGaps`**

- 读循环发的 5 条 `log` 事件没带 `ts`（UI 的 LogEntry 要求它）
- 入站 JSON 解析失败只发 log、不回执，前端那条 id 会一直等到 30s 超时
  → `error-codes.json#/notErrors`

**结构债**

- `migration` 一个事件名承载 7 种 phase，字段按 phase 不同
  → 建议收敛成按 phase 判别的联合类型，或拆成多个事件

## 这些清单正在自己收缩

本版期间发生过一次真实的收敛：`uiPendingCommands` 与 `uiPendingEvents`
（`job_list`/`job_status`/`cancel_job`/`job` 四笔）都已因为 `protocol.ts` 补上声明
而被**删除**。测试里的规则是「零项时必须整个键删掉，不许留空壳」——
所以清单只会变短或消失，不会烂在那儿没人管。
