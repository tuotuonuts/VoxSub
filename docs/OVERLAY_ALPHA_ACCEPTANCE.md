# 悬浮窗背景透明度与原生毛玻璃：修复及验收边界

本轮为源码修复，不是正式版、安装包发布，也不是实际桌面视觉验收通过。

## 已复现的三个问题

1. `glassTint` 将背景不透明度乘以 `1 - strength / 100 * 0.7`。用户设置 92% 不透明度、100% 毛玻璃时，CSS 实际为 27.6%；两项设置相互干扰。
2. 在本机 Electron 44.3.0 的隐藏窗口中，`setBackgroundMaterial("none")` 把窗口背景从透明改为白色。默认渲染及软件渲染、普通隐藏及 offscreen 四种组合均复现。角像素 alpha 由 0/1 变为 255；CSS 的 html/body 仍是透明。因此“关闭原生材质即可安全回退”的旧假设不成立。
3. 初始裁剪/材质检查的广播也增加了设置 revision，使正在读取保存配置的 renderer 误以为用户已改设置，取消启动恢复。

用户截图中圆角卡片外的矩形灰底，与之前日志 `clippingCheck=pass/materialCheck=pass` 不矛盾：GDI region/DWM 属性读回不能证明桌面合成已经正确。此前的软件离屏 alpha 测试也不能代替默认渲染路径或桌面验收。

## 修复策略

- 背景不透明度只有一个 CSS 变量，由独立设置控制；原生毛玻璃事件不再修改它或重绘字幕。文字保持不透明。
- 每次设置、移除或失败恢复原生材质后，都显式恢复 `#00000000` 窗口底色。不是只改 html/body 的 CSS。
- 透明圆角卡片暂不启用已出现外溢的系统 acrylic 路径。保留用户的开关/百分比配置，明确显示“普通半透明背景”；不可用的百分比滑块禁用，不能以降低不透明度冒充模糊强度。
- 普通半透明背景仍按实际卡片区域裁剪；关闭毛玻璃后，缩放/尺寸/显示变化仍更新裁剪。旧 Windows 的材质属性不可用不伪报材质通过，也不无限重建窗口。
- 区分设置 revision 与检查状态广播；初始检查不取消保存设置恢复，用户新操作仍优先。
- 保留窗口缩放隔离、隐藏状态、点击穿透与字幕允许截图的原有行为；不全局关闭 GPU。

## 静默复验

- Python 悬浮窗/设备契约：45 passed。
- 纯逻辑/组件：不透明度、设置保存/恢复、17 项毛玻璃控制、35 项 surface、10 项 verification、52 项窗口生命周期；TypeScript 检查通过。
- 新增 `frontend/tools/run-overlay-alpha.mjs`：真实隐藏 Electron + 实际 overlay HTML/CSS；默认渲染、软件渲染分别 24 组，包括普通隐藏/offscreen、20/50/92/100% 不透明度、none/acrylic/none 切换。透明角与中心 alpha 断言通过，0 显示/聚焦违规。
- 改正后的透明角 alpha 为 0/1；中心 alpha 与设置一致，92% 时为 235，而非不透明 255。
- 原生 GDI/DWM 读回与 Chromium 像素测试是分开的证据。上述 acrylic 开关只在隔离测试窗中验证移除后的透明底色，不表示生产路径恢复了桌面毛玻璃。

证据目录：`D:/OneDrive/app_dve/review-hardening-7ee65e1/`。包含原始复现 `opacity-gpu.json`、`opacity-software.json`，显式恢复颜色的对照，以及 `alpha-native-gpu.json`、`alpha-native-software.json`。

## 尚未验证 / 后续门槛

- 可见桌面、实际视频背景、跨物理显示器 DPI、用户鼠标拖动、安装包：**NOT_RUN**。
- 实际桌面毛玻璃：**未恢复、未宣称通过**。当前是明确告知用户的兼容降级；恢复之前必须验证真实透明窗口合成，不能仅靠属性值启用。
- 未重启或关闭用户应用，不播放/录制音频，不改用户配置、模型、正文或历史。

回滚：本轮更改有对应 Git commit；源码修改前备份位于 `.backups/review-hardening-20261006-064325/source-before.zip`，透明度排查前的增量快照位于 `.backups/review-overlay-alpha-20261006-070258/`。不要用这些快照覆盖用户数据目录。

## 全项目审查收尾复验

最终源码重新完成默认渲染/软件渲染各24组透明度回归和生产隔离应用10组冒烟，测试应用正常退出。默认模式记录gpu_compositing=enabled；这仍不是可见桌面合成验收。最终窗口生命周期增加至57项；完整1390项Python及其skip/xfail边界见 `docs/REVIEW_HARDENING_ACCEPTANCE.md`，最终证据位于 `review-hardening-7ee65e1/final-20261006-074327/`。
