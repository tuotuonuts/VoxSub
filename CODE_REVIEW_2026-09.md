# VoxSub 代码再审查与后续演进基线

> 审查日期：2026-09-05
> 审查范围：`voxsub/`、`tests/`、CI 质量门禁与现有维护文档
> 目的：在继续增加功能、发布版本、交接维护人员之前，明确当前结构风险、禁止继续恶化的规则，以及可分阶段落地的重构路线。

## 1. 审查结论

项目已经具备一批重要的防腐层：核心层不反向依赖 UI、生产队列明确容量、函数复杂度有自动门禁、配置和模型存储已有集中入口，且本地包可以通过 `compileall` 编译检查；静态导入图没有发现循环依赖。

但当前仍存在明显的“高耦合热点”。它们暂时没有破坏功能，却会让下一轮功能变更变成跨文件、跨线程、跨 UI 状态的连锁修改。风险最高的不是单个算法，而是**编排层、UI 窗口、目录数据和基础设施集中在少数超大模块中**。

建议把当前阶段定义为：**先冻结边界，再逐步拆分；不进行一次性大重构，不在新功能中继续扩大热点文件。**

## 2. 现状证据

### 已确认的优点

- `voxsub` 核心模块没有导入 `voxsub.ui`。
- AST 导入图未发现循环依赖。
- 生产代码没有默认容量的 `queue.Queue()`。
- 现有架构测试对核心 → UI 依赖、函数复杂度和队列容量有持续约束。
- `python -m compileall -q voxsub` 通过。
- 现有维护文档记录了配置、Pipeline、下载、模型、OCR、退出时序等关键历史决策，交接基础比一般早期项目完整。

### 当前规模热点

| 模块 | 约行数 | 主要风险 |
|---|---:|---|
| `voxsub/ui/settings_window.py` | 1866 | 页面构建、状态同步、配置读写、模型选择、存储迁移和关闭保护集中在一个窗口对象 |
| `voxsub/ui/main_window.py` | 1602 | 主窗口同时承担页面壳层、运行控制、Pipeline 事件、导出和浮窗协调 |
| `voxsub/pipeline.py` | 1492 | 实时音频、文件转写、翻译、TTS、线程生命周期和退出策略集中编排 |
| `voxsub/model_catalog.py` | 1245 | 大量模型元数据与目录查询、推荐、安装状态、校验逻辑共存 |
| `voxsub/ui/diagnostics_window.py` | 1038 | 诊断 UI、任务启动、报告发送、日志展现和状态格式化混合 |
| `voxsub/translate/qwen.py` | 1024 | llama-server 生命周期、运行时选择、HTTP 调用、批处理和错误降级混合 |
| `voxsub/ocr.py` | 971 | OCR 引擎适配、模型参数、行分组、缓存协作、翻译和渲染前数据整理混合 |
| `voxsub/ui/ocr_workspace.py` | 880 | 页面编排、截图、任务调度、覆盖层状态和导出协作混合 |

当前 `ui.app.main()` 约 203 行、复杂度评分接近现有门禁上限；`pipeline.py` 对多个核心子系统有较高扇出。它们是后续最容易继续膨胀的入口。

## 3. 需要优先处理的问题

### P1：UI 窗口对象正在成为状态中心

`SettingsWindow` 和 `MainWindow` 不只是显示控件，还保存配置、运行状态、页面切换、服务调用和多个窗口之间的联动。现有代码中的直接访问，例如 `win._toggle_run()`、`settings_win.tabs.setCurrentIndex(...)`、以及通过 `lambda` 读取另一窗口状态，说明 UI 对象之间已经存在隐式协议。

**后果：** 新增设置或页面时容易产生双向信号、重复保存、关闭时序问题和“改一个页面导致托盘/浮窗/Pipeline 回归”。

**目标：** UI 窗口只负责展示和发出用户意图；运行状态、配置变更和页面导航通过明确的 controller/service/command 接口传递。

### P1：Pipeline 是功能编排“总线”而不是单一用例服务

`pipeline.py` 同时了解 ASR、音频采集、云 STT、文件转写、翻译工厂、上下文、实时草稿、TTS、录音、路由和退出逻辑。即使已经抽出了若干模块，Pipeline 仍然是后续改动的最大冲突点。

**目标拆分：**

- `PipelineLifecycle`：状态机、启动/停止/关闭、截止时间。
- `RealtimeSession`：采集 → VAD/ASR → 草稿/翻译事件。
- `FileTranscriptionUseCase`：文件解码、分段、识别、字幕输出。
- `TranslationCoordinator`：翻译请求、过期结果、降级策略。
- `TtsCoordinator`：TTS 队列与 worker 生命周期。
- `Pipeline` 只保留用例组合和对外稳定接口。

拆分时保持现有公共入口和测试先行，不要一次性移动所有逻辑。

### P1：模型目录数据和目录服务耦合

`model_catalog.py` 既是大数据文件又包含模型筛选、推荐、安装状态和运行时适配判断。模型数量继续增长后，任何目录变更都会触碰行为代码并提高合并冲突概率。

**目标：** 将不可变目录数据移到独立的 `model_catalog_data.py`（第一阶段）或版本化 JSON/清单（第二阶段）；`ModelCatalog` 服务只负责校验、查询和能力过滤。UI 不应直接依赖全局 `CATALOG`，而应使用查询接口返回不可变 DTO。

### P1：翻译运行时生命周期过于集中

`translate/qwen.py` 同时负责运行时下载校验、后端选择、端口分配、子进程、输出尾部、HTTP、并发批处理和错误转换。该文件任何变化都可能影响模型启动、翻译准确性和退出回收。

**目标拆分：** `runtime_session`、`llama_client`、`translation_batcher`、`translation_errors` 四个职责；Qwen translator 只组合它们。

### P2：应用入口有导入副作用和大量闭包接线

`ui/app.py` 在模块导入阶段初始化日志和错误报告；`main()` 中通过多个闭包完成窗口、托盘、Pipeline 和退出流程的接线。这会增加测试隔离成本，也容易出现只在启动顺序或打包环境中复现的问题。

**目标：** 引入 `ApplicationBootstrap` 或 `AppRuntime`，将以下步骤变成显式阶段：

1. 基础设施初始化；
2. 单实例与配置加载；
3. 服务容器/运行时创建；
4. 页面创建；
5. 信号接线；
6. 退出策略注册。

模块导入应尽量只定义类型、函数和类，不执行进程级初始化。

### P2：配置和模型存储是高扇入基础设施热点

`config_store.py`、`model_storage.py`、`logging_setup.py` 被大量模块直接引用。它们目前是集中入口，这是正确方向，但也意味着任何接口变化会产生大面积影响。

**目标：** 稳定公共接口，新增 `ConfigSnapshot`/`ConfigPatch` 或等价 DTO；禁止业务模块拼接配置键名；模型路径操作统一返回明确的结果/错误类型，不把路径和状态散落在 UI 中。

### P2：测试门禁仍偏向静态形状检查

现有架构测试能够防止部分坏趋势，但复杂度评分是近似值，无法发现资源泄漏、信号重复连接、关闭顺序、配置写入竞态、跨页面隐式协议等问题。

**应补充的契约测试：**

- Pipeline 启动/停止/关闭幂等性；
- 所有 worker 退出后才释放 translator/cloud client；
- 过期翻译结果不会覆盖新 revision；
- 配置 patch 不丢失并发窗口的无关字段；
- 页面关闭时不产生重复信号或重复线程；
- 模型目录每个条目的必需字段、路径安全和来源校验；
- 运行时子进程失败时错误类型和清理结果稳定。

## 4. 建议采用的目标边界

不建议现在立即搬迁大量目录。先用现有包结构形成逻辑边界：

```text
voxsub/
  domain/          # 纯数据、协议、错误类型、无 Qt、无文件/网络副作用
  application/     # 用例编排、Pipeline facade、配置 patch、任务生命周期
  infrastructure/ # 文件、网络、模型、运行时、硬件、日志、Sentry
  ui/              # Qt 页面、view model、controller、信号接线
```

可在现有路径上逐步落地；新目录不是第一步。第一步应是把跨边界的接口写清楚，并阻止新代码继续直接依赖热点内部实现。

## 5. 分阶段执行计划

### 阶段 0：冻结恶化（立即）

- 不再向 `settings_window.py`、`main_window.py`、`pipeline.py`、`model_catalog.py`、`qwen.py` 增加大段业务逻辑。
- 新功能先放入独立 service/use-case 文件，再由热点模块调用。
- 禁止 UI 调用其他窗口的私有成员；增加公开命令方法或领域信号。
- 保持核心模块无 Qt 依赖、所有队列有容量、所有后台资源有 owner。
- 将本审查作为 PR checklist，而不是只靠开发者记忆。

### 阶段 1：先拆启动和 UI 编排

- 新建 `ui/app_runtime.py`，把 `ui/app.py` 缩减为命令行入口。
- 新建 `ui/navigation_controller.py`，统一页面显示、激活和关闭阻塞。
- 新建 `ui/tray_controller.py`，去掉 `app.py` 中的托盘闭包。
- 将设置页面按 tab 提取为无状态 view + controller；保留 `SettingsWindow` 作为兼容壳层。

### 阶段 2：拆 Pipeline

- 先为现有 Pipeline 的启动、停止、关闭和事件协议补契约测试。
- 抽取 `PipelineLifecycle`，确保所有线程共用同一个 deadline。
- 抽取实时、文件、翻译、TTS 协调器；Pipeline 只组装它们。
- 保持 `PipelineClient` 和 UI 使用的公开方法不变，避免大面积改 UI。

### 阶段 3：拆模型目录和翻译运行时

- 目录数据与查询逻辑分离，模型 ID 作为唯一跨层标识。
- 将 Qwen/llama-server 子进程协议和批处理从 translator 中移出。
- 为每个运行时 adapter 定义统一的 `start/health/translate/close` 协议。

### 阶段 4：工程化质量门禁

建议逐步加入并固定在 CI：

- Ruff：格式、导入顺序、明显错误；
- Pyright 或 mypy：从公共接口和 DTO 开始逐步启用；
- coverage：先只要求核心编排/资源释放路径达到门槛；
- import-linter 或自定义架构测试：固定层级依赖；
- 复杂度和文件大小预算：新代码不得超过预算，现有热点使用临时豁免并逐步偿还。

## 6. 后续硬规则（建议写入贡献规范）

1. 新功能不得直接扩大上述八个热点文件；确需修改时必须说明为何不能新建 service/use-case。
2. UI 不读取其他窗口的控件、私有字段或 tab 索引；跨页面动作通过 controller/command。
3. 核心服务不接受 Qt 对象；用数据类、回调协议或信号适配器隔离。
4. 任何线程、进程、队列、HTTP client、模型 engine 都必须有唯一 owner 和可测试的 `close()`/退出语义。
5. 配置键名和模型路径不得在 UI 中散落拼接；统一经 schema/adapter。
6. 外部错误必须在边界转换为项目错误类型，禁止把 `Exception` 直接泄漏到 UI。
7. 新增全局变量、模块导入副作用、私有成员跨对象调用，必须在 PR 中解释并添加回归测试。
8. 代码重构必须小步提交：先加契约测试，再移动实现，最后删除旧路径。

## 7. 本次审查未做的事情

当前工作区的 `.venv` 仍指向另一台机器上的 Python 路径，且当前系统 Python 3.12 未安装 pytest，因此本轮不能在本机重跑完整测试套件；仅完成了源码 AST 审查、依赖图审查和 `compileall` 检查。恢复本机 Python 3.11 + `requirements.lock` 后，应先执行完整门禁，再开始阶段 1 重构。

工作区原有的以下未跟踪文件未修改：

- `README-LAPTOP-FERQV95J.md`
- `tests/test_error_reporting-LAPTOP-FERQV95J.py`
- `tests/test_logging_ui-LAPTOP-FERQV95J.py`
- `tests/test_ui-LAPTOP-FERQV95J.py`

## 8. 近期最值得做的第一项改动

优先实施“阶段 0 + 阶段 1 的第一小步”：新增 `ApplicationRuntime`，只迁移启动阶段、窗口实例持有和退出注册，不改变业务行为；同时补一组启动/退出契约测试。这样能立即降低 `ui/app.py` 的编排压力，并为后续拆 `SettingsWindow`、`MainWindow` 和 Pipeline 建立稳定落点，风险远低于一次性重写。

## 9. 阶段 1 第一小步实施记录（2026-09-05）

已完成应用入口的第一步收敛：

- 新增 `voxsub/ui/app_runtime.py`，由 `ApplicationRuntime` 持有 QApplication 生命周期和顶层窗口组合流程。
- `voxsub/ui/app.py` 从约 308 行缩减为 27 行，只负责日志初始化、入口函数和调用 runtime。
- 将错误报告运行上下文初始化从模块导入阶段移动到 `ApplicationRuntime.run()`，降低导入副作用。
- 保留原有启动、单实例锁、托盘接线、页面创建、退出信号和 Pipeline 关闭顺序。
- 暂不拆分窗口控制器和 Pipeline，避免在没有完整 pytest 环境时进行过大的行为改动。

该项已在同一阶段完成：页面导航和托盘接线已提取为独立 controller，并补充了导航契约测试；随后又将应用退出顺序提取为独立 coordinator。

在同一小步中进一步完成了 `voxsub/ui/navigation_controller.py`：托盘菜单、页面显示/激活、主窗口导航信号和应用退出阻塞策略已从 runtime 中独立出来。`ApplicationRuntime.run()` 现在只负责启动顺序、组件创建、退出资源注册和事件循环；后续可以继续把窗口创建和退出资源注册拆成独立阶段，而不再继续扩大入口函数。

同时新增 `tests/test_navigation_controller.py`，覆盖：关闭被活动操作阻塞时的页面引导、主窗口导航信号、安装器关闭事件和单次退出调用。该测试不依赖 PySide6，通过 fake signal/window 验证控制器协议。

## 10. 阶段 1 第二小步实施记录（2026-09-05）

已完成应用退出编排的第二步收敛：

- 新增 `voxsub/ui/shutdown_coordinator.py`，由 `ApplicationShutdownCoordinator` 统一注册 `aboutToQuit` 退出回调。
- 保持原有退出顺序：模型广场 → OCR workspace → Settings 页面离开 → Pipeline → 错误报告 → 退出日志。
- Pipeline 关闭继续采用 best-effort 语义，单个资源关闭失败不会阻断后续退出步骤。
- `ApplicationRuntime` 不再直接持有退出闭包和资源释放细节，只负责组装 coordinator 并注册它。
- 新增 `tests/test_shutdown_coordinator.py`，覆盖退出顺序、重复注册幂等性及 Pipeline 关闭失败后的继续清理。

这一步没有改变业务 API，也没有迁移 Pipeline、窗口或模型目录内部逻辑；后续可在同一退出协议上继续增加启动/退出契约测试。
