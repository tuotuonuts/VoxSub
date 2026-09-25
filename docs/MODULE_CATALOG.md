# 语幕 VoxSub —— 公共组件 / 服务目录

> 任务书要求"公共组件/服务目录"作为最终交付物。本文件回答一个问题：
> **想加/改一个功能时，该复用的是哪一个、该往哪里放、谁负责它的状态。**
>
> 目录里每一条都标了"唯一实现"和"防复活的门禁"。找不到对应条目时，先怀疑自己
> 是不是要造第二套 —— 任务书的成功标准里有一条是"公共规则只有一处权威实现"。

---

## 一、后端服务（Python，`voxsub/` 与 `frontend/backend/`）

### 1.1 IPC 适配层（`frontend/backend/`）

| 组件 | 文件 | 职责 | 状态归属 | 门禁/测试 |
|---|---|---|---|---|
| **协议 I/O** | `ipc_protocol.py` | stdout 的**唯一**写入点、事件发射、进程退出 | 无状态 | 必须在 `sys.stdout` 换成 stderr **之前**导入（否则捕获到 stderr） |
| **命令分派（读循环）** | `ipc_loop.py` | 读、协议防御、分派；控制命令**就地执行**；提交后台作业前先登记请求；异步 `start_migration` 使用 clientMigrationId 关联状态与安全报告事件 | 在途请求表 | `tests/test_ipc_loop.py`（覆盖队列立即完成竞态、迁移回执重关联、报告隔离）|
| **作业执行器** | `job_runner.py` | 任务状态机、有界队列、取消；取消与开始/终态提交在同一锁内仲裁 | **任务状态的唯一权威** | `tests/test_job_runner.py` |
| **契约校验** | `contract_validation.py` | 用 `contracts/` 的 JSON Schema 做边界校验（标准库子集） | 无状态 | `tests/test_contracts.py` |
| **迁移台账与删除授权** | `migration_ledger.py` | 台账持久化 + "能不能删"的全部规则 | **清理授权的唯一权威** | `tests/test_migration_ledger.py`（27 条）|
| **旧版探测与迁移规划** | `legacy_migration.py` | 探测旧安装、算迁移计划、校验搬迁结果 | 迁移 state 文件 | `tests/test_legacy_migration.py` |
| **入口与兼容 facade** | `ipc_server.py` | 路径装配、基础设施方法、`main()`、再导出搬走的名字 | `BackendService` 实例 | `tests/test_ipc_commands.py`、`tests/test_ipc_integration.py` |

**命令实现按业务域分在 `handlers/`**（拆分见 `docs/DECISIONS.md` 第 6 条）：

| 域 | 文件 | 命令数 |
|---|---|---|
| 会话与设备 | `handlers/session.py` | 23（启停/暂停/模式/语言/音频设备/TTS/导出）|
| 模型库 | `handlers/models.py` | 5（列表/安装/卸载/目录/导入）|
| 旧版迁移 | `handlers/migration.py` | 7（探测/决策/规划/快照/校验/执行/清理）|
| 屏幕 OCR | `handlers/ocr.py` | 5（识别/译后成图/缓存目录/文件复制）|
| 诊断 | `handlers/diagnostics.py` | 8（自检/导出/日志/更新日志/设备/硬件档案）|
| 后台任务 | `handlers/jobs.py` | 3（列表/状态/取消）|

**加一条新命令**：写进对应域的 handler（不要在 `ipc_server.py` 里加）→ 在
`contracts/commands.json` 登记 → 补一条测试。**长任务不用自己起线程**：只要命令名不
在 `job_runner.CONTROL_COMMANDS` 里，`ipc_loop` 会自动把它排进单 worker，并自动获得
`job_status`/`cancel_job` 支持。迁移这类可能超过请求硬时限的任务，前端传 `async: true` 与唯一 `clientMigrationId`，先订阅后提交；关联 ID 可在受理回执超时后重新关联真实终态。仅异步 `start_migration` 成功终态携带迁移报告，其他任务结果不广播；若终态先于回执到达，缓存结果优先于随后断连。

### 1.2 核心与基础设施（`voxsub/`）

| 组件 | 文件 | 职责 | 关键规则 |
|---|---|---|---|
| **会话编排** | `pipeline.py` | 模式（A/B/C/D）、线程与队列、启停与暂停 | 资源能否替换只看 `_may_replace_resources()`；停止超时保持"停止中" |
| **配置** | `config_store.py` | 配置 schema（**配置规则的唯一权威**）、原子写入、版本迁移 | 未来版本只读保护；未知字段保留；损坏先备份 |
| **原子写入** | `file_io.py` | 崩溃安全的文件发布 | **唯一实现**（含 Windows 瞬时占用有界重试）；别处不要再写 `*.json.tmp + replace` |
| **日志** | `logging_setup.py` | 文件 + 控制台 + 环形缓冲；日志桥到 UI | 日志事件必带 `ts`（由 `IpcLoop._event` 统一注入）|
| **ASR / 云 STT** | `asr.py` · `cloud_stt.py` | 语音识别 | — |
| **翻译** | `translate/`（`factory`/`opus`/`qwen`/`cloud`/`llama_launch`/`cache`/`prefetch`） | 档位路由与各实现 | Qwen 子进程生命周期见 `docs/DECISIONS.md`；close 幂等、只杀本实例的进程 |
| **TTS** | `tts.py` · `tts_worker.py` | 语音合成 | stop 返回是否真正退出；超时仍存活时保留旧 worker，禁止并行热替换；译文交付时可在旧 worker 结束后恢复 |
| **实时组件构建** | `realtime_builder.py` | 事务化组装 ASR/VAD/STT/segmenter | 构建中失败按反向创建顺序调用可用的 `close()`，再原样抛出 |
| **OCR** | `ocr.py` · `ocr_cache.py` | 屏幕 OCR、版面合并、缓存 | 缓存键必须含影响结果的配置维度 |
| **音频** | `audio.py` · `process_audio.py` · `recording.py` | 设备枚举、进程级 loopback、录制 | 相关测试必须挂 `hardware_audio` 标记 |
| **模型** | `model_catalog.py` · `model_storage.py` · `downloader.py` · `bootstrap_models.py` | 目录、存储布局、下载、首启动引导 | `migrate_models` 在 `model_storage`，**不在** `model_catalog` |
| **硬件与路由** | `hardware.py` · `router.py` · `npu_validation.py` · `llama_runtime.py` | 硬件探测、加速器路由、运行时选择 | 读控制台工具输出**必须**收字节按本机代码页解（`decode_console_output`）|
| **文件与诊断** | `subtitles.py` · `file_transcriber.py` · `diagnostics.py` · `error_reporting.py` | 字幕导出、文件转写、诊断包 | — |

---

## 二、前端公共模块（`frontend/src/shared/` 与 `renderer/ui/`）

工作单 §3.7 的六个组件里，`PageLifecycle` 与其余五个的落地情况：

| 组件 | 位置 | 职责 | 测试 |
|---|---|---|---|
| **PageLifecycle** | `shared/page-lifecycle.ts` | 订阅/监听/定时器/异步回调的统一清理；`dispose` 幂等 | `tools/test-page-lifecycle.mjs` |
| **会话时间轴** | `shared/session-timeline.ts` | 会话事件归约（`start` 才重置字幕；`stop` 保留字幕）| `tools/test-session-timeline.mjs` |
| **后端连接状态** | `shared/backend-status.ts` | 连接 phase、会话视图归零、`needsResync`、ready 握手解析；固定提示文案由调用方注入翻译器 | `tools/test-backend-status.mjs` |
| **请求结果语义** | `shared/request-outcome.ts` | 超时≠失败≠取消；任务阶段与终态判定；固定结果文案由调用方注入翻译器 | `tools/test-request-outcome.mjs` |
| **清理请求构造** | `shared/migration-cleanup.ts` | 只发 `record_ids`+`confirm`，绝不发 `path` | `tools/test-migration-cleanup.mjs` |
| **日志级别** | `shared/log-levels.ts` | 级别映射与过滤 | `tools/test-log-levels.mjs` |
| **危险操作确认** | `shared/confirm-action.ts` | 统一的内联二次确认 | `tools/test-confirm-action.mjs` |
| **设置项字段** | `renderer/ui/field.ts` | 标签/说明/输入/校验/禁用原因 | 见 `tools/test-ui-components.mjs` |
| **路径选择** | `renderer/ui/path-picker.ts` | 文件/目录选择与结果反馈 | 同上 |
| **进度展示** | `renderer/ui/progress.ts` | 进度/取消中/失败/完成 | `tools/test-progress-bar.mjs` |

**前端 IR 规则**（沿用现有约定，不要改）：

- 页面构建返回 `{ element, dispose }`，切换页面时统一 `dispose`。
- 监听用具名回调或 `AbortController`；异步回调返回后检查页面是否仍有效。
- 组件**不得导入具体业务页面**，只能通过参数与回调协作；不为一次性代码做抽象。
- 沿用现有 CSS 类与 `i18n.ts` 的 `tr()`，不重新设计 UI。
- 测试要断言**实际展示与交互结果**，不是内部变量。
- 向导的 `dispose` 只释放其创建时捕获的 lifecycle/host；migration 迟到请求还须校验最新请求身份和当前 lifecycle，不能改写/导航新向导；模型目录请求在回写共享 DOM 前核验页面与请求代次，迟到响应不能覆盖新页。
- `store.ts` 中系统状态/请求结果的固定文案通过 `setUiTranslator(tr)` 交由渲染入口注册；后端 reason、命令名等动态数据保持原文。

---

## 三、测试基础设施

| 组件 | 位置 | 用途 |
|---|---|---|
| **唯一测试入口** | `scripts/run_tests.py` | 固定 basetemp 到 `.pytest-run`，每次清空。**不要手工往仓库根传 `--basetemp`** —— 那正是 244 个 `.pytest-*` 的来源 |
| **前端纯逻辑脚手架** | `frontend/tools/esbuild-ts.mjs` | 把 `src/shared/*.ts` 编译成临时 mjs 再 import，**不启动 Electron** |
| **前端 DOM 测试替身** | `frontend/tools/mini-dom.mjs` | 组件测试用的最小 DOM，用于断言渲染结果 |
| **跨进程集成** | `tests/test_ipc_integration.py` | 真起 sidecar、真走 NDJSON；隔离配置；进程必回收 |
| **契约一致性** | `tests/test_contracts.py` | 命令/事件双向覆盖、TS 一致性、字段漂移棘轮 |
| **架构门禁** | `tests/test_architecture.py` | 依赖方向、复杂度棘轮、`subprocess` 编码、测试卫生 |
| **测试策略门禁** | `tests/test_pytest_policy.py` | 默认运行不得碰真实音频；显式 `-m` 必须排除 `hardware_audio` |

---

## 四、想加东西时先看这张表

| 你要做的事 | 该复用什么 | 不要做什么 |
|---|---|---|
| 加一条 IPC 命令 | 写进 `handlers/<域>.py`；长任务自动进 worker | 不要在 `ipc_server.py` 里加；不要自己起线程 |
| 加一个长任务 | 什么都不用做（自动获得 jobId/状态/取消）| 不要写自己的队列；不要写自己的状态枚举 |
| 要删一个目录 | `migration_ledger.validate_cleanup_target` | 不要按路径删；不要写新的黑名单 |
| 要写一个文件 | `voxsub/file_io.write_text_atomically` | 不要自己 `tmp + replace` |
| 要读控制台工具输出 | `voxsub/hardware.decode_console_output` | 不要用 `text=True` 不给 `encoding` |
| 要换翻译器/识别器/模型目录 | 先过 `pipeline._may_replace_resources()` | 不要只看 `self._running` |
| 运行中改语言/档位 | 任务提交时快照（见 `docs/DECISIONS.md`） | 不要让在途任务读变化后的字段 |
| 加一个前端交互 | 先在 `views/` 里数重复次数 | 只为一次出现的东西抽象 |
| 加一个设置项 | Python 端 schema 定默认值 | 不要在前端自己定默认值 |
| 加测试 | 后端 `tests/`；前端 `shared/` + `tools/test-*.mjs` | 不要让测试出声/弹窗/起 Electron |
