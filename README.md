# 语幕 VoxSub — Electron 前端

用 Electron + TypeScript 重写的 VoxSub 前端。**`D:\OneDrive\app_dve\VoxSub` 仓库零改动**，后端
（Python，26,000 行）原样复用，通过 stdin/stdout 上的 JSON Lines 协议驱动。

## 为什么重写

原 Qt 前端的问题不在架构，在**成品组件生态**：QSS 没有变量、嵌套、过渡，手绘控件
与样式表混用导致设计令牌传播不到（实测同一份令牌有 4 处硬编码副本）。改令牌后
三层表面仍挤在 1.1:1 的对比度里，界面退化成一块近黑。

## 架构

```
┌─ Electron 主进程 (src/main) ─────────────────────────────┐
│  main.ts     窗口 / 托盘 / IPC 注册 / 对话框 / 实时 OCR 循环 │
│  backend.ts  Python 子进程桥（spawn + NDJSON 协议）        │
│  capture.ts  屏幕捕获、框选窗、OCR 覆盖窗                  │
│  preload.ts  contextBridge（唯一特权入口）                 │
└──────────────────────┬──────────────────────────────────┘
                       │ contextBridge → window.voxsub
┌──────────────────────┴──────────────────────────────────┐
│  渲染进程 (src/renderer)                                  │
│  index.ts     主窗装配（展牌 / 模式索引 / 工作区 / 目录）    │
│  store.ts     单一状态源 + 后端就绪门 + 命令封装            │
│  protocol.ts  命令名与事件类型（单一事实来源）              │
│  palette.ts   设计令牌（浅/深两档）                        │
│  views/       workspace · catalog · settings · diagnostics · ocr │
└──────────────────────┬──────────────────────────────────┘
                       │ NDJSON over stdio
┌──────────────────────┴──────────────────────────────────┐
│  backend/ipc_server.py — 把 voxsub 的能力翻译成协议命令     │
│  （import voxsub，不改 voxsub）                            │
└─────────────────────────────────────────────────────────┘
```

## 起步

```bash
npm install

# ⚠️ 必做：本机 npm 默认拦截包的 postinstall 脚本，
#    导致 electron / esbuild 的二进制不会被下载。装完必须补一步：
node node_modules/electron/install.js
node node_modules/esbuild/install.js

npm run build
npm start          # 或 ./node_modules/.bin/electron .
```

后端定位：默认找 `<仓库同级>/VoxSub`，可用环境变量覆盖：

```bash
VOXSUB_ROOT="D:/OneDrive/app_dve/VoxSub" ./node_modules/.bin/electron .
```

## 校验（交付前必跑）

```bash
npm run check           # 类型检查 + 设计令牌契约
npm run build           # 构建（prebuild 会先清零宽字符）
npm run verify:palette  # 令牌契约：对比度 / 色相 / 层级分离
npm run probe:backend   # 后端能力实测 20 项
npm run test:ocr        # OCR 端到端：造图→识别→翻译→译后图校验
npm run verify          # 上面三项一起跑
npm run smoke:ui        # 界面冒烟 28 项（需应用带 --remote-debugging-port=9222 运行）
npm run verify:features # Qt → Electron 功能对照（输出待确认清单）
```

冒烟与后端实测都需要应用在跑；启动示例：

```bash
VOXSUB_ROOT="D:/OneDrive/app_dve/VoxSub" ./node_modules/.bin/electron . --remote-debugging-port=9222
```

调试用工具：

```bash
# 启动时加 --remote-debugging-port=9222，然后：
node tools/cdp-eval.mjs "document.querySelectorAll('.cell').length"          # 主窗
node tools/cdp-eval.mjs "document.body.innerText" "字幕浮窗"                  # 指定窗口
node tools/verify-palette.mjs                                                # 令牌契约
```

## 必须知道的坑（都踩过）

### 1. Electron 的 `setContentProtection` 有时序要求

必须在窗口**已经显示之后**调用。在 `ready-to-show` 阶段调用会**返回成功但静默失效**
（`GetWindowDisplayAffinity` 读回 `WDA_NONE`，窗口变成没有任何保护的暴露面）。

```ts
win.once("ready-to-show", () => {
  win.showInactive();
  win.setContentProtection(true); // ← 必须在 show 之后
});
```

验证方法（不能只看返回值）：

```bash
# 读回真实 affinity，期望 0x00000011 (WDA_EXCLUDEFROMCAPTURE)
python -c "..."   # 见 tools 说明；或直接看 PrintWindow 抓到的是否为纯黑
```

### 2. 零宽字符会静默破坏代码

编辑过程中偶发的 U+200B 会让 `.shell` 选择器失效、`./palette` 无法解析，而报错信息
里两种字符肉眼无法分辨。已固化为构建门禁：`npm run build` 前自动跑
`scripts/sanitize.mjs`，发现即清除并报错退出。

### 3. 后端命令是异步就绪的

Python 侧要 spawn 解释器并 import onnxruntime 等，实测需要数秒。界面在同一帧就取模型
列表会必然落空（表现为"目录空白"，看不出原因）。`store.ts` 的 `call()` 内置了
**后端就绪门**，会自动等待 `ready` 事件（超时 30s），调用点不必关心时序。

### 4. `ModelMarketplace` 的方法收 `ModelSpec` 而非 id

`is_installed(model)` / `uninstall(model)` / `model_dir(model)` 都要传 spec 对象；
传字符串 id 会抛 `AttributeError`。用 `voxsub.model_catalog.get_model(id)` 转换。
构造时 `models_root` 传 `None` 才会启用**多根目录查找**（同时看到新旧存储位置），
传显式路径会把查找收窄成一个目录。

### 5. 原生标题栏与默认菜单

不处理的话，标题栏会被 Windows 强调色染色（实测 `#0078D4`），下方还会出现
Electron 默认菜单栏（File/Edit/View…）。两者都已处理：`titleBarStyle: "hidden"`
+ `titleBarOverlay`（配色对齐 `--ground`）、`Menu.setApplicationMenu(null)`。
自绘标题栏后需自行提供拖拽区（见 `app.css` 的 `body::before`）。

## 目录

```
src/main/         主进程（窗口 / 桥 / 捕获 / 预加载）
src/renderer/     渲染进程（装配 / 状态 / 令牌 / 视图 / 样式）
backend/          ipc_server.py —— 唯一与 voxsub 包接触的 Python 代码
scripts/          构建辅助（零宽字符清洗、静态资源复制）
tools/            验证与调试（令牌契约、CDP 求值）
```

## 设计语言

同人展目录（impeccable 方向）：单色墨印在纸面上，靠 1px 细线与留白分区；
密铺"邮票格"承载模型目录；选中态是"笔圈"而非实心填充；窄窗时整格重排。

令牌契约（`tools/verify-palette.mjs`）把"看不见的层级"变成可执行检查：
相邻表面 ≥1.20:1、正文 ≥4.5:1、accent 作控件 ≥3:1、表面必须带色相。
