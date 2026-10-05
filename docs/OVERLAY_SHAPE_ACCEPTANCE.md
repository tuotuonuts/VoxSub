# 字幕悬浮窗毛玻璃方形灰边修复

记录时间：2026-10-05T18:50:52.091488+00:00；基础提交 `0e8012a`。以下均为本地源码验收，未推送或打包发布。

## 根因与证据

用户截图：内部卡片有圆角，外围整个860×140窗口却是方形灰底。此前main对整个HWND调用setBackgroundMaterial("acrylic")，但圆角和6px留白只定义在renderer CSS；没有原生区域裁剪。CSS的overflow/border-radius不能约束整窗原生背景。此前只测到材质调用成功，不能证明DWM的可见圆角。

本轮独立隐藏HWND的GDI读回：未设置shape时GetWindowRgn为0；接入真实OverlaySurface后为3（复杂区域），860×140/100%时包围盒为[6,6,854,134]，四角/外圈点排除、中心点包含。该证据证明原生绘制区域变化，不冒充可见桌面毛玻璃截图。

## 修复边界

- shared/overlay-shape统一6px留白/16px圆角，renderer使用同一常量；整数扫描行合并，只有圆角带需要逐行计算，开销不随窗口高度增长。
- main/overlay-surface独立管理每个窗口的区域/材质缓存；弱引用owner，新窗口不会继承旧窗口的active状态。
- 先成功裁剪再启用acrylic；关闭先移除材质再恢复矩形区域；部分失败保守保留裁剪，避免清理重试把仍有毛玻璃的窗口重新展开。
- 尺寸/缩放变化刷新；跨屏scale变化使缓存失效，原生API仍使用DIP，不重复乘物理DPI。裁剪不可用时报告unavailable并退回原有背景。
- 保留字幕窗setContentProtection(false)、showInactive、focusable:false，OCR覆盖窗仍捕获排除。不改用户配置、正文、模型、音频或运行中的应用实例。
- 未新增业务UI元素，不复制公共组件。shape API是Electron实验性原生能力，异常按可选毛玻璃安全降级处理。

## 验收结果

| 检查 | 结果与边界 |
|---|---|
| 新geometry/surface单测 | 34项通过：圆角/对称/非法值/100–500%缩放、owner、幂等、resize、scale、重入、材质部分失败与清理重试 |
| 生产main生命周期fixture | 51项通过，包括真实main接线的重建/旧owner回调/resize、截图策略；fake Electron，不是桌面交互 |
| 既有毛玻璃UI/IPC测试 | 14项通过：开关、滑条、输入校验、恢复竞态、opacity与文字独立 |
| 真实Electron隐藏HWND + GDI | 2个owner，14次读回；10个开启组合，860×140及640×100、100/125/150/200%renderer缩放；外圈和角排除，中心包含；禁用后恢复无region |
| 真实离屏Chromium | 同10组合，透明角alpha=0，中心alpha=235；实际生产HTML/CSS、无preload/backend；不包含DWM材质合成 |
| 静默 | show:false/focusable:false，show事件失败守卫；14次Win32读回均不可见/非前台；子进程windowsHide，独立userData/APPDATA/LOCALAPPDATA，mute-audio，无真实音频或桌面捕获 |
| 前端 | 最终npm run check与npm run build通过，git diff --check通过 |
| Python | 1216 passed / 8 skipped / 7 deselected / 1既有xfail，88.86秒；清除PYTHONPATH/PYTHONHOME，显式-m "not hardware_audio"，隔离数据路径/隐藏子进程 |

最终隐藏原生运行由进程exit与报告status双重判定为PASS。验收脚本曾在首个窗口销毁后被Electron默认window-all-closed退出打断；已在fixture禁用默认退出，重新完整跑完两个owner，不将此前不完整记录计作通过。

## NOT_RUN

用户当前实例/可见桌面DWM合成与圆角抗锯齿、系统或第三方截图录屏、真实拖动/边缘缩放/鼠标穿透、物理多显示器DPI切换、安装版/GitHub CI。原生isResizable标志为true，不等于边缘拖拽已验收；原生shape外不接鼠标，故未承诺外留白区的交互与过去完全一致。隐藏窗口native DPI为96；125/150/200%是renderer zoom，不冒充物理DPI。

## 证据、复验与回滚

证据目录：`D:\OneDrive\app_dve\overlay-shape-acceptance`，frontend-check-final.log、frontend-build-final.log、python.log、native-final.json/log。源码内test-overlay-surface-native.cjs与probe-overlay-region.py可重复执行；须先build，提供VOXSUB_TEST_PYTHON、隔离APPDATA/LOCALAPPDATA和可选VOXSUB_TEST_REPORT；用隐藏子进程运行Electron，清除NODE_OPTIONS/ELECTRON_RUN_AS_NODE与Python路径注入。不得拿生产启动入口替代该独立fixture。

修改前备份：`.backups/overlay-shape-20261006-023959`（本地UTC+08时间）。优先git revert本轮提交回滚；若恢复备份，逐个恢复其中既有文件，并单独撤销本轮新增文件。不要删除或提交既有Cache/。

本轮只创建本地commit，未push、未重启用户应用；新主进程/CSS需要用户自行启动新构建才会生效。
