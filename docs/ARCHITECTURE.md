# 语幕 VoxSub —— 架构与模块职责图

> 目的：新接手的人（或 AI）读完这一份，就知道**每块代码归谁管、状态归谁所有、
> 什么东西可以互相引用、什么东西绝对不能**。
>
> 这份文档不是设计理想图，而是**当前代码的实际边界**：写在这里的规则都有
> 对应的自动化门禁（`tests/test_architecture.py`），违反会红。

---

## 一、整体分层与数据流

```
┌──────────────────────────────────────────────────────────┐
│ Electron renderer   frontend/src/renderer/               │
│   显示状态、收集输入、发送用户意图。                        │
│   可以拥有界面状态（表单草稿、展开状态）。                  │
│   ✗ 不拥有后台任务的最终状态   ✗ 不管理模型进程             │
└────────────────────────┬─────────────────────────────────┘
                         │ window.voxsub.*（白名单能力）
┌────────────────────────┴─────────────────────────────────┐
│ preload   frontend/src/main/preload.ts                   │
│   暴露受限能力。✗ 不暴露任意频道的通用调用入口              │
│   ✗ 不向页面泄露 Node / 文件系统能力                       │
└────────────────────────┬─────────────────────────────────┘
                         │ ipcRenderer / ipcMain
┌────────────────────────┴─────────────────────────────────┐
│ Electron main   frontend/src/main/                       │
│   窗口、原生对话框、屏幕采集、sidecar 连接、退出协调。       │
│   校验 IPC sender / 窗口角色 / 命令与参数。                 │
│   ✗ 不重复实现 Python 业务规则                             │
└────────────────────────┬─────────────────────────────────┘
                         │ NDJSON over stdio
┌────────────────────────┴─────────────────────────────────┐
│ IPC 适配层   frontend/backend/                            │
│   ipc_loop.py      读、协议校验、分派（控制命令就地执行）   │
│   job_runner.py    后台任务状态机、有界队列、取消           │
│   ipc_server.py    命令实现（_cmd_*），只做协议翻译         │
│   migration_ledger.py  迁移台账与删除授权（纯逻辑）         │
│   legacy_migration.py  新旧安装布局探测与迁移计划（纯逻辑）  │
│   contract_validation.py  契约（JSON Schema 子集）边界校验  │
└────────────────────────┬─────────────────────────────────┘
                         │ 普通函数调用
┌────────────────────────┴─────────────────────────────────┐
│ 应用服务 / 核心   voxsub/                                  │
│   pipeline.py 编排会话；asr/translate/tts/ocr 各管一段     │
│   ✗ 绝不 import 适配层，✗ 绝不 import UI 层                │
└────────────────────────┬─────────────────────────────────┘
                         │
┌────────────────────────┴─────────────────────────────────┐
│ 基础设施   voxsub/ 里的 model_*/hardware/downloader/       │
│   file_io/logging_setup/llama_runtime/ocr_cache ...       │
└──────────────────────────────────────────────────────────┘
```

**依赖方向永远是单向的：往右/往下可以，反向不行。**

由门禁强制：

| 规则 | 门禁 |
|---|---|
| `voxsub/` 不许 import `voxsub.ui` | `test_core_modules_do_not_import_ui_layer` |
| `voxsub/` 不许 import 适配层模块（`ipc_server`/`ipc_loop`/`job_runner`/`migration_ledger`/`legacy_migration`/`contract_validation`） | `test_core_package_never_depends_on_the_adapter_layer` |
| 适配层不许 import Qt（`PySide6`/`shiboken6`/`qfluentwidgets`） | `test_adapter_layer_does_not_pull_qt_back_in` |

---

## 二、模块职责表

### 2.1 IPC 适配层（`frontend/backend/`）

| 模块 | 职责 | 不该做的事 |
|---|---|---|
| `ipc_loop.py` | 读循环、协议防御、分派；控制命令就地执行；握手 | 不实现业务逻辑；不自己执行耗时任务 |
| `job_runner.py` | 任务状态机、有界队列、取消、退出保护判定 | 不知道 IPC 协议长什么样；不知道命令语义 |
| `ipc_server.py` | 每个 `_cmd_*` = 一条命令的协议翻译（参数 → 调用 → 结构化结果） | 不新增长流程编排（该抽出去的抽出去）；不改 `voxsub` 包 |
| `migration_ledger.py` | 迁移台账持久化 + 删除授权的全部判断规则 | 不删东西（只判断"能不能删"，删由调用方做） |
| `legacy_migration.py` | 探测旧安装布局、算迁移计划、校验搬迁结果 | 不删源目录；不决定 UI 怎么展示 |
| `contract_validation.py` | 用 `contracts/` 里的 JSON Schema 做边界校验 | 不引入重依赖；不做业务判断 |

### 2.2 核心（`voxsub/`）

| 模块 | 职责 |
|---|---|
| `pipeline.py` | 会话编排、模式（麦克风/系统声/文件/OCR）、任务状态发布 |
| `asr.py` · `cloud_stt.py` | 语音识别（本地 / 云） |
| `translate/` | 翻译路由（`factory`）、各实现（`opus`/`qwen`/`cloud`/`llama_launch`）、缓存、预取 |
| `tts.py` · `tts_worker.py` | 语音合成与工作线程 |
| `ocr.py` · `ocr_cache.py` | 屏幕 OCR 与缓存 |
| `audio.py` · `process_audio.py` · `recording.py` | 设备枚举、进程级 loopback、录制 |
| `model_catalog.py` · `model_storage.py` · `downloader.py` · `bootstrap_models.py` | 模型目录、存储布局、下载、首启动引导 |
| `hardware.py` · `router.py` · `npu_validation.py` · `llama_runtime.py` | 硬件探测、加速器路由、运行时选择 |
| `config_store.py` | 配置 schema（**配置规则的唯一权威来源**）、持久化、版本迁移 |
| `file_io.py` · `logging_setup.py` · `diagnostics.py` · `error_reporting.py` | 原子写入、日志、诊断、错误上报 |
| `subtitles.py` · `live_draft.py` · `contextual_text.py` · `text_cleaning.py` · `language_guard.py` | 字幕导出、实时草稿、上下文、文本清洗、语言约束 |
| `file_transcriber.py` · `realtime_builder.py` · `release_notes.py` · `models.py` | 文件转写、实时构建、更新日志、模型定义 |
| `runtime_bootstrap.py` | **当前是孤儿模块**（零引用）。要么接上，要么删 —— 别让它一直挂着 |

---

## 三、状态所有权（谁说了算）

| 状态 | 权威来源 | 谁可以写 | 界面能做什么 |
|---|---|---|---|
| 后台任务状态 | `job_runner.JobRunner` | 只有执行器的状态机 | 读 `job_status`/事件；**不能**因为"我等超时了"就当成失败 |
| 会话状态（运行/暂停/空闲） | `pipeline` | pipeline 内部 | 显示；发意图命令后以返回值为准、事件为最终 |
| 已保存配置 | `config_store.ConfigStore`（Python schema 权威） | 只有 `ConfigStore` | 发 `set_config`，回读确认 |
| 实际生效配置 | 任务提交时抓的**不可变快照** | pipeline 建立会话时 | 不能悄悄用新配置解释旧任务 |
| 字幕列表 | `pipeline` 产生，renderer 展示 | 只能追加/清空（**会话停止不清空**，导出要用） | 展示与导出 |
| 表格草稿 / 展开状态等纯界面状态 | renderer | renderer | 随便 |

**规则**：任何"界面显示的状态"都必须能追到上面某个权威来源。追不到的，说明要么该由后端有，要么它本来就只是界面草稿 —— 别混。

---

## 四、后台任务规则（工作单 §3.3 的落地版本）

```
queued ──► running ──► succeeded / failed
               └────► cancelling ──► cancelled
```

写代码时必须遵守：

1. **`cancelling` ≠ `cancelled`。** 收到取消请求只改状态为"取消中"，只有真正到达安全边界才落终态。原生推理不能立即打断的，就老老实实标"取消中"，等它返回后**丢弃结果**。
2. **请求超时不是失败，也不是取消。** 前端等不住了只是"这次没等到回话"，不能反过来改写后端状态。
3. **取消要能插队。** 它在读循环线程上处理，不排进作业队列。
4. **只有实际进入终态才解除退出保护**（`JobRunner.has_active_jobs()`）。
5. **队列有界。** 满了明确拒绝（`QueueFull`），不要无限堆积 —— 无界执行器会把积压藏起来。
6. **完成记录有界。** 只有**终态**记录可以被裁剪，活着的任务永远不能被裁掉。
7. **失败要带可识别错误码。** `error_code` 用异常类名或明确的业务码，别只丢一句人话。
8. **任务事件带 `jobId` 与 `sequence`。** 前端靠顺序号丢弃迟到/乱序事件。

**怎么加一条新的长任务命令**：什么都不用做 —— 只要它不是控制命令，`ipc_loop` 会自动把它排进 worker。如果你希望它支持协作式取消，在安全边界插一句 `_cancel_requested()`。

---

## 五、资源所有权（**规则已定，实现是下一轮的工作**）

当前状态：**规则写好了，代码还没全按它改**。缺陷 #6/#7/#8/#9 都属于这里。

要求：每个模型、客户端、线程、子进程都要能回答四个问题 —— **谁创建、谁允许使用、谁决定失效、谁负责关闭**。

必须遵守：

1. **工作线程还在用资源时，不许关闭或替换它。** 这是 Qwen 竞态与 OCR 借用翻译器的共同根因。
2. **`stop` 超时不能进入"安全空闲"状态。** 没停干净就诚实报"停止超时"，别显示 IDLE —— 否则下一次换模型会踩在还在跑的资源上。
3. **配置变更与资源重建走同一个门禁。** 现在各个 setter 各判各的 `self._running`，于是"提前替换"没防住。
4. **`close` 幂等。** 重复调用不引入新错误。
5. **退出用共享截止时间**，不要每个环节各叠一个长超时。
6. **只清理本实例拥有的进程**，绝不按进程名宽泛地杀。
7. **OCR 独立运行时必须独立回收**；如果借用会话翻译器，要有明确的借用协议，不能持有裸引用。
8. **复用现有的 OCR 缓存失效机制**，不要另写一套不完整的缓存。

---

## 六、通信契约

- 契约定义在仓库根的 `contracts/`（JSON Schema），**单一来源**。
  新增命令/事件的流程见 `contracts/README.md`。
- `ipc_server.handle()` 动态分派 `_cmd_<command>`；`BackendService.has_command()`
  是"这个命令存在吗"的唯一判断入口（读循环用它做未知命令的前置拒绝）。
- 握手事件 `ready` 必须带：`version`、`protocolVersion`、`backendGeneration`、
  `readiness{ready, activeJobs}`、`session`（当前会话与活动任务快照）。
  这几项是为了让**渲染层重载后能重新同步**。
- 协议解析的防御清单（逐条有测试）：无效 JSON、`null`、非对象、缺 `id`、缺
  `command`、未知命令、`args` 类型错、超大消息、子进程异常退出。

---

## 七、配置兼容规则

- **Python 端 schema 是配置规则的唯一权威来源。** 前端不自己定义默认值。
- 三种"配置"要分清：**用户保存的**、**当前实际生效的**、**某个在途任务持有的快照**。
- 长任务提交时抓**不可变快照**；影响结果的配置变更递增 generation 并进入缓存有效性判断。
- 运行中改语言/模型/端点，必须明确：对哪些新任务生效？旧任务是继续用原快照还是请求取消？旧结果还能不能展示？**不能悄悄用新配置解释旧任务。**
- 配置写入用受校验的 patch + 原子写入。
- 版本迁移：**只迁移已知旧版本**；遇到未来版本**禁止静默降级和破坏性保存**，进入只读保护；未知字段保留在原始持久化数据里，不作为已生效字段执行。
- **配置指纹不得记录或输出密钥原文。**

---

## 八、前端页面生命周期

- 页面构建函数返回 `{ element, dispose }`，切换页面时统一 `dispose`。
- 订阅、监听器、定时器、异步回调用具名回调或 `AbortController` 统一清理；`dispose` 必须幂等。
- 异步回调里要检查页面是否仍然有效。
- 共享的是**展示与交互**，不是业务逻辑；一次性代码不做抽象。
- 组件测试要验证"用户实际看得到、点得到的结果"，不是内部变量。

> 现状：渲染层此前有 12 处 window/document 监听、**0 处移除**（缺陷 #10）。
> 前端专项代理正在按上面的方式接入。
