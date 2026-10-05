# 二级页面顶部遮挡 / 返回栏高度修复

记录时间：2026-10-05T18:21:37.686555+00:00；基础提交 `aa031c4`。

## 根因

设置、诊断、模型页共用page-layer。原返回栏与正文同处滚动区，返回栏sticky且有标题栏避让偏移；滚动/焦点滚动时返回栏会覆盖导航、首张卡片或模型页说明。openPage替换DOM但未主动复位滚动位置。用原router/CSS在同一真实Chromium测试中复现，修复前图像可见设置页顶部导航被遮住。

## 修复

- 新公共组件 `frontend/src/renderer/ui/page-frame.ts`：公共Button返回控件＋标题栏，与独立page__content滚动区分离；router仍负责翻译标题、返回、焦点与dispose，不把业务状态移入公共组件。
- frame填满可用高度，正文flex/min-height:0/overflow:auto，header不参与正文滚动，卡片/导航不再跑到返回栏背后。
- 返回栏默认字号下实测约36.95px；减少顶部重复留白，仍保留窗口标题按钮/拖拽安全区。窄窗保留箭头及aria-label。
- 新页主动重置旧layer滚动；focus返回按钮使用preventScroll，避免焦点动作改变正文位置。
- settings nav仅在自身正文滚动区吸顶，不再额外叠加标题栏/返回栏偏移。
- 迁移向导直接挂载时保留原layer间距和滚动策略；紧凑间距仅适用于公共page frame。未改模型/识别/翻译配置、用户日志与正文、下载/录音逻辑。

## PASS

1. 新公共组件4项miniDOM行为测试，纳入npm check：header/body分离、原content节点保留、公共Button回调、窄窗可访问标签/安全文字、实例隔离。
2. 真实Electron离屏Chromium：中文/英文、深色/浅色、14组窗口/缩放（包括1114x511、1226x251、320x240、360x640、超宽3440及125/150/200%），4种主工作区＋3种二级页、全部设置/诊断tab与路由滚动复位。**440项布局测量，0失败**。测试前同断言440项有172项未满足新布局要求（包括旧版无独立scroll body），不是声称172个独立用户bug。
3. 最终离屏安全采样 **963次，0可见/焦点违规，0捕获的renderer错误**。只对隔离离屏测试窗口capturePage，不抓用户桌面、不控制用户应用。已人工检查设置/诊断/模型离屏PNG，顶端内容和按钮完整。
4. 默认1240x820设置页：headerTop=34、headerBottom=70.953、bodyTop=70.953、首内容top=80.938（CSS像素），返回栏与正文不相交；滚动/切页检查通过。
5. 完整npm run check、npm run build：PASS；Python安全全量1216 passed / 8 skipped / 7 deselected / 1既有xfail，87.59秒、0失败；git diff --check：PASS。

## 静默与证据边界

Electron测试只加载生产renderer/CSS，使用假preload/IPC和合成模型列表，show:false、offscreen:true、独立userData、关闭硬件加速；不加载生产main/后端，不使用真实模型/麦克风/系统声音。Python清除PYTHONPATH/PYTHONHOME，APPDATA/LOCALAPPDATA隔离，`-m "not hardware_audio"`，真实子进程隐藏。最终全程未替用户重启应用。

NOT_RUN：用户运行中实例/其实际配置、物理显示器DPI切换、原生窗口标题按钮实际交互、键盘/屏幕阅读器人工验收、迁移向导实际可见操作、安装版/GitHubCI。离屏图与数据不能冒充用户桌面或成品验收。

原始日志、截图、layout-results.json：`D:/OneDrive/app_dve/page-header-acceptance`（before/after）。仓库机器摘要：`PAGE_HEADER_ACCEPTANCE_EVIDENCE.json`。

## 回滚 / 交付

备份 `.backups/page-header-20261006-020935`，原文件按路径恢复；新增组件/测试可通过本轮commit回滚。恢复时不要覆盖后续用户改动。保留已有未跟踪Cache目录，不清理不提交。本轮只本地commit，未自动push/打包/安装；运行中旧进程需重新启动新构建才加载布局修复。
