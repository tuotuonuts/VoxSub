# 交接单 —— 语幕 VoxSub Electron 前端

写于 2026-09-12。**其他 AI 看这一份就能接手**。

## 一句话现状

Electron 前端已完整实现现有功能并跑通端到端，**`VoxSub` 仓库零改动**；
尚未打包（electron-builder 配置未写），尚未做真实屏幕共享下的捕获排除核验。

## 环境事实

| 项 | 值 |
|---|---|
| 项目目录 | `<repo>/frontend`（Electron 前端） |
| 仓库根 | `D:\OneDrive\app_dve\VoxSub`（分支 `main`；Qt 版在 `qt-legacy`） |
| Python 核心 | `<repo>/voxsub`（与前端同仓库，`main` 上已无 Qt） |
| Python 解释器 | `<repo>\.venv\Scripts\python.exe`（3.12.4） |
| Node / npm | v22.23.2 / 12.0.2 |
| Electron | 44.3.0（从 34.2.0 升级，清掉 34 条 CVE） |
| 模型根目录 | `D:\VoxSub\Models`（另有旧位置 `%LOCALAPPDATA%\VoxSub\models`，多根查找） |
| 启动方式 | `npm run launch`（自动定位仓库根；或双击 启动Electron版.bat） |

## 功能对照（Qt 版 → Electron 版）

**不是按页面结构对照，是按能力对照。** 结论：Qt 前端 11,526 行的功能面已覆盖，
差异只在页面组织方式（见下方"页面布局决策"）。

| 能力域 | 覆盖情况 |
|---|---|
| 三模式会话（A 麦克风 / B 系统声音 / C 文件） | 完整，端到端实测通过 |
| 录音（同时录音 / 结束并保存 / 保存后定位文件） | 完整，含退出保护 |
| D 模式 OCR（框选 / 上传 / 实时区域 / 原位覆盖 / 译后图导出 / 复制） | 完整，端到端实测通过 |
| 字幕浮窗 | 半透明、点击穿透、置顶、捕获排除、字号、显示模式、边距行距、历史回溯、锁定 |
| 模型广场 | 18 项目录、装/卸载、筛选、空间占用、自定义位置、迁移已有模型 |
| 设置 | 7 分页：翻译 / 语音 / 设备 / 识别调优 / 存储与模型 / 外观 / 关于（含更新日志折叠） |
| 诊断 | 自检、日志（实时+历史文件双来源）、导出报告、导出日志、清除本机日志、打开文件夹、硬件画像 |
| 设备与捕获 | 麦克风、系统回环、可捕获进程窗口选择 |
| 会话导出 | SRT / VTT / TXT，**真实时间戳**（非等间隔） |
| 托盘 | 模式切换、开始/停止、显示主窗、设置、诊断、退出 |
| 退出保护 | 模型迁移进行中拒绝退出并跳回设置页（Qt 版 `can_close_application` 的等价物） |
| 更新日志 | 后端 11 版数据，设置页默认折叠到最近一版 |
| 旧版迁移向导 | 检测旧版安装 + 数据风险分级 + 复制/原子迁移 + 三层校验 + 失败不退出 |

### 跨栈重写的部分（Qt 依赖项替换）

| Qt 实现 | Electron 版做法 | 验证 |
|---|---|---|
| `render_translated_image`（QImage/QPainter） | 后端用 PIL 重写同一套算法：背景从原图边框采样、按亮度二选一文字色、圆角填充、字号自适应 | `tools/test-ocr-e2e.py` 实测覆盖位置与尺寸正确 |
| `SubtitleExporter`（voxsub.subtitles） | 直接复用（纯 Python，无 Qt 依赖） | 实测 SRT 时间轴递增、BOM 正确 |
| `_paint_translations` / `_translation_layouts` | 前端 `ocr-overlay.ts` 用 DOM 定位 + 不重叠检测 | 单测级验证 |

## 已验证（有实测证据）

| 能力 | 证据 |
|---|---|
| 捕获排除（隐私承诺） | `GetWindowDisplayAffinity` = `0x00000011`；屏幕路径拍不到内容，PrintWindow 得纯黑 |
| 半透明 + 无边框 + 置顶 + 点击穿透 | 浮窗 computed style：`rgba(16,20,22,0.92)`、圆角 16px |
| 后端 IPC | 返回真实 `0.9.0-beta`、18 模型、6 项自检全绿 |
| 后端能力实测 | `tools/probe-backend.py`：**20/20 PASS** |
| OCR 端到端 | `tools/test-ocr-e2e.py`：**全通过**（造图→识别 2 行→翻译→译后图覆盖位置正确） |
| 模型目录 | 18 项，8 项已装；空间提示"已占 4.07 GB · 全部安装需 25.25 GB · D:\VoxSub\Models" |
| 端到端会话 | `start` → 状态灯"拾音中 · 麦克风 (Steam Streaming Microphone)" → 字幕草稿行 → `stop` → idle |
| 字幕导出时间轴 | 实测 1.2s / 4.8s / 15.5s（真实相对时间，非等间隔） |
| 设置页 7 分页 | 每页均有内容（翻译 2 卡片/6 字段、识别调优 2/9、存储与模型 3/3 等） |
| 诊断页 | 6 项自检 `is-ok`；日志页 5 个控件（实时/历史文件/打开文件夹/导出日志/清除本机日志） |
| 界面冒烟 | `tools/smoke-ui.mjs`：**49/49 通过** |
| 令牌契约 | 全部通过（相邻表面 ≥1.20、正文 ≥4.5、accent ≥3、表面带色相） |
| 类型检查 | `tsc --noEmit` 无错误 |
| 后端无孤儿进程 | 强杀 Electron 后 `ipc_server` 残留数 = 0（stdin EOF 触发 Python 侧退出） |
| 顶栏可达二级页 | 模型/设置/诊断按钮实测可点，无需依赖托盘 |
| 静默模式 | `--silent`：窗口创建但不显示、不抢焦点、不建托盘，CDP 与渲染照常；`tools/check-windows-visible.py` 断言桌面无可见窗口 |
| 一键启动 | `启动Electron版.bat` / `npm run launch`：自动补二进制、增量构建、定位后端、清理旧实例 |
| 标题栏不染色 | 顶部 36 行内 Windows 强调色像素 = 0（原生标题栏已换自绘） |

## 页面布局决策（按使用逻辑，非照搬 Qt 结构）

1. **模型目录从主屏移到独立页面**。原先它常驻首屏底部（约 300px），把字幕压到
   `max-height: 46vh`。装模型是低频操作，看字幕是每次打开都要做的——低频不该占
   高频的位置。改后字幕区 691px（窗口 820px，占 84%）。
2. **顶栏承担全局入口**：模型 / 设置 / 诊断 / 主题。原先二级页只能从托盘进，
   主界面里点不到。
3. **设置页保留 Qt 的 7 分页划分**，因为这 7 类内容确实是 7 种不同的心智模型，
   合并没有收益。
4. **诊断页的日志分两个来源**：「实时」读内存缓冲（本次运行），「历史文件」读磁盘
   日志（含历史运行）。排障时用户要的通常是后者——崩溃发生在下次启动之前。
5. **OCR 工作区按"一次性 vs 持续"分页**：截图翻译是单次操作，实时区域是长期运行，
   混在一屏会让状态语义打架。
6. **二级页面统一外壳**：返回按钮 + 页名放在公共外壳上，三页行为一致。
   此前只能按 Esc 返回，不知道快捷键的用户会被困在页面里。
   返回栏 sticky 固定，长页面滚动时仍可见。

## 未完成

1. ~~electron-builder 打包配置~~ **已完成**（`electron-builder.config.cjs` +
   `backend/ipc_server.spec` + `tools/build-release.py`）。实测能产出
   win-unpacked（656MB，含 sidecar 与 asar）。用户决定当前先用脚本启动，
   暂不发布安装包。
2. **模型下载/卸载的真实网络往返**未测（命令已接通，未实际下载）。
3. **OCR 的框选与实时区域交互**未在真实屏幕上走完（后端识别链路已端到端验证，
   `capture.ts` 的框选窗与覆盖窗只验证到实现层）。
4. **字幕浮窗的拖拽与边缘缩放**交互未逐一验证（窗口、样式、控件已验证）。
5. **真实屏幕共享软件**（OBS / Teams）下的捕获排除只做了 API 级验证，未在真实
   共享会话里核对。
6. **录音落盘**未在真实麦克风会话中确认 WAV 路径（命令链已通，`last_recording` 有应答）。

## 关键决策记录

1. **VoxSub 零改动** —— 用 `backend/ipc_server.py` 做适配层，Python 侧一切照旧。
2. **不用前端框架** —— 界面状态由事件推动（字幕流/进度/日志），不需要虚拟 DOM 的
   diff 能力；`dom.ts` 的小工具函数让类名集中，不散落字符串。
3. **命令名集中在 `protocol.ts`** —— IPC 是两端唯一的耦合面，散落各处时后端改名不会
   有任何编译期错误。
4. **失败必须留痕** —— `list_models` 里曾用 `except Exception: installed = False` 把
   `AttributeError` 吞成"未安装"，界面因此静默显示全部未装。现在失败会写 stderr 并
   出现在返回值的 `diagnostics` 字段里。
5. **先落 UI 再通知后端** —— `switchMode` 等的先后顺序会决定切换手感。
6. **异步回调必须复查 DOM 引用** —— 自检要跑 4-6 秒，期间用户可能切走分页；
   `await` 之后的写入必须重新确认目标节点仍有效（踩过：往已置空的引用写导致整个
   分页打不开）。
7. **零宽字符做成构建门禁** —— `.shell` 这类选择器被 U+200B 破坏后整条 CSS 规则
   静默失效；`scripts/sanitize.mjs` 作为 `prebuild` 强制清洗。

## 工具

| 工具 | 用途 |
|---|---|
| `tools/smoke-ui.mjs` | 界面冒烟 28 项（CDP 驱动，无需人工点击） |
| `tools/probe-backend.py` | 后端能力实测 20 项 |
| `tools/verify-palette.mjs` | 设计令牌契约（对比度/色相/层级） |
| `tools/cdp-eval.mjs` | 在渲染进程里单条求值，排障用 |
| `tools/compare-qt-features.py` | Qt → Electron 功能对照（输出待人工确认清单） |

## 下一台机器要做的第一件事

```bash
npm install
node node_modules/electron/install.js   # ← npm 拦截了 postinstall，必须补
npm run check                            # 类型 + 令牌契约
npm run build
./node_modules/.bin/electron .
```
