# 交接单 —— 语幕 VoxSub Electron 前端

写于 2026-09-12。**其他 AI 看这一份就能接手**。

## 一句话现状

Electron 前端已完整实现现有功能并跑通端到端，**`VoxSub` 仓库零改动**；
尚未打包（electron-builder 配置未写），尚未做真实屏幕共享下的捕获排除核验。

## 环境事实

| 项 | 值 |
|---|---|
| 项目目录 | `D:\OneDrive\app_dve\voxsub-electron`（独立 git 仓库） |
| 后端源码 | `D:\OneDrive\app_dve\VoxSub`（branch `main`，**零改动**，`git status` 干净） |
| Python 解释器 | `D:\OneDrive\app_dve\VoxSub\.venv\Scripts\python.exe`（3.12.4） |
| Node / npm | v22.23.2 / 12.0.2 |
| Electron | 44.3.0（从 34.2.0 升级，清掉 34 条 CVE） |
| 模型根目录 | `D:\VoxSub\Models`（另有旧位置 `%LOCALAPPDATA%\VoxSub\models`，多根查找） |
| 启动方式 | `VOXSUB_ROOT="D:/OneDrive/app_dve/VoxSub" ./node_modules/.bin/electron .` |

## 已验证（有实测证据）

| 能力 | 证据 |
|---|---|
| 捕获排除（隐私承诺） | `GetWindowDisplayAffinity` = `0x00000011`；屏幕路径拍不到内容，PrintWindow 得纯黑 |
| 半透明 + 无边框 + 置顶 + 点击穿透 | 浮窗 computed style：`rgba(16,20,22,0.92)`、圆角 16px |
| 后端 IPC | 返回真实 `0.9.0-beta`、18 模型、6 项自检全绿 |
| 模型目录 | 18 项，8 项已装（含真实体积 953.8/474.1/1820.1 MB） |
| 端到端会话 | `start` → 状态灯"拾音中 · 麦克风 (Steam Streaming Microphone)" → 字幕草稿行 → `stop` → idle |
| 设置页 6 分页 | 翻译 3 块/6 字段、语音 2/2、设备 2/3、识别调优 3/9、外观 2/2、关于 3/5 |
| 诊断页 | 6 项自检 `is-ok`，摘要"全部 6 项正常" |
| 令牌契约 | 全部通过（相邻表面 ≥1.20、正文 ≥4.5、accent ≥3、表面带色相） |
| 类型检查 | `tsc --noEmit` 无错误 |
| 后端无孤儿进程 | 强杀 Electron 后 `ipc_server` 残留数 = 0（stdin EOF 触发 Python 侧退出） |
| 顶栏可达二级页 | 设置/诊断按钮实测可点，无需依赖托盘 |
| 标题栏不染色 | 顶部 36 行内 Windows 强调色像素 = 0（原生标题栏已换自绘） |

## 未完成

1. **electron-builder 打包配置**（`package.json` 里没有 build 段）。生产环境需要把
   `backend/` 与 Python 运行时作为 extraResources 打进去——`backend.ts` 的
   `resolvePython()` 已经预留了 `process.resourcesPath/backend/ipc_server.py` 这条路径。
2. **模型下载/卸载的真实网络往返**未测（命令已接通，未实际下载）。
3. **OCR 框选与实时区域**的实际交互未走完（`capture.ts` 的 `pickScreenArea` /
   `createOverlayForArea` 已实现，只验证了单测级调用）。
4. **字幕浮窗的拖拽与锁定**交互未逐一验证（窗口与样式已验证）。
5. **真实屏幕共享软件**（OBS / Teams）下的捕获排除只做了 API 级验证，未在真实
   共享会话里核对。

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

## 下一台机器要做的第一件事

```bash
npm install
node node_modules/electron/install.js   # ← npm 拦截了 postinstall，必须补
npm run check                            # 类型 + 令牌契约
npm run build
VOXSUB_ROOT="<VoxSub 路径>" ./node_modules/.bin/electron .
```
