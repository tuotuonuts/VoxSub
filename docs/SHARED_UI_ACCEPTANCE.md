# 公共 UI 组件收编验收

## 范围与约定

用户要求已记录在项目 `AGENTS.md` 第 7 节与 `frontend/src/renderer/ui/README.md`：新增 UI 元素优先检查并复用公共组件；尚无对应组件时优先将通用结构和基础交互实现为公共组件。业务流程不强行抽象。

本轮盘点发现：设置页私有的输入框、下拉框、单选组、开关及卡片；设置/诊断相同导航；主导航、各页面和路径选择字段的标准按钮；模型广场/OCR 的筛选按钮。已收编：

- `ui/button.ts`：标准按钮 + 筛选按钮；原 class、type、disabled、hidden 及各页业务事件保持。
- `ui/card.ts`：标题/正文卡片与可增量填充 Frame，保留 section/div 语义和子节点引用。
- `ui/controls.ts`：文本/密码输入、下拉框、单选组、开关，设置页不再私有实现；诊断筛选、日志容量单位、工作区同时录音开关复用。
- `ui/tab-nav.ts`：设置/诊断导航合并，面板创建及 dispose 仍由页面负责。
- 现有 `buildField`、`buildPathPicker`、`buildProgressBar`、`buildStatusRow` 保留并列入组件索引；PathPicker 的按钮复用公共 Button。

公共组件不读 store、不调用 IPC、不隐式保存配置、不弹确认框、不启动任务；静态文案由调用方传入。工作区录音监听仍归 PageLifecycle，避免抽取后重复触发或释放失效。原 CSS 未修改，aria-selected/aria-pressed 与激活样式同步。

未机械抽取的部分：模型 cell、字幕流条目、OCR 预览/覆盖层、迁移步骤和判定块等业务结构，以及不同语义的数字/范围输入、动态模型/语言选项编排。并非所有 `h()` 都应包装成组件。

## 已验证

- 新增真实公共组件 MiniDOM 测试 16/16：原生按钮类型及样式、状态、文本安全、卡片结构/节点引用、输入提交事件、下拉初值、单选互斥及徽标、开关布尔回调和监听释放、分页独立状态、依赖边界。
- 增加页面禁止重新实现标准按钮/卡片/设置式导航的源码守卫；测试已纳入 `npm run check`。
- 最终 `npm run check` PASS：类型、palette、组件/页面生命周期、录音控制、模型广场竞态、诊断、主页状态文案等既有前端回归。
- 最终 `npm run build` PASS；本地 dist 更新。
- Python 安全回归：1059 passed / 4 skipped / 17 deselected / 1 xfailed / 1 warning（55.74s）；排除 integration/hardware_audio，既有 tar 弃用告警未改。
- 隐藏真实 Electron + Python IPC 的页面/DOM 验收 16/16：主页开关/按钮 CSS，设置 7 个分页及卡片，容量单位不保存的换算，诊断 3 个分页/硬件卡片，模型筛选，OCR 子页/预览按钮。
- 最终隐藏验收 138 次窗口采样，0 可见/焦点违规，0 Runtime.exceptionThrown；正常退出，本轮进程残留为空。

## 验收脚本修正（保留失败记录）

- 初次脚本把返回按钮全文当成“返回”，实际包含箭头；改为按专用选择器点击按钮，未改产品代码。
- OCR 预览默认选中“译后”，脚本误以为必定选中“原图”；核对实际 DOM 后改成先点击“原图”再断言选择状态，未改变产品默认行为。
- MiniDOM 未模拟浏览器 option 自动更新 select.value；公共下拉框对存在的初始选项显式设置 value，避免测试环境初值丢失，保持真实浏览器既有选择结果。

## 验收边界 / 未验证

真实隐藏验收使用隔离 APPDATA/LOCALAPPDATA/TEMP，仅检查 DOM、导航与无保存的单位换算；D 模式切换只写隔离测试配置。不开始录音、播放、模型推理、框选、下载或清理。真实运行不覆盖安装包、安装版、英文全部页面、迁移执行及实际音频（NOT_RUN）；这些不可冒称验收通过。

用户配置、历史、安装版和已安装模型未改。未打包、发布或推送。

## 证据与回滚

证据：`D:/OneDrive/app_dve/shared-ui-acceptance/`；最终提交/结果见其中 `DELIVERY.json`。前端 check/build 与 Python 日志为同目录收集副本，真实运行失败与最终记录均保留。

修改前源码与 dist 备份：`D:/OneDrive/app_dve/VoxSub/.backups/20261005-042858-shared-ui`。

回滚：工作区干净且不覆盖后续修改时，`git revert` 本轮提交后重新构建；需要立即回退本地构建时恢复备份中的 `frontend/dist`。不要 reset --hard，不恢复或覆盖用户配置/历史。
