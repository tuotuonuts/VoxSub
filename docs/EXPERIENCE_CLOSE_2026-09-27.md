# 体验修复阶段性收尾 — 2026-09-27

## 历史记录说明（2026-09-29 更新）

本文件保留9月27日收尾时的事实，不是当前未完成清单。之后已有 `68c3b4d4c9d8a5e86d87c540a3a87bb28156c554` 接入滚动/录音及 `ce74a02988c3e11cd82c33cbcdd1d119027207cd` 文档；9月27日批次后来获得授权推送。录音接线等后续验收缺口由本轮返修处理，详见 [ACCEPTANCE_REPAIR_2026-09-29.md](ACCEPTANCE_REPAIR_2026-09-29.md)。新报告与仓库外交付清单提供当前 HEAD、测试和回滚范围。

下文“未push/草稿撤出/不要续跑”只描述当时，不能用它否定后续已执行授权；native Electron/正式安装版本仍没有本轮新验收证据。

## 当时结论

用户在执行中要求“先收尾吧”，因此冻结范围：只交付已完成的窗口生命周期保护、日志时间统一；**不宣称四项体验问题全部完成**。停止追加调查、真实 Electron 排障和独立审计。滚动、录音两个未完成分支已保存草稿并从运行代码/默认测试发现中撤出，既有功能没有被半接入代码替换。

基线：`7e9e5cbcc895ae8740cbd6532091a09471f3c098`，main，开始时工作区干净。两项代码提交：
- `1d61547b1c264108520993e8a769b191d781226c` — Main 窗口销毁后的安全通知与回调归属。
- `ac957dc9cc22d27240641cdf442fe61a4b63fe3b` — 日志绝对时间、稳定接收顺序、本地显示/导出与默认测试入口。

仅本地提交；未 push、未构建正式安装包、未替换安装目录、未操作 Release。源码修改不会自动更新用户正在运行的版本。

## 已交付与证据

### 窗口销毁保护

实际调用链：真实 `BackendBridge.handleLine` → `emit` → Main 通知。旧主窗/浮窗被销毁后，引用未失效，optional chaining 只检查 null，仍会调用已销毁对象。新测试的 fail-closed Electron 替身确定复现 `TypeError: Object has been destroyed`；这证明当前源码具备该失败路径，但不能凭用户复制的堆栈证明其当时必然是同一个窗口对象或同一构建。

集中检查 BrowserWindow/webContents、退出状态；closed 回调按对象身份清引用；旧 ready 回调不作用于新窗口。仅对检查后发生的 native destruction race 做有诊断的局部处理，不吞不相关编程异常。9 个窗口生命周期检查通过；既有迁移接管 21 项与退出保护继续通过。

旧 dist 文件仅定位/读取，未重建覆盖：`frontend/dist/main/main.js` 的备份 SHA256 为 `a9d541adc2b0eed92cdaf41a07e5a1d6c2f37936e1dde903406119a8f8207c8b`。源/产物与用户报错时版本的逐字一致性未证实。

### 日志时间

旧链路混合无时区秒级 Python 时间、renderer UTC 时间和截断日期的展示。现在生产端记录真实创建时刻/任务事件时刻，带时区及毫秒；保留 raw、来源、接收时间/序号，不为使日志看起来有序而改写或排序事件。显示本地日期、毫秒和当时 UTC 偏移；旧无日期/时区记录明确标为无法确定绝对时刻。实时/文件视图及导出共用时间规则，并保留原始证据；新增静态文案有中英映射。

测试覆盖 UTC/偏移同一时刻、午夜、毫秒、迟到/同刻/多来源、非法与历史记录、四个显式 TZ（含 DST）、实际诊断页模块的显示和导出。这里的页面验证是 MiniDOM 行为测试，**不是浏览器布局或 Electron 成品验收**。日志时间列 CSS 已适配长时间文本，真实视觉验收仍 NOT_RUN。

## 合并门禁（撤出未完分支后执行）

环境：项目 `.venv/Scripts/python.exe`，Python 3.12.4；Node 26.7.0。每次运行独立隔离 APPDATA、LOCALAPPDATA、TEMP、TMP、TMPDIR 与 pytest basetemp，清除 PYTHONPATH/PYTHONHOME；npm offline。

- 安全 Python：**922 passed, 5 skipped, 17 deselected, 1 xfailed, 1 warning in 37.96s**；命令 `pytest -q -rs -m 'not integration and not hardware_audio'`（必须通过下述隔离 runner）。
- `npm run check && npm run test:acceptance-contracts`：exit 0；包含新增窗口/日志测试、迁移接管 21 checks、25/25 结构契约及 11 个负例。
- `git diff --check`：通过。
- 本次前置 A 复验已通过：旧安全套件 917 passed、前端 check/acceptance；旧最终 HEAD 回退证据已核查。旧 sidecar 7 passed 不计入本次 922，也不冒充本次重跑。
- **本次独立审查 NOT_RUN**：按最新收尾要求没有再开一轮审计；上一轮独立审查不为本次代码背书。

原始证据根：`E:/Hermes_data/cache/scratch/voxsub-experience-20260927-013832/`。
最终日志：`close-backend-c6e8d386/result.log`、`close-frontend-04896751/result.log`。
持久交付目录：`C:/Users/Zhang Ruiduo/Hermes/Tool_cache/voxsub-experience-20260927/`，含 evidence.zip 与隔离 runner。

可复跑命令（Git Bash，仓库根）：
```bash
env -u PYTHONPATH -u PYTHONHOME .venv/Scripts/python.exe 'C:/Users/Zhang Ruiduo/Hermes/Tool_cache/voxsub-experience-20260927/run_checks.py' recheck-backend pytest -q -rs -m 'not integration and not hardware_audio'
env -u PYTHONPATH -u PYTHONHOME .venv/Scripts/python.exe 'C:/Users/Zhang Ruiduo/Hermes/Tool_cache/voxsub-experience-20260927/run_checks.py' recheck-frontend 'npm run check && npm run test:acceptance-contracts'
```
runner 内绝对路径是本机核实值，换设备必须先核实；不要回落用户真实目录。

## 未完成分支 / 不得算通过

1. **滚动**：已真实 headless 浏览器复现撑高（clientHeight=scrollHeight=13316、scrollTop=0）；后续验证被工具拒绝，未绕过。CSS 草稿未完成 GREEN；自动跟随/用户上滚不拉回仍待实现。因此草稿已撤出，不称问题已修好。
2. **录音开关**：现有文档与旧 UI 指向“麦克风模式的可选 WAV 保存、完全停止时设置”，不是系统声音采集开关。已调查并编写确认状态 controller/测试及 Pipeline 接入提案；**未完成工作区/状态协议/Pipeline 整合**。handler 半接入修改已恢复，新增未完成测试已移出默认发现。未来需先复核 `recording/INTEGRATION.md`，不能仅复制 handler。
3. **真实 Electron 生命周期**：尝试的进程 exit 0 没产生验收完成文件，不能算通过；加入证据检查后明确失败（ENOENT）。按收尾要求停止诊断，脚本暂存，不纳入默认门禁。此项为验证未完成，不称“没有启动过 Electron”。
4. 真实系统音/麦克风、模型/硬件、迁移、安装包/frozen sidecar、本次完整产品 GUI 均 NOT_RUN。

## 备份、回滚与续接

修改前完整文件快照：`.backups/experience-20260927-013832/files.zip`、`manifest.json`、`RESTORE.md`；310 个文件条目已逐字节验证。覆盖当时 Git 跟踪文件及备份所枚举 dist 文件，不包含用户配置/模型/安装目录，不是这些数据的备份。

未完成草稿：同目录 `deferred/`，8 个文件及 SHA256 manifest；包含滚动 CSS/测试、录音 handler/controller/测试、无有效证据的 native smoke。注意其中 app.css 是滚动草稿的旧快照，**不可整文件覆盖现在的 app.css**，否则会丢失日志列改动。精确录音方案及 RED/GREEN 提案证据随持久 evidence.zip 保存。

代码级恢复可逆序 revert 本次文档提交、`ac957dc`、`1d61547` 回到基线；必须先保全将来的未提交改动，不能直接 reset。完整最终 HEAD 的 scratch clone 回退验证结果及精确最终 SHA 写在仓库外 `rollback-evidence.json`；不回滚工作仓库。备份手动恢复须按 manifest 指定文件恢复，并按新增文件清单移除仅本次创建项，禁止覆盖后续用户编辑。

后续只有用户重新授权继续时再接滚动/录音、真实产品验证；不自动续跑。
