# 悬浮窗毛玻璃验收

## 实现与使用

- 入口：**设置 → 外观 → 悬浮窗毛玻璃**。默认关闭，百分比默认 50%；支持 0%–100%，关闭后保留百分比，关掉时滑条禁用。
- 使用 Electron 的 `BrowserWindow.setBackgroundMaterial("acrylic")` 请求系统原生桌面背景材质；不是用 CSS blur 冒充跨窗口毛玻璃。要求 Windows 11 22H2 / build 22621 或以上。低版本明确提示并保留原背景；原生调用异常记录 warning 并回退背景。
- **百分比是材质可见程度，不是系统模糊半径**。系统控制 blur radius，应用只减少原字幕面板的深色遮罩：`opacity × (1 - strength / 100 × 0.7)`。0% 不使用材质；关闭保留原有背景不透明度。系统“透明效果”、节能及远程桌面等可能影响实际呈现。
- 仅改变背景，不降低字幕文字透明度、不模糊文字。窗口不重建，不调用 show/focus；保持无框、透明、置顶、点击穿透及原有显示后捕获排除流程。
- 拖动实时预览，松开自动保存；设置开关单独保存。`overlay_glass_enabled` / `overlay_glass_strength` 使用现有统一配置 schema，旧配置无字段时默认关闭，无需重写历史。
- 复用开关；新增公共 `buildPercentageSlider`，并收编原有背景不透明度滑条。组件只处理 DOM/百分比/禁用/input/change，不读业务配置或调用 IPC。
- 独立 revision 防止延迟启动配置覆盖用户预览；设置提示也防陈旧 IPC 返回覆盖新结果。连续调百分比只改 tint，不反复重设 DWM 材质。

## 已验证 PASS

1. `npm --prefix frontend run check`：类型、色板、完整生产逻辑/公共 UI/快捷键等门禁，以及新增毛玻璃 14 项通过。
2. `npm --prefix frontend run build`：最新源码构建成功。
3. 安全 Python 回归：**1074 passed / 4 skipped / 17 deselected / 1 xfailed / 1 warning**。新增 15 个毛玻璃配置隔离回归；warning 是已有 tarfile DeprecationWarning。
4. 本机 Windows 11 build 26300、真实最新 Electron + Python sidecar，两次独立进程启动：10 项验收通过，包括默认关闭/50%、拖动预览但未松开不保存、松开保存、0%关闭、100%材质、关闭保留百分比、进程重启恢复、浮窗渲染器重载恢复、点击穿透与材质共存。
5. 只对本轮进程拥有的隐藏 HWND 调用 **DwmGetWindowAttribute(DWMWA_SYSTEMBACKDROP_TYPE=38)** 只读复核：开启/100%为 **3 (DWMSBT_TRANSIENTWINDOW，Acrylic)**，0%/关闭为 **1 (DWMSBT_NONE)**。此项比“API 未报错”更强，但仍不等于桌面视觉验收。
6. 隔离 APPDATA/LOCALAPPDATA/TEMP；`--silent --debug --keep --no-build` 启动。两轮共 **141 次窗口采样，0 可见窗口/焦点抢占违规**。没有音频采集、播放或键盘注入，正常退出，0 本轮进程残留。不触碰安装版、模型、用户配置与历史。

原始证据（仓库外）：`D:\OneDrive\app_dve\glass-acceptance\checks-initial.json`、`checks-restart.json`、`silent-window-latest.json`、`silent-window-restart.json`、真实验收日志。全量门禁日志：父目录 `glass-final-check.log` / `glass-build.log` / `glass-pytest.log`。

## 未验证 NOT_RUN

- 浮窗叠在真实可见桌面上的视觉质量、透明边缘/系统动画/多显示器呈现：为了不抢占屏幕，全程保持隐藏；原生材质读回不是视觉证明。
- 毛玻璃切换后的可见窗捕获排除端到端、真实最小化/托盘视觉场景：原有流程及模拟生命周期门禁保留，未显示窗口进行实测。
- Windows 10 实机、系统关闭透明效果/远程桌面/节能场景：版本和失败回退已通过隔离行为测试，非本机实测。
- 安装包、安装版更新、音频、模型推理：本轮未运行/未修改。

## 备份与交付

源码与本地 dist 修改前备份：`.backups/overlay-glass-20261005-052354/`，包含 `RESTORE.md`。回退可将备份源码按同路径恢复、`dist/` 恢复到 `frontend/dist/`；新增文件以本轮 commit 的清单为准。也可对本轮提交执行 Git revert；不要恢复或修改用户配置/历史。

本轮创建本地 commit，**未推送 GitHub、未打包、未发布**。最终 commit 及 clean 状态记录于仓库外 `glass-acceptance/DELIVERY.json`。
