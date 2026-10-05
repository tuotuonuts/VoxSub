# 字幕悬浮窗无法被截图：修复与验收

记录时间：2026-10-05T18:04:16.350658+00:00；基础提交 `fc6ff55`。

## 根因与范围

字幕窗 `createOverlayWindow` 在 ready-to-show 后执行 `setContentProtection(true)`。Electron 官方文档说明 Windows 上这会请求 WDA_EXCLUDEFROMCAPTURE，让窗口从截图/屏幕捕获中被排除；旧系统可能截成黑块。这不是毛玻璃圆角或鼠标穿透造成的。
来源：https://www.electronjs.org/docs/latest/api/browser-window#winsetcontentprotectionenable-macos-windows

本次按“截图中需要包含字幕悬浮窗”修复，不新增悬浮窗里的截图按钮，也不放宽 OCR IPC owner 校验。
字幕窗明确设为 false；OCR 原位覆盖窗保留 true 防止识别自己的译文。只修改字幕窗捕获策略，未改毛玻璃、窗口布局、置顶、穿透、业务数据、模型或下载。字幕现在允许系统捕获，不再提供此前的防截图语义。

## 回归证据

- 新增3项生产main模块VM/fake Electron测试：初次显示允许截图且不focus、隐藏/显示及穿透切换不重新保护、重建与迟到ready隔离。
- 修复前47项通过、3项失败；修复后50项通过、0失败。
- 原OCR capture fixture5项继续通过，保护调用仍为true；原毛玻璃/其它前端门禁保留。
- 完整 `npm run check` 与 `npm run build`：PASS。
- 后端安全全量：1216 passed、8 skipped、7 deselected、1既有xfail，88.01秒、0失败；筛选 `-m "not hardware_audio"`，环境隔离，真实子进程CREATE_NO_WINDOW。
- `git diff --check`：PASS。

原始证据：`D:/OneDrive/app_dve/overlay-screenshot-acceptance`。测试未启动原生Electron/真实框选窗，未捕获用户桌面/字幕正文，未调用音频设备或播放声音；没有重启/控制用户运行中的应用。新测试使用假窗口，只证明生产代码的原生调用参数和生命周期，不冒充系统截图实测。

## NOT_RUN

Windows系统截图工具、QQ/微信截图、OBS/其它录屏实际画面、真实毛玻璃合成、不同Windows版本/显示器、安装版、GitHub CI均NOT_RUN。当前运行的旧进程需用户自行重启到新构建后才使用新策略；未自动打包/安装/推送。

## 回滚

原件备份：`.backups/overlay-screenshot-20261006-020141`，按对应文件恢复可回滚；不要覆盖后续用户改动。仓库原有未跟踪 `Cache/` 不清理、不提交。
