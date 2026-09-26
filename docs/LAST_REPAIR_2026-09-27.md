# 最后一轮限定返修：A/B 生命周期与 C 交接闭环

本轮基线：`1a3d47769c0c2ef914648b2794cb1bf3d0d976a8`，main，开始时工作区干净。范围仅为迁移接管保护、失效 stop 收尾与安全验收/回退交接。未推送、不打包、不改变兼容告警默认值。

## 执行环境与范围

- Python：项目 `.venv/Scripts/python.exe`，3.12.4；Node v26.7.0；npm 12.0.2。
- 工作目录：`D:/OneDrive/app_dve/VoxSub`；前端命令在其 frontend 子目录。
- Hermes scratch 实际路径：`E:/Hermes_data/cache/scratch`。启动时 TMPDIR 指向系统临时目录，未信任该变量；每次显式建立 UUID 目录并核对解析结果，隔离 APPDATA、LOCALAPPDATA、TEMP、TMP（同时重定向 TMPDIR）。
- 未修改真实用户配置、凭据、模型、迁移源、安装目录或 Release。测试 fixture 写入/删除仅限独占 scratch。
- 真实 sidecar L3 已单独授权：仅测试创建的子进程，独立隔离配置，允许只读诊断/窗口与能力枚举；不是实际 GUI/音频/模型流程。

## A：迁移接管后的退出保护

- 原失败：组合探针已显示迁移成功，但 `real quit allows exit after renderer takeover completion` 断言失败（exit 1）；固定时钟下另一次 RED 证明旧 request owner 在重建 renderer 后碰撞。
- 修复：复用 main 的 busyOwners，记录后台实例、client request 与 jobId；确认受理/重复任务接管时同步关联并转移保护。已发送迁移不再由 renderer release 解除；仅匹配的后台终态/状态查询，或确定未发送/明确拒绝，才能释放。未知关联保持保护。请求 ID 使用 UUID。BackendBridge 对每次 spawn 建立独立实例身份并忽略旧 stdout/exit。
- 组合测试：真实 main/preload/BackendBridge/migration/store，仅替换 Electron、sidecar 流与 mini-DOM；保留 main 与 sidecar fixture，重建 renderer VM。覆盖实际 quit/native close、全程保护、其他 owner、重复/迟到事件、页面重开、断连、后台重启同名 job、先终态后回执及 job_status 恢复。
- 命令：frontend 下 `node tools/test-migration-takeover.mjs`；先 RED，再 **21 takeover checks passed**（纳入 npm run check）。证据 `E:/Hermes_data/cache/scratch/voxsub-last-repair-20260927-001933/A/` 中 red.log、red-owner-collision.log、green.log。
- 文件：frontend/src/main/main.ts、backend.ts、renderer/views/migration.ts、frontend/package.json；新增 frontend/tools/test-migration-takeover.mjs。

## B：idle stop 的失效收尾

- 原失败：`test_stale_idle_stop_cannot_finalize_a_new_start` 中实际 start 发布 worker 后，旧 stop 把 RUNNING 写成 IDLE，RED **1 failed, 1 passed in 1.33s**。
- 修复：沿用 _state_lock，单一生命周期代次拒绝失效 finalizer；外部 TTS shutdown 期间用短期 finalizer 计数关闭启动/资源替换入口，不持状态锁 join/close。旧 stop 返回 False，不释放新资源；也覆盖新启动失败后没有活 worker 的代次变化。
- 新增 tests/test_pipeline_stale_stop.py；修改 voxsub/pipeline.py。Event 控制 idle 决策后暂停、真实 Pipeline.start、真实 Python worker loops、再释放 stop，检查状态/worker/stop flag/资源身份/TTS 与返回值；仅替换硬件/模型/TTS 外部边界。
- 定向命令：项目 venv `-m pytest tests/test_pipeline_stale_stop.py tests/test_pipeline_settling.py tests/test_pipeline_setter_atomicity.py tests/test_pipeline.py tests/test_architecture.py -q -m "not integration and not hardware_audio" --basetemp <本次独占scratch>/pytest`；隔离细节见 B/run_checks.py 与原始日志。GREEN **106 passed, 3 deselected in 9.02s**，含正常启停、构建取消、超时/TTS、close 交错、延迟清理、setter 和架构门禁。
- RED/GREEN 证据位于 `E:/Hermes_data/cache/scratch/voxsub-last-repair-20260927-001933/B/red-0ca4b28d/result.txt`、`B/delivery-7310de3c/result.txt`。结论限于已覆盖的公共接口并发时序；真实桌面操作链 NOT_RUN。

## C：安全说明修复

- docs/HANDOVER.md §一改为可直接执行的 Windows Git Bash 隔离命令；显式清除 PYTHONPATH/PYTHONHOME、使用项目 venv、独占 Hermes scratch、隔离四环境目录、打印解析后路径。
- 默认 Python 表达式为 `not integration and not hardware_audio`；前端为 `npm run check` 和 `npm run test:acceptance-contracts`。L3 单列 `tests/test_ipc_integration.py -m "integration and not hardware_audio"`。
- `scripts/run_tests.py` 历史目录修剪、默认 pytest 仍含 integration、`npm run verify`/probe 继承用户环境且包含配置写操作均显式警告；未删除或改造这些工具。
- 旧 `.backups/phase1_20260921_042735/` 重新枚举：目录存在、文件列表为空。旧报告的 cp 示例已标注 **历史备份不可用，以下命令不得执行**，改为 text 历史记录；局部恢复后删除测试的建议已撤销。未从 Git 重建文件冒充旧备份。
- 按 HANDOVER 的真实 L3 代码块在 A/B 合并后再次执行：`7 passed in 3.14s`，exit 0，日志 `E:/Hermes_data/cache/scratch/voxsub-last-repair-20260927-001933/ipc-20260927-004728.log`。合并后的默认安全命令亦已原样执行，结果见下方。

## 本地分项提交

- A：`41e3968ca5ff91f61104941c7ef84d16f388b4b8` — renderer 接管的任务级退出保护。
- B：`43cac3427c895d64305722db18ce7aa6d36457a8` — 拒绝新启动之后的失效 stop 收尾。
- B 复审修正：`3a58046d6d48160c78310f24326bf0031d079aa4` — 延迟 settlement watcher 同样携带代次并走公共 finalizer，含两条先 RED 后 GREEN 的确定性回归。
- C：包含本文件及 HANDOVER、MAINTAINABILITY_REPORT、FINISH 历史指引、STATUS、TODO 的最终文档提交。完整最终 SHA/提交列表写入仓库外回退证据和最终回复，避免为自引用新增未演练提交。
- 全部基于 `1a3d47769c0c2ef914648b2794cb1bf3d0d976a8`，未推送。最终工作区状态与提交范围 diff check 以仓库外交付日志为准。

## 三个可靠 Git 回退目标

**共同起点**：本轮最终交付 HEAD（包含 A、B 代码提交和最终交接文档提交）。精确 SHA、命令列表与 tree 比对保存在仓库外 `E:/Hermes_data/cache/scratch/voxsub-last-repair-20260927-001933/rollback-evidence.json`；最终回复亦提供 SHA。不要把早先只含代码的演练代替此起点。

| 目标 | 目标 SHA | 逆序撤销范围 | 文档/新增文件 |
|---|---|---|---|
| 仅撤销本次最后一轮返修 | `1a3d47769c0c2ef914648b2794cb1bf3d0d976a8` | 本轮所有提交，最新文档 → `3a58046` → `43cac34` → `41e3968` | 包含本轮文档；本轮新增 tracked 文件由 revert 删除 |
| 撤销此前返修，回到上一基线 | `1c0781e3d647846844040c21ed38f4882c5db93c` | 上行全部，再 `1a3d477` → `8af1258` → `869be20` → `f50dd05` | 包含上一轮最终文档和本轮文档，不遗留新增测试/模块 |
| 连同此前公共组件增量撤销 | `6cfbbc7569b4627e3bf81d101f3455939ceb1e89` | 上行全部，再 `1c0781e` → `afbe3c8` → `a99467e` → `1d3b842` → `9cfc614` | 全部对应文档及 tracked 新文件一并回退 |

### 执行前置条件

1. 首先保存本说明与仓库外证据。核对实际 HEAD 等于上述演练的最终交付 SHA；出现后续提交时**停止照抄**，须重新评估后续工作与依赖。
2. `git status --porcelain=v1` 必须为空；未提交、未跟踪或他人的改动先妥善交接，不覆盖、不自动 stash、不 reset --hard、不切换分支。
3. 目标必须是当前 HEAD 的祖先，范围不能包含 merge；逐条审阅待撤销 SHA 与文件清单。此说明不授权在用户工作区自动执行回滚。
4. 命令按 Git 拓扑逆序列出最晚提交在前，包含文档。下列示例仅选择“撤销本次”的目标；另两目标仅替换成表中完整 SHA。先在独立 scratch clone 演练，再获准用于实际仓库。

```bash
cd D:/OneDrive/app_dve/VoxSub
# 人工核对 HEAD 与仓库外最终演练日志一致后，才继续。
start=$(git rev-parse HEAD)
target=1a3d47769c0c2ef914648b2794cb1bf3d0d976a8
test -z "$(git status --porcelain=v1)" || exit 1
git merge-base --is-ancestor "$target" "$start" || exit 1
test -z "$(git rev-list --merges "$target..$start")" || exit 1
git log --format='%H %s' "$target..$start"
# 上方列表经人工核对后运行：
commits=$(git rev-list --topo-order "$target..$start")
test -n "$commits" || exit 1
git revert --no-edit $commits
```

出现冲突时停止，保留命令与冲突清单，不采用 ours/theirs 批量覆盖。选择撤销此次序列可执行 `git revert --abort`（丢弃本次冲突解决，不会撤销开始前的已提交历史）；若需要人工解决，则再次审阅/测试后 `git revert --continue`，并重新比对 tree。最终要求 `git rev-parse HEAD^{tree}` 等于 `git rev-parse <target>^{tree}`，且 status 为空。

Git revert 只影响跟踪源码/文档，不恢复或删除用户配置、模型、迁移数据、安装目录、Release、ignored `.backups` 或 scratch。新增 tracked 文件由对应 inverse diff 处理；不要手工广泛删除。Git 树恢复不等于运行时用户数据恢复。

### 最终交付演练程序

先完成代码、审查修正和所有交接文档的提交，再从那个最终 HEAD 创建三个独立 scratch clone；分别逆序 revert 至上表目标，比较完整 Git tree、确认工作区干净。证据保存在仓库外，不再为写入演练数字产生新的文档提交；如仍追加提交，必须从新的最终 HEAD 重做三条演练。演练不得回滚原仓库。

## 备份（仅补充，不替代 Git）

本轮修改前快照：`.backups/last-repair-20260927-001933/`；21 个现有文件逐字节核验，RESTORE.md 写明基线、清单、新增文件及恢复方式。复审改动前另备份 pipeline.py 和新增测试于其 `review-watcher/` 子目录（这是基于 `43cac34` 的中途工作树快照，不能冒充起始基线）。起始备份是**部分文件的基线快照**，复审子目录是中途快照；两者都不是整个仓库备份；不得盲目覆盖后续工作。实现期间若新增文件/补充快照，按该目录各 manifest 区分；只有 Git 完整回退可恢复整个版本树。

## 最终验证与独立审查

主代理按 HANDOVER 第一段代码块原样执行，日志 `E:/Hermes_data/cache/scratch/voxsub-last-repair-20260927-001933/safe-20260927-004616.log`：

- Python 安全套件：**917 passed, 5 skipped, 17 deselected, 1 xfailed, 1 warning in 37.29s**。5 skipped 为缺 ASR/翻译/TTS 模型、诊断模型目录及 Windows 符号链接权限；不算通过。warning 为既有 tar extractall 的 Python 3.14 行为弃用提示，本轮不扩展修复。
- `npm run check`：exit 0，含 TypeScript、色板及逻辑套件；新增迁移接管 **21 checks**，既有页面 **126/126**。
- `npm run test:acceptance-contracts`：exit 0，**25/25 structural contracts** 与 **11 in-memory negative controls**；结构验证不是实际产品链路证明。
- 真实 sidecar：单列 **7 passed**，不与单元/定向数量相加。
- `git diff --check`：exit 0；最终提交范围 diff check 随最终 Git 检查记录。

### 独立审查与返修

- 初审：未参与实现的 Review Agent `sa-0-c06da401`（deleg_b2f70cd5）。A 组合测试独立重跑 **21 checks passed**；C 安全命令静态核对未见阻断。B 首批 **6 passed**，但独立额外探针确定性复现 watcher 遗漏：TTS 超时 → 旧 watcher 暂停 → 第二次 stop → 新 start → 旧 watcher 将新 RUNNING 写为 IDLE（1 failed in 0.84s）。因此先前“B 已完成”的阶段判断不构成最终闭环。
- 返修：主代理先加两条确定性测试，RED **2 failed, 6 deselected in 1.42s**；一条检查旧观察不能停止新 TTS/覆盖新运行，一条检查外部 shutdown 中允许获取状态锁、但仍拒绝 start/资源替换。watcher 捕获原代次并复用公共 finalizer，取消绕过仲裁的直接 TTS stop/IDLE 写入；新代次可建立自己的 watcher，旧代次不得阻断新收尾。GREEN **108 passed, 3 deselected in 9.96s**（含原生命周期与架构门禁）。
- 证据：`B/watcher-red-335c7f10/result.txt` 与 `B/watcher-green-135c6170/result.txt`，相对本轮仓库外证据根目录；审查原始探针在 `E:/Hermes_data/cache/scratch/voxsub-review-651edb7d/test_watcher_review.py`。
- 定向复审：另一个未参与实现的 Review Agent `sa-0-5aa272b3`（deleg_ee57d46e）仅复核这项阻断与新 delta，独立实跑两模块 **39 passed in 5.91s**（exit 0）。结论：watcher P1 已闭环，所审范围未发现未解决阻断；无锁内外部 shutdown、新代次清理所有权丢失的证据。证据 `B/review-watch-b96399b8/result.txt`。
- 主代理已核读原始 RED、GREEN、复审测试日志；各专项与全套有重叠，不累加数量。独立代码审查不是 GUI 验收，也不是项目经理批准。

## 未验证与安全降级

GUI/真实桌面操作链、真实用户目录迁移/删除、模型推理、音频捕获/播放、硬件实机工作流、冻结 sidecar/安装包仍 NOT_RUN。清理硬超时无自动解锁/权威恢复通道，仍保留安全保护；本轮不扩展模型导入恢复或全量 IPC 强制校验。

源码返修、范围内独立审查与安全门禁已闭环。最终提交后的三个回退目标演练及工作区状态以仓库外证据为最终交付条件；不能用本文代替未执行的演练。

**返修完成，等待项目经理复验。** 此文档不是项目经理批准记录。
