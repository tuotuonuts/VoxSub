# VoxSub 可维护性基线

更新日期：2026-08-24

这份文档记录本轮代码审查后的结构边界和持续约束。目标不是机械追求小文件，
而是让每项可变状态只有一个负责人、外部 I/O 有明确失败语义，并让新功能通过
稳定接口接入。

## 验收结果

- 修改前基线：210 passed，8 skipped。
- 本轮最终验收：228 passed，8 skipped。
- `compileall` 全包编译通过，`git diff --check` 通过。
- 生产代码中没有估算分支复杂度达到 15 的函数。
- 生产代码中没有使用默认容量的无界 `queue.Queue()`。
- 核心模块没有反向导入 `voxsub.ui`。

以上后三项由 `tests/test_architecture.py` 持续守卫，后续变更违反边界会直接让
测试失败。

## 已消除的确认隐患

### 配置与持久化

- 唯一配置入口迁到 `voxsub.config_store`；`voxsub.ui.config_store` 仅为兼容导入。
- 配置 schema 统一处理类型、枚举、数值范围、有限浮点数和 BaseURL 结构。
- 引入 `config_version` 和集中迁移，旧云翻译字段可安全升级。
- 配置读改写用进程内重入锁保护，多个窗口不会因并发保存互相覆盖。
- 配置、模型清单、模型广场状态和字幕文本统一使用原子写入。
- 二进制模型文件先复制到同目录临时文件、刷盘，再原子替换；中断时保留旧文件。

### Pipeline 与后台任务

- `PipelineState` 明确表示 idle/starting/running/stopping/failed，异常退出不再只靠
  多个布尔量推断。
- 采集、识别、翻译、TTS、按应用音频回调队列全部有容量和明确过载策略。
- 文件音频解码/识别移到 `file_transcriber.py`；字幕格式化移到 `subtitles.py`；
  实时识别组件构建移到 `realtime_builder.py`。
- TTS 已接入独立 worker，慢播放不会阻塞识别/翻译；缺模型或播放失败会降级为
  仅字幕，而不是保留一个不生效的设置项。
- UI 与 Pipeline 的配置映射移到 Qt 无关的 `pipeline_configurator.py`。

### 下载、模型与硬件

- 下载器独立为 `downloader.py`，拆开来源选择、响应校验、断点续传、流式写入和
  完成提交；无尺寸/SHA 证据的旧 `.part` 不会被误当成完整文件。
- 模型相对路径统一拒绝绝对路径、`..` 和解析后越过模型根目录的路径。
- 多文件模型复用前同时校验尺寸和 SHA（若清单提供），提交单文件时使用原子替换。
- 硬件探测拆为系统资源、显卡、NPU、ORT provider 等独立探针；llama 后端选择拆为
  独显/NPU/核显/CPU 候选策略。
- llama-server 命令与 OpenVINO 环境构建移到 `translate/llama_launch.py`；进程启动、
  健康检查和真实 NPU 推理验证分开，失败后仍按已记录后端降级。

### UI 状态

- 会话计时与历史、字幕历史、识别调优草稿分别由 `ConversationSession`、
  `SubtitleHistory`、`RecognitionTuningDraft` 拥有。
- 会话导出移到 `conversation_export.py`；Qt 窗口只负责控件、信号和展示。
- `SettingsWindow`、`MainWindow`、`SubtitleOverlay` 仍是较长的视图壳，但其长方法
  主要是低分支的声明式布局，业务状态和外部 I/O 已移出。后续新增页面应增加独立
  pane/controller，不再向这些窗口加入新的业务生命周期。

## 新功能合入守则

- 核心层不得导入 UI；跨层通信使用回调、协议或不可变数据对象。
- 一个可变状态只能有一个明确拥有者；工作线程不得直接操作 Qt 控件。
- 所有长时间或外部 I/O 都必须可取消、有超时、记录失败原因，并定义降级路径。
- 队列必须显式声明容量和过载语义；禁止用无界队列掩盖慢消费者。
- 配置键只能加入统一 schema，并同步提供默认值、约束和迁移策略。
- 持久化正式文件不得直接覆盖；文本走 `write_text_atomically`，二进制复制走
  `copy_file_atomically`。
- 新增分支复杂度达到 15 的函数会被架构测试拒绝；应按探测、决策、I/O、提交等
  阶段拆分。
- UI 文件行数不是单独的失败条件，但不得在 QWidget 中重新引入业务状态、下载、
  模型进程或文件导出职责。
- 重构先补行为测试再迁移；兼容壳只转发实现，不复制逻辑。
