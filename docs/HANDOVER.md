# 语幕 VoxSub —— 交接手册（新功能接入 / 测试 / 构建 / 发布 / 回滚）

> 目标：一个没参与过本项目的人，只靠这份文档 + 仓库里的门禁，就能安全地加功能、
> 跑测试、出构建、必要时回滚。
>
> 配套阅读：`docs/ARCHITECTURE.md`（模块边界与状态所有权）、
> `docs/MAINTAINABILITY_REPORT.md`（本轮改了什么、还剩什么）。

---

## 一、日常安全验收（Windows Git Bash）

以下是源码安全验收，不等同 GUI、真实模型/音频或成品验收。工作目录、解释器、环境隔离和 marker 必须一起保留；不可直接复制旧版默认 pytest 命令。此机器 Hermes scratch 的实际绝对路径为 `E:/Hermes_data/cache/scratch`，换机器先核实，禁止回落到系统临时目录或 D:/tmp。

```bash
(
set -e
cd D:/OneDrive/app_dve/VoxSub
unset PYTHONPATH PYTHONHOME
run="E:/Hermes_data/cache/scratch/voxsub-safe-$(./.venv/Scripts/python.exe -c 'import uuid; print(uuid.uuid4().hex)')"
mkdir -p "$run/APPDATA" "$run/LOCALAPPDATA" "$run/TEMP" "$run/TMP"
export APPDATA="$run/APPDATA" LOCALAPPDATA="$run/LOCALAPPDATA" TEMP="$run/TEMP" TMP="$run/TMP" TMPDIR="$run/TMP"
./.venv/Scripts/python.exe -c 'import os,pathlib; root=pathlib.Path("E:/Hermes_data/cache/scratch").resolve(); paths={k:pathlib.Path(os.environ[k]).resolve() for k in ("APPDATA","LOCALAPPDATA","TEMP","TMP","TMPDIR")}; assert all(p.is_relative_to(root) for p in paths.values()); print(paths)'
./.venv/Scripts/python.exe -m pytest -q -rs -m "not integration and not hardware_audio" --basetemp "$run/pytest"
cd D:/OneDrive/app_dve/VoxSub/frontend
npm run check
npm run test:acceptance-contracts
)
```

marker 明确排除 integration 和 hardware_audio；skip/xfail/排除项不是通过。前端默认只执行 Node/TypeScript 门禁，不运行 Electron。括号隔离环境变量，结束后不把测试环境留给日常终端。

### 单独运行真实 sidecar 通信测试（L3）

仅在允许真实子进程只读通信检查时使用以下独立命令，不与上方单元/行为数量相加。测试启动自己的 Python sidecar，发握手/ping/state/get_config/只读诊断等命令，finally 仅回收该测试创建的子进程；可能枚举系统能力/窗口，但不启动音频捕获、播放、模型推理或用户目录迁移。

```bash
(
set -e
cd D:/OneDrive/app_dve/VoxSub
unset PYTHONPATH PYTHONHOME
run="E:/Hermes_data/cache/scratch/voxsub-ipc-$(./.venv/Scripts/python.exe -c 'import uuid; print(uuid.uuid4().hex)')"
mkdir -p "$run/APPDATA" "$run/LOCALAPPDATA" "$run/TEMP" "$run/TMP"
export APPDATA="$run/APPDATA" LOCALAPPDATA="$run/LOCALAPPDATA" TEMP="$run/TEMP" TMP="$run/TMP" TMPDIR="$run/TMP"
./.venv/Scripts/python.exe -c 'import os,pathlib; root=pathlib.Path("E:/Hermes_data/cache/scratch").resolve(); paths={k:pathlib.Path(os.environ[k]).resolve() for k in ("APPDATA","LOCALAPPDATA","TEMP","TMP","TMPDIR")}; assert all(p.is_relative_to(root) for p in paths.values()); print(paths)'
./.venv/Scripts/python.exe -m pytest tests/test_ipc_integration.py -q -rs -m "integration and not hardware_audio" --basetemp "$run/pytest"
)
```

### 旧工具警告（未删除，也未改造）

- `scripts/run_tests.py` 会修剪历史 `.pytest-run` 子目录，且仅设置 basetemp；不完整隔离用户配置。它不是本轮的日常安全入口。默认 pytest 只排除 hardware_audio，并不排除全部 integration。
- `npm run verify` 会继续调用 backend probe/OCR/迁移检查。`tools/probe-backend.py` 继承用户环境并包含 set_config、set_mode、set_langs、set_asr_tuning、导出等写操作；禁止把它当成无副作用校验直接执行。
- 发布脚本、GUI、模型/音频和成品验证须另行确认环境与副作用。本轮未运行发布脚本，不把 `--check-only` 称为完全无副作用。

最新返修证据与三个 Git 回退目标见 [LAST_REPAIR_2026-09-27.md](LAST_REPAIR_2026-09-27.md)。

---

## 二、加一个新功能要碰哪些文件

### 典型链路（以"增加一个设置项"为例）

| 步骤 | 文件 | 注意 |
|---|---|---|
| 1. 定义配置键与默认值 | `voxsub/config_store.py` 的 `_DEFAULTS` | **Python schema 是权威来源**，前端不自己定默认值 |
| 2. 加 IPC 命令（如果需要） | `frontend/backend/ipc_server.py` 的 `_cmd_xxx` | 长任务不用自己起线程，`ipc_loop` 会自动排队 |
| 3. 补契约 | `contracts/`（见 `contracts/README.md`） | 不加契约的话覆盖测试会红 |
| 4. 前端调用 | `frontend/src/renderer/protocol.ts` 加命令名映射 → 页面里调用 | 不要绕过 preload 白名单 |
| 5. 页面/组件 | `frontend/src/renderer/views/` | 页面构建返回 `{ element, dispose }`，切换时统一 dispose |
| 6. 加测试 | 后端 `tests/`，前端 `frontend/src/shared/` + `frontend/tools/test-*.mjs` | 见 §三 |
| 7. 加类型 | `frontend/src/renderer/protocol.ts` | `npm run typecheck` 必须过 |

### 加一条新的长任务命令

**几乎什么都不用做。** 只要命令名不在 `job_runner.CONTROL_COMMANDS` 里，
`ipc_loop` 就会自动把它排进单 worker 执行，并且：

- 默认保持**同步应答**语义（前端不改也能用）；
- 调用方带 `args.async = true` 时，立刻收到 `{jobId, accepted, status}`，结果走 `event:"job"`；
- 异步 `start_migration` 由 Renderer 传唯一 `clientMigrationId`；该 ID 随迁移状态事件回传，成功终态同时带迁移报告，供受理回执硬超时后的终态重关联；其他 job 的任意结果不广播。
- 前端须先订阅事件、再提交异步请求；关联 ID 让其在缺少 `jobId` 回执时继续等同一任务；若终态先于回执到达，缓存结果优先于后续 failed/unavailable 回执或断连。退出保护只在权威终态或确定未发送时解除；仅断连不足以认定任务结束。
- 自动获得 `job_status` / `job_list` / `cancel_job` 的支持。

如果你希望它支持**协作式取消**，在它的安全边界（例如每一步之间）插一句：

```python
if _cancel_requested():
    break
```

### 什么时候该把代码抽出去

看到下面任意一条，就该抽成独立模块并配单测：

- 这段逻辑需要**逐条列出规则**（像删除授权那样），写在命令里就没法逐条测；
- 它需要**脱离协议层**被测试（不想起进程、不想起 Electron）；
- `tests/test_architecture.py` 的复杂度预算开始拦你。

**反例**：只为了一次性流程而加抽象层。工作单明确说了"不以增加组件数量作为验收目标"。

---

## 三、测试怎么写

分层（工作单 §3.9），别把不同层的测试混在一起：

| 层 | 内容 | 位置 | 要求 |
|---|---|---|---|
| L1 纯单元 | 纯函数、状态机、规则判断 | `tests/test_*.py` | 无 IO、无网络、无需模型 |
| L2 服务/契约 | 真实业务代码，只替换模型/网络/OS 边界 | `tests/test_*.py` | 覆盖并发、取消、迟到结果、资源关闭 |
| L3 跨进程集成 | 真起 sidecar、真走 NDJSON | 标 `@pytest.mark.integration` | 用隔离的配置目录 |
| L4 Electron 静默 E2E | 真起 Electron + CDP | `frontend/tools/test-*.mjs` | **必须** `npm run launch:silent -- --debug` |
| L5 真机验收 | 真实硬件/成品包 | 单独记录 | 没有硬件就写 NOT_RUN，**绝不用 mock 顶替** |

### 硬规矩

1. **并发测试用 barrier/Event，不用 `sleep` 猜时间。** 参考 `tests/test_job_runner.py` 的
   `_wait_until()` 轮询写法。
2. **不许 mock 掉你正在验证的生命周期方法本身。** 那等于测了个寂寞。
3. **不许删测试或放宽断言来变绿。** 测试红了先看是代码错还是断言错。
4. **测试不得弹窗、抢焦点、播放音频。** 前端测试漏掉 `--silent` 会弹到用户桌面上。
5. **日常安全测试使用每次独占的 Hermes scratch 绝对目录**：按 §一隔离四个环境目录并显式传入 basetemp。不要使用未核实的 TMPDIR，不往仓库根或系统临时目录写测试产物；旧 wrapper 有清理副作用，不是安全默认入口。
6. **每个"已修复"都要有真实运行输出**，没跑的写 NOT_RUN 加原因。
   严禁编造运行结果 —— 这比不做更糟。

### 竞态与资源释放回归

- IPC：请求记录必须在任务交给 worker 前可见；测试用 worker 可立即完成的真实顺序，断言请求仍能拿到终态回复。
- 任务取消：取消请求、worker 开始和成功/失败终态提交必须通过同一状态锁仲裁；排队任务一旦取消不得执行，运行任务不得从 cancelling/cancelled 倒退回 running/succeeded。
- TTS：`stop(timeout)` 必须报告是否真实退出；超时仍存活的 worker 继续持有旧资源，不创建并行替代实例。验证真实阻塞播放的 worker，释放事件后确认退出和后续重建。
- 组装多项实时组件时，逐个记录成功创建的对象；后续失败按逆序调用资源提供的 `close()`，清理异常写日志且不覆盖原始构建异常。
- UI 异步/页面句柄：用 A/B 两页逆序 dispose 与迟到响应的确定性测试，确认 A 不会释放或写入 B；migration 旧请求迟到时不得清除新任务保护、写入共享报告或导航新向导；不以源码字符串断言替代运行时测试。

### 前端纯逻辑测试的写法（不启动 Electron）

把可测逻辑放进 `frontend/src/shared/*.ts`，再用 esbuild 编译成临时 `.mjs` 后 import。
既有范例：`frontend/src/shared/log-levels.ts` + `frontend/tools/test-log-levels.mjs`。

---

## 四、门禁清单（红了就该知道为什么）

`tests/test_architecture.py` —— 全部是机械可判定的规则：

| 规则 | 说明 |
|---|---|
| 核心包不许 import UI 层 | 核心必须能脱离 Qt 使用 |
| 核心包不许 import 适配层 | 依赖方向单向：适配层 → 核心 |
| 适配层不许 import Qt | 打包排除 Qt 是刻意的 |
| 函数复杂度 ≤ 15 | 存量有 8 条棘轮基线，**只允许变短**；新增超标直接红 |
| `queue.Queue()` 必须显式 `maxsize` | 无界队列会把积压藏起来 |
| `subprocess` 文本模式必须声明 `encoding` | 中文 Windows 上 `text=True` 会静默拿到空输出 |
| 统一测试入口 + basetemp 被 gitignore | 堵住临时目录泛滥 |

**棘轮基线怎么用**：你拆掉了一个基线里的函数，`test_complexity_baseline_only_shrinks`
会立刻提醒你把它从 `COMPLEXITY_BASELINE` 删掉。基线**只该变短**，不许把上限改大。

---

## 五、构建与发布

### 常用命令

```bash
cd frontend

npm run build              # 前端：main + renderer + assets
npm run build:backend      # PyInstaller sidecar（用仓库 .venv）
npm run package            # 完整安装包（electron-builder）
npm run package:dir        # 只出免安装目录
npm run verify:packaged    # 校验打包产物

# 发布门禁（只检查、不产出）
python ../frontend/tools/build-release.py --check-only
```

> 具体参数以 `python frontend/tools/build-release.py --help` 为准 ——
> 本轮的发布专项代理扩充了版本一致性检查、产物校验与构建清单。

### 发布前置条件（缺一不可）

- [ ] §一 Python 隔离安全套件通过；实际发布所需的集成/硬件测试另行授权、隔离、记录，不把排除项算通过。
- [ ] 前端默认门禁：在 frontend 下执行 `npm run check` 与 `npm run test:acceptance-contracts`；旧 verify/probe 不得继承用户环境直接运行。
- [ ] **所有版本位点一致**（package.json / electron-builder 配置 / Inno Setup 脚本 /
      Python 包版本 / 协议版本 / 文档文案）—— 有不一致直接失败
- [ ] sidecar 重建成功（否则修好的 bug 不会进打包产物）
- [ ] 主程序 exe 存在（**缺失必须是硬失败**，不能静默通过）
- [ ] 安装包按当前版本过滤（不能捡到上一次的旧 exe）
- [ ] 构建清单落盘：commit / 产品版本 / 协议版本 / 工具链 / 依赖锁摘要 / 执行过的测试 / 产物与 sha256

### 两条硬纪律

1. **构建前置检查只许检查，不许静默改写源码。**
   `frontend/package.json` 的 `prebuild` 过去会跑 `scripts/sanitize.mjs` 直接改源文件 ——
   这类"构建时偷偷改代码"的行为已经收口（见 `build-release.py` 的 `--allow-source-rewrite`），
   默认不写源。
2. **正式发布目录不覆盖。** 约定输出到 `D:\OneDrive\app_dve\Release`；
   构建/验证实验一律走 `--dir-only` 或临时目录。

---

## 六、出问题怎么回滚

### 当前可靠恢复入口

以 [LAST_REPAIR_2026-09-27.md](LAST_REPAIR_2026-09-27.md) 的三个 Git revert 目标为准；从最终含文档 HEAD 的完整演练证据保存在仓库外。禁止把以下历史局部快照视为整版本恢复。`.backups/phase1_20260921_042735/` 在本轮复核为空，历史恢复命令不得执行。

### 历史维护跟进（2026-09-25/26，非当前执行入口）

本轮返修从基线 `main@1c0781e3d647846844040c21ed38f4882c5db93c` 开始；不要把下方“前一轮已提交批次”的回退命令套到本轮工作树上。本轮文件快照及逐项恢复指引位于：

- `.backups/public-components-followup-20260925-183459/`
- `.backups/ipc-contract-runtime-20260926-025434/`
- `.backups/catalog-race-followup-20260926-050646/`
- `.backups/pipeline-owner-followup-20260926-051723/`
- `.backups/ipc-envelope-followup-20260926-060740/`
- `.backups/pipeline-start-close-race-20260926-082119/`
- `.backups/handover-followup-20260926-045619/`
- `.backups/final-handover-contracts-20260926-100929/`
- `.backups/async-ack-admission-20260926-164656/`
- `.backups/pipeline-setter-atomicity-20260926-173607/`
- `.backups/pipeline-test-clock-isolation-20260926-193345/`
- `.backups/delivery-docs-followup-20260926-201929/`

每个目录的 `RESTORE.md` 记录快照文件、基线与恢复方法。先核对 `git status` 和目标路径，再依照单个阶段说明恢复；不要使用 `git reset --hard`、宽泛 `git clean` 或覆盖真实配置/用户数据。

- 上两行所对应批次的提交与旧演练已记录在 FINISH_2026-09-26.md；其仅代码回退不覆盖最终文档。当前完整恢复说明以 LAST_REPAIR_2026-09-27.md 为准。

### 前一轮已提交批次（与本轮返修分开）

返修前的五个既有提交为 `9cfc614`、`1d3b842`、`a99467e`、`afbe3c8`、`1c0781e`。2026-09-26 曾在 `E:\Hermes_data\cache\scratch\voxsub-r5-prev-revert-487\` 的隔离 clone 中实际按逆序 revert 这五个提交：结果树为 `6cfbbc7569b4627e3bf81d101f3455939ceb1e89`，revert 演练提交为 `d753a5f5d8b9591fe01b06db292643ea27206da0`，日志报告 `TREE_MATCH=yes` 与 `CLEAN_WORKTREE=yes`（`drill.log`）。该演练只证明前一轮批次在当时隔离基线上可回退；不属于 VoxSub 正式 `main` 历史，不证明本轮返修回退。

### 单次改动

每次改动前先建立带时间戳备份，并在对应 `RESTORE.md` 记录文件清单及恢复方法。`.backups/` 已在 `.gitignore` 中。配置、模型及发布产物不属于本轮返修回退范围；未经明确批准不得恢复覆盖。

### 配置出问题的回滚

- 配置损坏：程序会自己备份成 `config.json.corrupt-<时间戳>` 再回落默认值，把备份改名回 `config.json` 即可复原。
- **配置版本比程序新**：程序进入只读保护并给出明确提示 —— 这是**刻意**的，别绕过它去写文件，那会把新版本的字段抹掉。正确做法是升级程序。

---

## 七、常见坑（踩过并且已经固化成门禁的）

| 坑 | 现象 | 为什么难查 | 现在怎么防 |
|---|---|---|---|
| `text=True` 读中文 Windows 工具输出 | NPU 探测静默返回空列表 | 解码异常发生在 subprocess **读取线程**里，主流程看不见，只留一条资源警告 | 门禁强制 `encoding` 声明 |
| `migrate_models` 导错模块 | 打包版 `import_models` 必失败，源码运行完全正常 | 函数级 import，静态分析看不到；只有打包版才炸 | 回归测试 + spec `hiddenimports` 补全 |
| 未来版本配置被降级写回 | 用户"设置莫名其妙丢了" | 读的时候很安静，问题出在下一次保存 | `ConfigVersionTooNew` + 只读保护 |
| 长任务占住读循环 | 界面"卡死"，停止按钮没反应 | 命令都堆在管道里排队，日志看不出异常 | 控制命令插队 + 有 25 条测试盯着响应时间 |
| 各传各的 `--basetemp` | 仓库根堆了 244 个 `.pytest-*` | 没人敢删，也没人知道哪些还有用 | 唯一入口 + gitignore + 门禁 |
| 孤儿模块 | `voxsub/runtime_bootstrap.py` 零引用还留着 | 读代码时会以为它有用 | 本文档登记；要么接上要么删 |

---

## 八、文档地图

| 文档 | 内容 |
|---|---|
| `docs/ARCHITECTURE.md` | 分层、模块职责、状态所有权、后台任务规则、资源所有权规则、契约与配置规则 |
| `docs/MODULE_CATALOG.md` | **公共组件/服务目录**：每个公共组件在哪、谁管状态、怎么复用、不要做什么 |
| `docs/DECISIONS.md` | **重要技术决策及理由**（含"我们放弃了什么"）|
| `docs/HANDOVER.md`（本文件） | 上手路径、加功能流程、测试分层、门禁清单、构建发布、回滚、常见坑 |
| `docs/MAINTAINABILITY_REPORT.md` | 本轮改了什么、14 项缺陷核验表、验证证据、NOT_RUN、备份回滚、剩余风险 |
| `contracts/README.md` | 怎么加一条命令/事件、协议版本怎么递增、契约测试红了怎么办、待修清单 |
| `contracts/*.json` | 通信契约的**单一来源**（protocol / commands / events / error-codes） |
| `STATUS.md` / `TODO.txt` | 项目当前进度与任务追踪（每次里程碑后更新） |
| `AGENTS.md` | 给 AI 代理的操作纪律 |

## 九、契约是什么、为什么要理它（新人最容易忽略的一块）

前后端之间的每条命令、每个事件都定义在 `contracts/`（JSON Schema，单一来源），
`tests/test_contracts.py` 会**双向**比对：代码里有的契约里必须有，契约里有的代码里必须发。

为什么值得：字段名对不上是**静默**的。TS 里可选字段读不到就是 `undefined`，
不报错、不崩，只是"任务跑完了界面没反应"。本轮就抓出一例（后端发 `action`、
前端读 `status`）。所以契约里有个**棘轮清单**（`knownGaps` / `uiFieldDrift`）：
已知的不一致必须逐条登记，多一笔会红、修好不删登记也会红 —— 缺口只能被显式登记，
不能被静默接受。

运行时校验现已接入生产 `IpcLoop`：请求信封在 `_split_request()` 解析边界检查，已知命令的 args 在 `_admissible()` 检查；`_send()` 校验应答信封、成功结果、异步受理回执及业务事件。Pipeline/handler 通过 `ipc_protocol._event()` 发出的模块级 producer 事件由 `ipc_server.main` 安装的 dispatcher 转入同一校验边界；只有契约诊断日志跳过自身复校以避免递归。

`CONTRACT_ENFORCE = False` 仍是默认模式：违规记 WARNING 并继续发送；注册表加载失败会记 ERROR，兼容模式继续运行但没有 schema 校验。显式设为 `True` 时请求/args/响应/事件违规会拒绝或丢弃，注册表缺失 fail-closed。严格模式目前只在隔离 IpcLoop/schema 测试中验证；所有真实 renderer 调用、冻结 sidecar 与成品打包兼容仍是 **NOT_RUN**。成功 response 的 `data` 有命令级结果校验；错误回复没有成功结果可验。迁移成功终态另校验与 `start_migration` 关联的报告；这不等于 Electron 端到端证明。

## 2026-09-26 收尾交接

最新源码提交、验证、回退演练与剩余风险统一见 [FINISH_2026-09-26.md](FINISH_2026-09-26.md)。代码三个本地提交已完成，未推送；未知清理仍须人工核对，不存在自动恢复通道。下方为早先中间记录，未提交/待复审/回退 NOT_RUN 状态已经过期。

### 早先返修快照（历史）

基线为 `main@1c0781e3d647846844040c21ed38f4882c5db93c`（返修前本地 `main` 较 `origin/main` ahead 14）；当前修复均仍是未暂存工作树改动，尚未分阶段提交，禁止推送。当前快照目录及恢复方法见本文件 §六与各目录 `RESTORE.md`。

### 当前复修要点

- IPC：严格模式的异步受理回执改为在 `JobRunner.on_queued` 的可见队列入队前校验；校验拒绝时应答 `contract_violation`，不登记请求、不接受任务。单项 RED→GREEN 行为测试以真实 `JobRunner` 和无副作用假服务验证该边界。
- Pipeline：资源 setter 在同一 `_state_lock` 内完成准入检查与变更，与 `_claim_start()` 串行；覆盖语言、模型目录、STT、翻译器、ASR 模型和 ASR tuning。停止/关闭超时仍保留运行时 owner，直至 worker 确实退出。
- 测试隔离：计时用例以 Pipeline 模块局部 clock shim 替代全局 `time.monotonic` 替换；B 模式 loopback 选择测试使用 fixture 设备，避免调用真实设备枚举。

### 本轮验证证据

| 范围 | 命令/结果 | 解释 |
|---|---|---|
| Python 策略安全套件 | `.venv/Scripts/python.exe -m pytest -q -rs -m "not integration and not hardware_audio"`：`883 passed / 5 skipped / 17 deselected / 1 xfailed`，退出码 0 | `LOCALAPPDATA`、`TEMP/TMP`、缓存与 basetemp 指向 Hermes scratch；5 个 skip 包括真实模型/素材缺失与 Windows 符号链接权限；保留 1 条 `tarfile.extractall` DeprecationWarning。被显式排除的 integration/hardware 不视为通过。 |
| Python Pipeline/IPC 专项 | 相关模块与 fake-only lifecycle selector：`250 passed`；时钟/设备选择专项 `3 passed` | 未运行真实设备、模型、音频、迁移或主进程。 |
| 前端门禁 | `npm run check`：退出码 0；包含 `tsc --noEmit`、色板和 `test:logic` | 页面生命周期汇总 `113/113`；catalog 乱序 harness `8/8`。 |
| Acceptance contracts | `npm run test:acceptance-contracts`：`25/25` 正例及 11 个内存负对照通过 | 这是结构/负对照证据，不是 IPC 或 Electron 运行时证明。 |

### 未验证与执行边界

- 三路最新独立只读复审结果待回传；收到后按发现复核并必要时返修。
- 本轮完整返修集成 revert 演练为 **NOT_RUN**；上一轮 scratch clone 的逆序 revert 结果只属于前一轮。
- Electron GUI、真实 sidecar/冻结包、真实 migration、OCR、音频采集/播放、模型推理和硬件工作流均未运行；`CONTRACT_ENFORCE=False` 仍为默认。
- 曾有一次未设置隔离 `LOCALAPPDATA` 的 RED Pipeline 单项测试将日志写到本机 `%LOCALAPPDATA%\\VoxSub\\logs\\voxsub.log`；该日志未被读取或清理。此后 Python 测试均将 LocalAppData 和临时目录定向至 Hermes scratch。一次较早的默认标记套件也实际调用过旧版 loopback 枚举测试（仅设备枚举、未采集/播放）；该测试现已改为 fixture 设备，最终安全套件显式排除 `integration` 和 `hardware_audio`。
- `scripts/run_tests.py` 曾有退出码 1 且未保留失败正文；本轮未重跑该清理型 wrapper，改用显式 scratch 的安全标记测试命令。不得据直接 pytest 的结果声称该 wrapper 已通过。
- 本轮不覆盖 `%LOCALAPPDATA%` 真实配置、模型、安装包或 `Release`；无提交尚未完成，禁止 push。
