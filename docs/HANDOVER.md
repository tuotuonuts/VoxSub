# 语幕 VoxSub —— 交接手册（新功能接入 / 测试 / 构建 / 发布 / 回滚）

> 目标：一个没参与过本项目的人，只靠这份文档 + 仓库里的门禁，就能安全地加功能、
> 跑测试、出构建、必要时回滚。
>
> 配套阅读：`docs/ARCHITECTURE.md`（模块边界与状态所有权）、
> `docs/MAINTAINABILITY_REPORT.md`（本轮改了什么、还剩什么）。

---

## 一、最短上手路径

```bash
cd D:/OneDrive/app_dve/VoxSub

# 1. 后端全量测试（唯一入口，固定隔离临时目录）
./.venv/Scripts/python.exe scripts/run_tests.py -q

# 2. 跨进程集成（真起 sidecar 走真管道；不参与默认选择，要显式点名）
#    必须带上 "and not hardware_audio"：显式 -m 会覆盖 pytest.ini 里的默认排除，
#    否则会选中会真从扬声器播声音的 loopback 用例。
./.venv/Scripts/python.exe scripts/run_tests.py tests/test_ipc_integration.py -m "integration and not hardware_audio"

# 3. 前端类型检查 + 纯逻辑测试
cd frontend && npm run check

# 4. 一条命令跑完前端全部校验
npm run verify

# 5. 发布门禁（只检查，不产出任何东西）
python frontend/tools/build-release.py --check-only
```

前四条都绿了，再动手。

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
5. **测试临时目录只有一个位置**：`scripts/run_tests.py` 管理的 `.pytest-run/`。
   不要手工往仓库根传 `--basetemp` —— 那正是 244 个 `.pytest-*` 的来源。
6. **每个"已修复"都要有真实运行输出**，没跑的写 NOT_RUN 加原因。
   严禁编造运行结果 —— 这比不做更糟。

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

- [ ] Python 全量测试绿：`./.venv/Scripts/python.exe scripts/run_tests.py -q`
- [ ] 前端校验绿：`cd frontend && npm run verify`
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

### 单次改动的回滚

每次改动前先按约定建带时间戳的备份：

```bash
cd D:/OneDrive/app_dve/VoxSub
mkdir -p .backups/<改动名>-$(date +%Y%m%d-%H%M%S)
cp frontend/backend/ipc_server.py .backups/<改动名>-<时间戳>/
```

回滚就是把备份拷回去。`.backups/` 已在 `.gitignore` 里。

### 本轮改动的回滚

见 `docs/MAINTAINABILITY_REPORT.md` §七，含具体的 `cp` 命令。

### 配置出问题的回滚

- 配置损坏：程序会自己备份成 `config.json.corrupt-<时间戳>` 再回落默认值，
  把备份改名回 `config.json` 即可复原。
- **配置版本比程序新**：程序进入只读保护并给出明确提示 —— 这是**刻意**的，
  别绕过它去写文件，那会把新版本的字段抹掉。正确做法是升级程序。

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

运行时校验已经接线（`IpcLoop._send()` 是唯一出站口），但 `CONTRACT_ENFORCE = False`：
**默认只把不一致记成 WARNING，不拒绝请求**。理由写在代码注释里 —— 契约写错不该比
没有契约更糟。翻成 `True` 的条件也写在那儿。
