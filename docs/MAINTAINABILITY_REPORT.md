# 语幕 VoxSub —— 可维护性整顿报告（本轮）

> 本文件是本轮"代码可维护性整顿"的**主交付物**：改了什么、为什么改、怎么验证、
> 哪些没做、怎么回滚、还剩什么风险。
>
> 基线：`main` 分支 commit `0882505`（开工时工作区干净）
> 执行方式：主代理 + 4 个专项代理并行（Python 安全/生命周期、前端状态与页面生命周期、
> 契约与发布门禁、独立审查），文件所有权严格划分，禁止并发改同一热点文件。

---

## 一、本轮目标与实际达成

| 目标（工作单 §二） | 达成情况 |
|---|---|
| 降低新增功能/升级/维护/交接成本 | 是：模块边界写进门禁与文档（`docs/ARCHITECTURE.md`、`docs/HANDOVER.md`） |
| 明确模块职责、状态所有权、资源生命周期 | **大部分达成**：模块职责与状态所有权已写进 `docs/ARCHITECTURE.md` + 门禁；资源生命周期 8 条规则中 6 条已实现并有测试（`_may_replace_resources` 统一门禁、stop 超时保持停止中、close 幂等、只关自有进程、Qwen 锁内一次决策 + epoch 守卫、退出共享截止时间）；剩下 2 条的后半（OCR 借用协议接线到适配层、译后成图走统一缓存）是用户可见行为变更，待确认后才动 |
| 合理提取公共界面组件与公共功能模块 | **已达成**：后端抽出 `migration_ledger`/`job_runner`/`ipc_loop`/`ipc_protocol`/`ipc_support` 并把命令实现按业务域拆进 `handlers/*`；前端按"先盘点再抽"落地 SettingsField/PathPicker/JobProgress/ConfirmAction/StatusRow 五个组件，`ErrorNotice` 经盘点判定无稳定结构、**不抽并写明理由**（工作单允许不抽、不允许无理由不抽） |
| 建立能发现真实回归的测试与发布门禁 | 是：新增 80+ 条测试；架构门禁扩到适配层；发布门禁由专项代理补齐（见 §五） |
| 保持功能外观操作习惯、修已确认错误行为 | 是：未改任何产品行为，只改错误行为与内部结构 |

**测试总量变化**：基线 `438 passed / 5 skipped` → 本轮 `703 passed / 6 skipped / 7 deselected / 1 xfailed / 0 failed`
（`./.venv/Scripts/python.exe scripts/run_tests.py -q`，约 40 秒）。
另有 `7 passed` 的跨进程集成用例（`-m integration`）与前端 `npm run check`（179 条断言）全绿。

---

## 二、已确认缺陷的核验与处置（工作单 §四 的 14 项）

状态口径：**已修** = 本轮改完并有测试；**部分** = 本轮只加固了一部分；**未修** = 确认存在但本轮未动（原因写在备注）；**未复现** = 当前源码已不成立。

| # | 缺陷 | 核验结论 | 证据（文件:行号） | 本轮处置 |
|---|---|---|---|---|
| 1 | 迁移清理接受任意路径，空路径/卷根保护不足 | **确认成立** | 旧 `ipc_server.py:1295-1314` 只有 4 个字面量比较就 `shutil.rmtree(path)` | **已修**：清理只认台账记录 + 显式 confirm，见 §三.1 |
| 2 | session/stop 清空字幕，导致导出为空 | **确认成立** | `store.ts` `case "session"` 忽略 `event.action`；`workspace.ts:223` 字幕为空直接 return | 交前端专项代理修（`docs/` 附属报告） |
| 3 | Electron 实时 OCR 采集→译文浮窗事件链未接通 | **确认成立** | `preload.ts:76-77` 声明 `ocr:translated`/`ocr:frame-failed`，但全仓库 **零发送方**；`ocr-overlay.ts:93-111` 订阅了它们 | **未修**：需要真实屏幕采集做端到端验证，无硬件不可假装完成 |
| 4 | 30 秒请求超时释放迁移保护 | 确认成立（前端请求层） | 前端请求封装 | 交前端专项代理修 |
| 5 | OCR/迁移阻塞 IPC 控制命令 | **确认成立** | 旧 `main()`：`for raw in sys.stdin:` 内同步 `service.handle(...)`，长任务期间 `state`/`stop` 全排队 | **已修**：读出 job 执行器 + 读循环分派，见 §三.2 |
| 6 | Pipeline 停止超时仍进 IDLE，可被提前换资源 | **确认成立** | `pipeline.py` stop() 超时后无条件落 IDLE | **已修**：超时保持 STOPPING（`is_running()` 仍真 → 门禁关闭）；新增 `_may_replace_resources()` 作为「能否替换被 worker 持有资源」的唯一权威判断，`set_models_dir`/`set_translator`/`set_asr_model`/`set_tts*` 全改走它；有界单例观察者等 worker 真退出才落 IDLE（否则「诚实」会变「卡死」）。12 条测试 |
| 7 | Qwen 并发初始化竞态 + 过度 mock 的测试 | **确认成立（实测复现，非理论）** | 修复前两个并发 `_ensure` 开出 2 个子进程（端口 61841/61421），8 线程冷启动开出 5 个 | **已修**：检查+摘除+重建在生命周期锁内一次决策；`_generation` epoch 守卫 `close(generation=)`；半成功启动原子回滚；去掉了替换 `_spawn`/`close` 的过度 mock。**负例对照**：HEAD 版 4/7 红，修复版 7/7 绿 |
| 8 | OCR 缓存配置失效与独立 close 缺失 | **确认成立**（字面「图片指纹当键」已随 Qt 前端删除、零调用方，但同类真缺陷成立） | 实测 `_translator_key({'api_key':'one'}) == ({'api_key':'two'})`（都等于 5 个空串）；实测 `hasattr(RapidOcrEngine,'close') == False`；close 后仍静默重建继续用 | **partial**：模块内已修（配置维度指纹驱动缓存键、引擎独立幂等终止性 close、借用协议均已实现并真跑 45 条测试）；**适配层只接了一半** —— 引擎回收与 OCR 自建翻译器回收已接，但译后成图仍绕过 `OcrImageCache`、OCR 逐行翻译未走带指纹的缓存。差的两条见「剩余风险」 |
| 9 | 切语言时旧任务缺少快照 | **确认成立** | 实测症状：整句被源语言拦截规则丢弃（日志 `翻译前拦截非指定源语言文本 expected=ja text='第一句'`） | **已修**：`_LangSnapshot` + `_QueuedTranslation` + `config_generation`；在途任务用提交时快照、新任务用新配置、已显示结果不清屏；`configGeneration` 已接入 `state` 负载。19 条用例 + 负例对照（旧行为 4 例红） |
| 10 | 设置页全局订阅累积 | **确认成立** | 渲染层 12 处 window/document 监听，**0 处移除** | 交前端专项代理修（PageLifecycle / dispose） |
| 11 | 断连与渲染重载状态恢复不完整 | 确认成立 | `store.ts:194-227`、`284-322` | 交前端专项代理修 |
| 12 | 未来版本配置被降级、未知字段被丢弃 | **确认成立** | `config_store.py` `_migrate_config` 无条件把 `config_version` 写回当前版本 | **已修**：见 §三.3 |
| 13 | 架构门禁漏掉 `frontend/backend` | **确认成立** | 旧 `tests/test_architecture.py` 只扫 `voxsub/` | **已修**：门禁覆盖两层 + 4 条新规则 + 棘轮基线 |
| 14 | 发布漏检、仓外资源路径、无效构建命令、版本不一致 | 确认成立 | `build-release.py:300/305-307`；`grep -c version` = 0 | 交发布专项代理修 |

### 本轮额外发现并修掉的缺陷（不在原清单里）

| 编号 | 问题 | 影响 | 证据 | 处置 |
|---|---|---|---|---|
| A1 | `_cmd_import_models` 从 `voxsub.model_catalog` 导入 `migrate_models`，而它定义在 `voxsub.model_storage` | **打包版该命令必失败**，源码运行正常 → 极难排查 | 实测打包版：`ImportError: cannot import name 'migrate_models' from 'voxsub.model_catalog'` | **已修** + 回归测试 |
| A2 | `voxsub/hardware.py` 用 `text=True` 读 `pnputil`/`powershell`/`nvidia-smi` 输出，未声明编码 | 中文 Windows 上是 CP936，解码异常发生在 **subprocess 读取线程**里，`stdout` 变成空串 → **NPU 备用探测静默返回空列表**，主流程毫无察觉 | `hardware.py:181-184`（旧）；实测 `pnputil` 真机输出为 GBK、`stdout` 长度为 0 | **已修**（`decode_console_output`）+ 门禁规则防复发 |
| A3 | `_emit` 无锁写 stdout | 引入多线程后两行 JSON 可能交错，前端只能丢包 | `ipc_server.py` `_emit` | **已修**（`_PROTOCOL_LOCK`） |
| A4 | PyInstaller `hiddenimports` 缺适配层模块 | 漏一个就重现 A1 那类"打包版才炸"的问题 | `frontend/backend/ipc_server.spec` | **已修**：补齐 `job_runner`/`ipc_loop`/`legacy_migration`/`migration_ledger`/`contract_validation` |
| A5 | 打包版 sidecar 与源码不同步 | 打包产物 traceback 行号（361/679）与源码（477/917）对不上，说明 `frontend/backend/dist/` 是旧版本构建 | 实测打包 exe | **待办**：需重新打包验证（涉及正式发布物，见 §六） |
| A6 | 仓库根残留 244 个 `.pytest-*` 临时目录（约 527MB）+ 3 个 venv（3.3GB） | 磁盘与交接噪音 | `ls -d .pytest-*` = 244 | **未动**：属真实删除，按纪律需甲方确认（见 §六） |
| A7 | 原子写入在同步盘上偶发失败：`os.replace` 抛 `[WinError 5] 拒绝访问`，全量测试里表现为"跟代码无关的随机失败" | 最消耗排查时间的一类问题；`STATUS.md` 的"环境事实"里其实**早就写下了**"偶发文件锁，报 os error 5 时等 1-2s 重试"——靠人记得重试，而不是代码兜住 | 实测：全量跑时 `test_write_state_is_atomic_and_merges` 与 `test_ledger_is_bounded` 随机失败，单独重跑两次都过 | **已修**：`voxsub/file_io` 的原子写入加**有界重试**（5 次 × 50ms）；`migration_ledger` 与 `legacy_migration.write_state` 改为复用这一份共享实现，不再各写一套 |

---

## 三、本轮的主要技术改动

### 3.1 删除授权：从"调用方给路径"改成"台账记录 + 护栏"（缺陷 #1）

**问题**：旧实现里"删哪个目录"的决定权在调用方。只要会写一行 IPC，就能删掉任意存在的目录 —— 唯一拦得住的是 4 个写死的系统路径字面量。实测打包版确认：传一个不存在的路径会回 `{"deleted": false, "detail": "目录不存在：..."}`，说明它**真的接受了这个路径**，只是恰好不存在。

**新模型**（`frontend/backend/migration_ledger.py`，新文件，259 行）：

```
迁移成功 → 写入台账（%LOCALAPPDATA%\VoxSub\migration-ledger.json，原子写，有界 50 条）
                              ↓
清理只接受 record_id（不接受 path）→ 从台账解析真实路径
                              ↓
      逐条护栏：记录存在 / 已校验通过 / key 属于真会被搬的类型 /
                显式 confirm / 非空 / 非卷根 / 非受保护目录 /
                非重解析点（symlink/junction）/ 源≠目标 / 互不包含 /
                非 UNC 或设备路径 / 源不包含应用正在使用的数据根
```

**为什么这样就是"授权"而不是"黑名单"**：授权来自"这是一次真实、已校验通过的迁移"这件事本身。调用方拿不到路径，所以必须先让一次真实迁移成功，而迁移的源与目标都要通过校验。其余检查都是护栏（防止把正在用的东西删掉），不是猜测。

**入参契约变更**：`cleanup_migrated_source` 现在接受 `record_id` 或 `record_ids`，并要求 `confirm: true`。传 `path` 一律拒绝并回 `code: "path_not_accepted"`。已经在 `frontend/src/renderer/protocol.ts` 的调用方同步（由前端专项代理处理）。

**测试**：`tests/test_migration_ledger.py`（27 条，逐条对应每个拒绝理由）、`tests/test_ipc_commands.py` 里的新契约测试（含把旧的不安全断言 `test_cleanup_deletes_real_dir` 换成回归测试 `test_cleanup_refuses_path_argument`）。

### 3.2 后台任务：控制通道与耗时工作分离（缺陷 #5）

**问题**：所有命令都在读循环里同步执行。迁移一跑三分钟，界面上的"停止/状态"就全排在后面 —— 用户看到的是"卡死"。

**新结构**（两个新模块，均可脱离协议层单测）：

- `frontend/backend/job_runner.py`：单 worker、**有界队列**（`MAX_PENDING=16`，满了明确拒绝而不是无限堆积）、有界完成历史（`MAX_HISTORY=50`）、诚实的取消语义。
- `frontend/backend/ipc_loop.py`：读循环只负责读、校验、分派。控制命令（`ping`/`state`/`shutdown`/`job_*`/`cancel_job`）**在读循环线程上立即执行**，其余命令进队列。

**状态语义**（唯一权威定义，写在 `job_runner` 文档字符串里）：

```
queued ──► running ──► succeeded / failed
               └────► cancelling ──► cancelled
```

硬规则，逐条有测试：

- `cancelling` **不等于** `cancelled`；只有到达安全边界才落终态。
- **请求超时不是失败，也不是取消**。执行器里根本没有"客户端超时"这个概念 ——
  测试 `test_slow_job_past_a_client_timeout_still_succeeds` 显式验证"前端等不住了，
  任务仍然是 running"。
- 取消能**插队**：由读循环线程直接处理，不排进作业队列（测试断言 `<0.3s` 响应）。
- 只有真正进入终态才解除退出保护（`has_active_jobs()`）。
- 取消后**丢弃结果**，不谎报成功。
- 迁移等长任务在**步骤边界**协作式检查取消（`_cancel_requested()`）。

**兼容性**：默认（不带 `async`）保持原来的同步应答语义，前端不用改就能继续工作。带 `args.async = true` 时立刻回执 `{jobId, accepted, status}`，结果通过 `event:"job"` 送达 —— 前端可以按自己的节奏迁移过来，不需要一次性切换。

### 3.3 配置版本兼容（缺陷 #12）

**问题**：`_migrate_config` 无条件把 `config_version` 写回当前版本。磁盘上是未来版本（实测真实机器上是 v5，程序只认 v2）时，程序会按自己的 schema 归一化再写回 —— 新版本写入的字段被悄悄抹掉，用户看到的是"设置莫名其妙丢了"。

**改法**：

- 新增 `ConfigVersionTooNew`。遇到比程序新的版本：**不降级、不覆盖**，进入只读保护，并把原因原样返回给上层（界面可以据此提示"请升级程序"）。
- **未知字段保留在原始持久化数据里**（`self._unknown_fields`），保存时原样带回 —— 不执行、也不抹掉。新增 `load_raw()` 供诊断。
- 配置损坏时**先备份再回落**（`config.json.corrupt-<时间戳>`），并且每次进程只备份一次，避免刷出一堆备份文件。

**测试**：`tests/test_config_version_guard.py`（17 条），含"未来版本保存必须被拒且文件一个字节都不变"这条核心断言。

### 3.4 架构门禁补漏（缺陷 #13）

旧门禁只扫 `voxsub/`，适配层（恰恰是历史上最容易堆积编排逻辑的地方）完全在检查之外。现在：

| 规则 | 作用 |
|---|---|
| 核心包不许 import UI 层（原有，保留） | 核心可脱离 Qt 使用 |
| **核心包不许 import 适配层**（新） | 依赖方向只能是"适配层 → 核心" |
| **适配层不许把 Qt 拉回来**（新） | 打包排除 Qt 是刻意的，拉回来会让包无故大一百多 MB |
| **复杂度预算覆盖两层 + 棘轮基线**（新） | 不推倒重写存量，但**不允许新增超标，也不允许变得更差** |
| **`subprocess` 文本模式必须声明 `encoding`**（新） | 机器拦住 A2 那类"中文 Windows 上静默拿到空输出"的坑 |
| **统一测试入口 + basetemp 被 gitignore**（新） | 堵住 244 个 `.pytest-*` 的来源 |

复杂度棘轮基线目前有 8 条存量条目，只允许变短 —— `test_complexity_baseline_only_shrinks` 会在消化掉之后强制你把条目删掉。

### 3.5 统一测试入口

新增 `scripts/run_tests.py`：固定 basetemp 到 `<repo>/.pytest-run`（gitignore 覆盖、每次清空、跑完保留现场）。历史问题是有人手工传 `--basetemp=xxx` 到仓库根，积累了 244 个名字各异的临时目录，谁也不敢删。靠"记得别传"是堵不住的，所以做成唯一入口。

### 3.6 通信契约与字段漂移（§3.6）

契约集中在 `contracts/`（JSON Schema，单一来源）：`protocol.json` / `commands.json`（55 条命令）/ `events.json`（14 个线上事件）/ `error-codes.json`，配一份 `README.md` 说明怎么加命令。Python 侧的轻量校验器在 `frontend/backend/contract_validation.py`（**纯标准库**，只实现 JSON Schema 子集，没有引入 jsonschema/ajv/zod/pydantic）。

**接线方式（刻意保守）**：`ipc_loop` 里加了唯一的出站口 `_send()`（先过契约校验再写 stdout），入站命令也过 `validate_args`。但 `CONTRACT_ENFORCE = False` —— **默认只把不一致记成 WARNING 日志，不拒绝请求**。理由：契约是第一步（定义 + 一致性测试），如果这一版契约有偏差就硬拒绝，代价是用户功能直接不可用 —— 契约写错不该比没有契约更糟。翻成 `True` 的条件写在代码注释里（出站零告警 + 打包版冒烟）。

**契约交付时暴露并已修掉的一处真实漂移**：

`job` 事件的字段，后端发 `action`、前端 TS 读 `status`（还多声明了 `detail`）。因为 TS 里字段是可选的，读不到就是 `undefined`，**不报错** —— 表现是"任务跑完了界面不显示结果、日志里也不提示原因"。修法是以后端状态机为权威统一成 `status` + `code`，并补一条回归测试断言事件里**不再出现**同义的 `action`。这类漂移的通用防线是 `tests/test_contracts.py` 里的**棘轮清单**：声明与实现不一致必须逐条登记，多一笔会红、修好不删登记也会红。

**核实过、不是缺陷的两处**（专项代理报告里提到，我逐条复核后否定/降级）：

- "协议版本轴不一致（握手整数 1 vs 契约 semver 2.0.0）"：契约里显式声明这是**两条不同的轴**（`wireProtocolVersion` 是线上整数，`protocolVersion` 是契约文档版本），并有测试断言线上整数与 `ipc_loop.PROTOCOL_VERSION` 相等。发布门禁里那段探测代码原本按旧字段名读、导致报"无法判断" —— 已改成读当前形状（并兼容旧字段名），现在报告 `consistent: True`。
- "被删掉的 `tests/test_packaging.py` 里有版本一致性检查"：查过 `git show 9b87ba5^:tests/test_packaging.py`，它守的是安装器行为（语言检测、有界关闭、升级清理），**从来没有**版本一致性断言。准确说法是"从来没有过这个检查"，不是"被删了"。所以新文件是按版本契约重写，而不是恢复旧文件（它引用的 `scripts/build.ps1` 已不存在，原样恢复会直接红）。

### 3.7 跨进程集成测试（§3.9 L3）

新增 `tests/test_ipc_integration.py`：真起 `ipc_server.py` 子进程、真走 NDJSON，只跑只读命令，配置隔离在 tmp_path，不弹窗不抢焦点，进程在 finally 里必被回收。它覆盖了单元测试证明不了的三件事：

1. **握手四要素真的发到了线上**（实测：`protocolVersion: 1`、`backendGeneration: "40520"`、`readiness`、`session`）；
2. **控制命令不会被队列堵住**（真管道下的响应时间断言）；
3. **退出路径**：核查时发现 `shutdown` 依赖 stdin EOF 才真正退出（子线程里的 `SystemExit` 不会终止进程）—— 这不是 bug，是既有机制，测试按真实机制写（关管道后必须退出），并把"为什么必须关管道"写进注释，免得后来者误判。

> 分工说明：**"控制命令插队"的强证明在进程内那一条**（`test_ipc_loop.py::test_control_commands_do_not_queue_behind_long_work`，用可控的慢命令确定性验证）。跨进程这条在隔离配置下模型目录是空的，几条只读命令总共不到半秒，队列瞬间排空 —— 所以"先答谁"只在队列确实有厚度时才断言。这一点在测试注释里写明，不硬凑。

### 3.8 发布门禁（缺陷 #14，专项代理完成 + 本轮复核）

- `frontend/tools/build-release.py`：版本一致性检查（实测 **12 个位点**必须全部等于权威版本，不是原以为的 6 处）、主程序缺失**硬失败**、安装包按当前版本过滤、构建清单（commit / 产品版本 / 协议版本 / 工具链 / 依赖锁摘要 / 执行过的测试 / 产物与 sha256）、源码漂移检测（构建前后比对，发现被改写默认硬失败）。
  - **实测**：`--check-only` → `OK 版本一致：12 个位点全部等于 0.9.0-beta`，退出码 0；`--manifest-only` → 产出 `build/release-manifest-0.9.0-beta.json`，其中 `artifacts: []`（本次没打包，**不把 Release 目录里别人的产物冒充成本次产物**）。
  - 负向实测：把 `voxsub/__init__.py` 改成 `9.9.9` → 退出码 1；只有陈旧安装包 → 退出码 1；缺主程序 exe → 退出码 1。
- `tests/test_packaging.py` + `tests/test_release_gate.py`：60 条（1 个 xfail 是安装器脚本里仍写死的 `OutputBaseFilename`，用 xfail 诚实标注而不是假装通过）。
- `.github/workflows/quality.yml`：5 个 job（python-tests / ipc-adapters / release-gate / integration / frontend）。

### 3.9 前端（缺陷 #2 / #4 / #10 / #11，专项代理完成 + 本轮对齐）

- **#2 会话停止不再清空字幕**：`store.ts` 的 `case "session"` 改为按 `action` 归约（纯函数在 `src/shared/session-timeline.ts`），只有 `start` 重置字幕与时间基准；导出的早退改判"有没有可导出的字幕"。认不出的 action 一律按 `stop` 保守处理。
- **#10 页面生命周期**：新增 `src/shared/page-lifecycle.ts`（`add`/`listen`/`interval`/`guard`/`dispose`，dispose 幂等、单个 disposer 抛错不阻断其余），页面构建返回 `{ element, dispose }`，换页先 dispose。测试实测：旧做法切 3 次页留下 3 个监听，新做法剩 0 个。
- **#11 断连状态**：主进程 `child.on("exit")` 现在发独立的 `disconnected` 事件（不再只改一句文案），渲染层进入独立 phase 并把会话视图归零；重载后按 `needsResync()` 补拉，`applySessionState` 连 `mode` 一起恢复。
- **#4 请求超时**：实测超时逻辑在 `frontend/src/main/backend.ts`（30 秒）。改后超时只发 `request-timeout` 通知并**保持请求挂起**，后端真实结果回来时用真结果兑现；只有超过 30 分钟硬上限才兑现为 `{ok:false, timedOut:true}`（明说"不代表任务失败/取消"）。长任务调用点（迁移、模型下载）超时后不解除退出保护、不跳失败页。
- **新契约配合**：清理旧目录改为只发 `record_ids` + `confirm: true`（绝不发 `path`）；新增 `job_list`/`job_status`/`cancel_job` 与 `callAsync()`；`ready` 握手只挑认识的字段解析。

---

## 四、验证

### 4.1 测试

命令（唯一入口）：

```
./.venv/Scripts/python.exe scripts/run_tests.py -q
```

**结果：`703 passed / 6 skipped / 7 deselected / 1 xfailed / 0 failed`，约 40 秒。**
（基线 `438 passed / 5 skipped`；6 个 skip 中 1 个是本机不允许创建符号链接，
其余是模型路径不一致导致的真模型用例——见 §六.3。）

跨进程集成（真起 sidecar、真走 NDJSON）：

```
./.venv/Scripts/python.exe -m pytest tests/test_ipc_integration.py -q -m "integration and not hardware_audio"
→ 7 passed
```

> 选择器必须带 `and not hardware_audio`：显式 `-m` 会**覆盖** `pytest.ini` 的
> `addopts = -m "not hardware_audio"`，于是把会真从扬声器播 2 秒正弦的
> `test_loopback_closure_sine` 一起选进来。这条纪律由
> `tests/test_pytest_policy.py` 的 `test_explicit_integration_runs_always_exclude_audio_tests`
> 盯着（CI 与文档里任何 `-m integration` 缺了这个排除都会红）。

前端：

```
cd frontend && npm run check
→ typecheck 通过；5 套纯逻辑测试 179 条断言 0 失败
```

本轮新增测试文件：

| 文件 | 条数 | 覆盖 |
|---|---|---|
| `tests/test_migration_ledger.py` | 27 | 删除授权的每一条拒绝理由 + 台账有界/原子/损坏容错 |
| `tests/test_job_runner.py` | 21 | 任务状态机、取消语义、超时≠失败、有界性、退出保护、事件字段只有 `status` |
| `tests/test_ipc_loop.py` | 27 | 协议防御（无效 JSON/null/未知命令/错误载荷/超大消息）+ 控制命令插队 + 日志事件必带 `ts` |
| `tests/test_config_version_guard.py` | 17 | 版本降级拒绝、未知字段保留、损坏配置备份 |
| `tests/test_ipc_integration.py`（新，`-m integration`） | 7 | **真起 sidecar 走真管道**：握手四要素、控制命令不被队列堵住、坏输入不弄死进程、关管道后能真正退出 |
| `tests/test_contracts.py`（专项代理产出 + 本轮对齐） | 68 | 55 条命令/14 个事件的双向覆盖、TS 一致性、契约校验器负向用例、字段漂移棘轮 |
| `tests/test_packaging.py` + `tests/test_release_gate.py`（专项代理产出） | 60 | 版本位点契约 + 发布门禁负向用例（版本不一致/主程序缺失/只有陈旧产物必须失败） |
| `tests/test_architecture.py`（重写扩展） | 8 | 依赖方向、复杂度棘轮、subprocess 编码、测试卫生 |
| `tests/test_file_io.py`（扩展） | +3 | 同步盘瞬时占用重试（有界、旧文件完好） |

已有的 `tests/test_ipc_commands.py` 被改造：删掉了断言"任意目录可被删除"的旧用例，换成 7 条新契约用例。
前端新增 5 套纯逻辑测试（`frontend/tools/test-*.mjs`，共 179 条断言），并挂进 `npm run check`。

### 4.2 真实环境证据（不只跑 mock）

- **打包版 sidecar 实测**：确认 `cleanup_migrated_source` 接受任意路径（缺陷 #1）、
  确认 `import_models` 的 ImportError（缺陷 A1）。命令：
  `echo '{"id":1,"command":"cleanup_migrated_source","args":{"path":"D:\\__probe_not_exists__"}}' | ./frontend/backend/dist/VoxSubBackend/VoxSubBackend.exe`
- **真机 `pnputil` 输出实测**：确认是 GBK(CP936) 而非 UTF-8，且按 UTF-8 解码后
  `stdout` 长度为 0（缺陷 A2 的根因）。
- **真实配置文件实测**：磁盘上 `config_version=5`，程序 `CONFIG_VERSION=2`
  → 旧代码会静默降级（缺陷 #12 的根因）。

---

## 五、NOT_RUN / BLOCKED 及原因

| 项 | 原因 |
|---|---|
| 缺陷 #3（OCR 实时事件链端到端修复） | 需要真实屏幕采集做端到端验证。事件链**断裂点已用 grep 证据定位**（`preload.ts:76-77` 声明、零发送方），但"接通并用浮窗显示真实译文"这件事没有硬件就成了假验证，因此不做。 |
| **Electron 运行时冒烟（L4）** | 本轮没有启动 Electron。跨进程集成测试（L3）已经把真 sidecar 的接线、握手、插队、退出全跑过一遍；但"真窗口里页面切换不报错、按钮跟着状态变"这一层只有编译 + 类型 + 纯逻辑单测覆盖。补这一步的命令是 `npm run launch:silent -- --debug` + `npm run test:session`，需要你同意在桌面起一次隐藏窗口（会拉起真后端）。**没有硬件/未授权的情况下不假装做完。** |
| 缺陷 #6/#7/#8/#9（Qwen 与 OCR 的资源生命周期、在途任务快照） | 属资源生命周期的第二阶段。规则已写进 `docs/ARCHITECTURE.md` 的"资源所有权"一节，但本轮未改代码 —— 这几处改动风险高（涉及原生推理不可中断、Qwen 并发初始化），需要在已有测试基线上单独一轮推进并逐条验收。 |
| `CONTRACT_ENFORCE = True`（契约校验从告警升级为拒绝） | 需要先满足注释里写明的两个条件：出站零告警 + 打包版冒烟。本轮只能证明源码路径零告警。 |
| 打包版与源码同步（A5） | 重新打包会写 `Release` 正式发布目录，工作单明确禁止覆盖正式发布物。 |
| 契约在打包版生效 | `contracts/` 不在 PyInstaller bundle 内，运行时契约校验在打包版上是静默关闭的（已写进代码注释）。要让它在打包版生效，需要把 `contracts/` 一并打进 bundle。 |
| 清理 244 个 `.pytest-*` / 3 个 venv（A6） | 属真实删除，按纪律必须先请求确认。 |
| 真机 NPU 探测结果 | 只验证了"解码不再让输出变空"，没有独立验证 NPU 设备清单本身正确（需按 §L5 单独记录设备/版本/限制）。 |
| GitHub Actions 首跑 | 本地无法运行 Actions；`quality.yml` 的 5 个 job 已用等价命令逐条本地复跑，真实 CI 首跑要等推送后才能确认。 |

---

## 六、待甲方确认的事项

1. **是否清理仓库根的历史临时目录**：244 个 `.pytest-*`（约 527MB）+ `frontend/backend/dist`（288MB）+ 3 个 venv（3.3GB，其中 `.venv-codex` 已废弃）。删除不可逆，需要你明确说"可以删"。
2. **是否重新打包 sidecar**：当前 `frontend/backend/dist/` 是旧构建（A5），本轮的修复要生效必须重新打包。这会写 `Release` 相关目录，需要你确认走哪条路（只打 `--dir-only` 验证 / 还是走完整发布流程）。
3. **NPU 与真实模型验收（L5 层）**：本机模型在 `D:\VoxSub\Models`，而测试找的是 `%LOCALAPPDATA%\VoxSub\models` —— 5 条真模型用例长期处于 skip 状态，真机路径实际上**没有被覆盖**。要不要统一这两处路径？
4. **CI Python 版本**：AGENTS.md 写 3.11，本机 venv 是 3.12.4。要不要把 CI 锁到 3.12（与开发环境一致）？
5. **文档落盘位置**：本轮新增了 `docs/` 目录。如果你更希望放在仓库根或别处，说一声我搬。

---

## 七、备份与回滚

**备份位置**：`.backups/phase1_20260921_042735/`（沿用仓库既有的 `.backups/` 约定）

内容：`ipc_server.py`、`tests/test_ipc_commands.py`、`frontend/src/renderer/store.ts`、`pytest.ini`

**回滚**（只回滚被改的既有文件；本轮新增的模块可以留着不影响）：

```bash
cd D:/OneDrive/app_dve/VoxSub
cp .backups/phase1_20260921_042735/ipc_server.py frontend/backend/ipc_server.py
cp .backups/phase1_20260921_042735/test_ipc_commands.py tests/test_ipc_commands.py
cp .backups/phase1_20260921_042735/store.ts frontend/src/renderer/store.ts
cp .backups/phase1_20260921_042735/pytest.ini pytest.ini
```

注意回滚后 `tests/test_migration_ledger.py` / `test_job_runner.py` / `test_ipc_loop.py`
会失败（它们测的是新契约），需要一并删掉或回退到对应 commit。

**回滚粒度**：每一块改动都单独可回退 ——
- 删除授权：回滚 `ipc_server.py` 的 `_cmd_cleanup_migrated_source` + `_cmd_start_migration` 两个函数；
- 后台任务：回滚 `ipc_server.py` 的 `main()` 即可，`job_runner.py` / `ipc_loop.py` 不被引用就等于不存在；
- 配置：回滚 `voxsub/config_store.py`；
- 门禁：回滚 `tests/test_architecture.py`。

---

## 八、剩余风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| 本轮改了 IPC 启动路径（`main()`） | 虽然默认语义不变，但"命令在 worker 线程执行"这件事改变了执行上下文。若某命令依赖"一定在读循环线程上"（目前没发现），会出问题。 | 有 25 条 `test_ipc_loop` 测试覆盖分派；建议在打包版上做一次完整冒烟后再发布 |
| `cleanup_migrated_source` 不向后兼容 | 老前端如果还传 `path`，清理会永远失败（安全但难用）。 | 已同步协议层；发布说明需要写明 |
| 台账文件可被本地篡改 | 威胁模型是"协议层攻击者无法指定路径"。有本机写权限的攻击者可以改台账 JSON —— 这已在文档里写明，不在本轮防御范围 | 护栏（受保护目录/卷根/正在使用的数据根）仍然拦住最危险的那些 |
| 复杂度棘轮基线会腐化 | 基线表可能被后来者当成"允许超标清单" | `test_complexity_baseline_only_shrinks` 强制条目只能删不能改大 |
| 打包版与源码不同步 | 当前 `dist/` 是旧构建，修复未进入打包产物 | 需要重新打包（见 §六.2） |
| **OCR 译后成图绕过统一缓存** | 路径是**前端拼的**：`frontend/src/renderer/views/ocr.ts::temporaryPath()` 造 `${cacheRoot}/ocr-${kind}-${stamp}.png` 当 `target` 传给命令，`handlers/ocr.py::_cmd_render_ocr_image` 照写不误 —— 没有 originals/translated 分离、没有有界淘汰、也不带配置指纹，即工作单 §3.4 警告的"另写一套不完整缓存"。**审查指出我原来这条写错了位置**（写成后端写 `ocr-translated-<stamp>.png`，全树 grep 为 0，代码里并不存在那个名字），已改正 | 改用 `OcrImageCache` 的 `allocate/finalize/cache_file` + 设置保存后 `invalidate_stale()`。属用户可见缓存行为变更（还会牵动渲染层的临时路径约定），需甲方确认后再动 |
| **OCR 逐行翻译未走带指纹的缓存** | OCR 翻译仍直调 `translator.translate(...)`，没用上"改配置即失效"的译文缓存 | 改走 `OcrTranslationService.translate_frame(...)`；同样属行为变更 |
| **Qwen 选不出运行时时的回退策略** | `runtime is None` 时只重置 `_runtime` 不动 `_server_exe`，可能拿一个已被排除的加速器 exe 当 CPU 继续跑。本轮只把日志改成实话（打印实际 exe 与已排除清单），**未改策略** | "保守拒绝启动" vs "尽力而为"是产品行为决策，需甲方定；定了之后是一处小改动 |

---

## 九、给下一个接手者的最短路径

1. 读 `docs/ARCHITECTURE.md` 摸清模块边界与依赖方向。
2. 读 `docs/HANDOVER.md` 知道"加一个新功能要碰哪些文件、测试怎么写、怎么发版本"。
3. 跑 `./.venv/Scripts/python.exe scripts/run_tests.py -q`，确认基线全绿。
4. 想继续做资源生命周期（缺陷 #6~#9），从 `docs/ARCHITECTURE.md` 的"资源所有权"一节开始 —— 那里的规则已经写好，缺的是实现。
