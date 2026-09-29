# VoxSub 验收返修交付记录（2026-09-29）

## 1. 总结与验收边界

本轮仅修复录音状态持续同步、Pipeline 收尾能力通知、窗口销毁后的迟到操作，并统一交接。字幕滚动与日志时间沿用已有实现，只做回归，不计为本轮新功能。源码不会自动更新已安装版本；没有构建、安装、替换安装目录或修改 Release。

录音开关仅决定麦克风模式是否保存 WAV，不是系统音采集开关。界面持续反映后端确认值；未知、断连或未确认时不可把旧值显示为当前事实。停止完成后能力通知与 setter 准入一致；窗口被销毁后的迟到操作被局部忽略，活窗口仍正常工作。

本轮源码返修、完整隔离回归及范围内独立审查已完成，无未解决的范围内代码阻断（BLOCKED：无）。最终文档提交与回退演练的精确结果由提交后生成的 DELIVERY.json 记录。真实体验仍需甲方复验，本文件不是项目经理批准或正式发布记录。

## 2. Git、备份与逐项修复

- 分支：`main`。
- 起始 HEAD：`ce74a02988c3e11cd82c33cbcdd1d119027207cd`，开始时工作区干净。
- 当前代码 HEAD：`992790b73201524f9c770c42e1c9c1b7640fe32b`。后续文档提交会改变 HEAD，因此不能把代码 SHA 标成最终交付 SHA。
- 最终含文档 HEAD、最终工作区状态、分阶段提交与回退结果：以提交后生成的仓库外 `C:/Users/Zhang Ruiduo/Hermes/Tool_cache/voxsub-acceptance-20260929/DELIVERY.json` 为准。可用 `git rev-parse HEAD` 与之核对；避免文档记录自身 SHA 的自引用循环。
- 本轮只本地提交，**不推送**。之前已推送的批次不代表本轮已推送。
- 基线备份：`.backups/acceptance-repair-20260929-161514/baseline-tracked.zip`、`manifest.json`、`RESTORE.md`。304 个 Git 跟踪文件、ZIP CRC 校验通过；是完整的当时 tracked-source 快照，不是工作机、用户数据、依赖、模型、dist 或安装包备份。
- 新增文件登记：同目录 `NEW_FILES_phase1.txt`、`NEW_FILES_phase2-3.txt`。本报告也是本轮新增 tracked 文件；以最终 `git diff --name-status ce74a02988c3e11cd82c33cbcdd1d119027207cd..HEAD` 为完整修改/新增清单。没有把中途快照当成基线。

### 2.1 录音控制实时同步

根因：workspace 只初始 refresh，未消费 store 后续录音快照；旧监听挂在 window 而状态广播在 document。ready/state 查询没有统一恢复录音字段；迟到查询/回执、旧页释放可能覆盖新状态。

文件：`frontend/src/renderer/{store.ts,protocol.ts,recording-control.ts,views/workspace.ts}`；`frontend/tools/test-recording-control.mjs`、新增 `test-recording-workspace-entry.ts`、`test-recording-workspace.mjs`；默认门禁 `frontend/package.json`；生命周期测试 `test-page-dispose.mjs`。

行为：复用 store.subscribe 与 PageLifecycle，同一页面 owner 持续消费权威快照；dispose 成对解除订阅，重建先释放旧页；ready/断连/未知清除旧能力；新事件优先于旧查询与点击回执。recordingReason 只接收可选字符串并作纯文本显示，当前后端没有发该字段，不伪造后端拒绝原因。

测试：`node tools/test-recording-control.mjs`（通过隔离 runner）。先 RED：store 收到 active=true，DOM 红点仍 hidden；随后分片 RED→GREEN 覆盖握手、模式、暂停、断连、拒绝/超时、重建、监听释放和竞态。22 个真实生产 workspace/store 装配 MiniDOM 用例 + 6 个 controller 测试组通过；不是 28 条独立断言、不是 Chromium/Electron。

旧页面测试改为测量真实 store 订阅/同一 listener 释放，不添加无用途 window 监听器。正常释放的零残留断言保留；不释放负对照现仅设置页逐轮留下 window 监听，workspace 监听改由 store 专项计数。

提交：`ee797edfbce8e31120d1d41adb8e9a36525071dd`。

### 2.2 Pipeline 最终停止状态

根因：IDLE 事件发生于 `_stop_finalizers` 归零之前；取消启动的 builder 也可能尚持有 admission。最后事件不可修改，与随后实际 setter 准入不一致。

文件：`voxsub/pipeline.py`、`tests/test_pipeline_stale_stop.py`、新增 `tests/test_pipeline_recording_events.py`。

行为：最后 stop finalizer 释放后发布当前真实状态；取消启动的最后 builder 释放后也补发。保留 lifecycle generation 防护；不写死 capability=true；不在状态锁内关闭外部资源。

测试：真实公共 start/stop/close、watcher 与 state callback；仅模型/设备边界替身。同步/重复 stop、TTS/capture 延迟和超时收尾、重叠 owner、新代次交错、取消启动、close、setter 一致性均使用 Event 控制。同步 RED 真实捕获最终事件 recordingCanChange=false。定向 28 passed；含 architecture 的集合 40 passed（包含前述 28，不能相加）。builder RED 日志另含两个已纠正的假 TTS fixture 超时，不把测试 fixture 故障描述为产品故障。

提交：`c0a89f5c7cb62eab89bf7157e97b57c40879dadd`。

### 2.3 BrowserWindow 销毁保护

根因：已保护 backend 通知，但 click-through、toggle/show/hide、size、second-instance restore/show/focus 仍直接使用可能已销毁的对象；旧主窗 close 回调也可能通过 busy guard 操作新主窗。

文件：`frontend/src/main/main.ts`、`frontend/tools/test-window-lifecycle.mjs`。

行为：复用精确 destroyed 错误局部处理，useWindow 固定本次窗口 owner；检查后销毁也安全忽略，无关异常继续传播。旧 close/ready/closed 回调不影响新窗，退出后不再重建或发出无意义操作。show/hide 返回布尔执行结果、resize 失败返回 null；click-through 保持配置意图回执契约，不把回执当 native 操作成功证明。

测试：`node tools/test-window-lifecycle.mjs`；fail-closed Electron 替身驱动真实 Main/BackendBridge、IPC、托盘/app 回调。第一轮 RED 23 failed / 22 passed；旧 close owner 补充 RED 1 failed / 45 passed；GREEN 46 passed / 0 failed。覆盖活窗、已毁、check-to-call、重建、退出、不同 owner、精确异常匹配与不相关错误传播。**不是静默真实 Electron 测试**。

提交：`6366841ebc4d8897e885e90f7d08334408dd9c9d`。

### 2.4 滚动、日志、交接

滚动实现源自 `68c3b4d4c9d8a5e86d87c540a3a87bb28156c554`，日志源自 `ac957dc9cc22d27240641cdf442fe61a4b63fe3b`。本轮不改它们的生产实现；分别执行真实 headless Chromium 布局/跟随测试及日志时间多 TZ 逻辑/页面测试，结果见矩阵。

统一 `STATUS.md`、`TODO.txt`、`docs/HANDOVER.md`、两个体验历史报告及本报告。历史未完成、未推送结论标明当时快照并被当前交付覆盖；文档修订不是产品功能修复。

### 2.5 最终全量发现的契约漏项

首次最终 Python 门禁：941 passed / **1 failed**，不是通过；失败为 TS 的 recordingReason 未同步 JSON Schema。新增 `tests/test_recording_contract.py` 实测 4 RED 后，在 `contracts/commands.json`、`events.json`、`protocol.json` 对三个 StatePayload 和 state event 增加可选 string 字段，不放宽 required 或其他类型规则。新测试覆盖旧载荷、有效原因、错误类型；与契约集 GREEN 74 passed。

提交：`d222afcaba18720fd1154be35dd907f10190a05c`。仅补齐第1阶段兼容契约，不新增后端原因生产。原始 RED/GREEN：`contract-red-c7bd4433` / `contract-green-0b637399`。此后重新运行全部最终门禁，以下 verified-* 均基于该代码 SHA。

### 2.6 独立审查 R1/R2 返修

提交：`992790b73201524f9c770c42e1c9c1b7640fe32b`；文件 `frontend/src/renderer/store.ts`、`views/workspace.ts`、`frontend/tools/test-recording-workspace.mjs`。

R1 根因是已发出的请求权威绑定到已销毁页面；改为共享请求所有者，发出时撤销旧快照，新页等待同一写入。成功回执更新当前共享状态，拒绝/超时/异常保持未知；旧页面DOM不更新，也不需要额外state事件。R2 根因是会话命令回执绕过查询代次保护；start/pause/resume/stop统一使用revision/连接/快照保护，新事件、新连接或新命令优先。重复null失效也以revision通知，不遗漏取消待发意图。

实际 RED：独立probe `r1r2-probe-red-d2941ce3`；默认R1 `r1-default-red-9400c27c`（6失败）；默认R2 `r2-default-red-6ac50507`（12失败）。GREEN：原controller6组保留，原22个workspace测试未放宽，扩至46/46；原独立probe两条通过。`r1r2-final-check-14b23fb0` 完整check通过后提交；提交后录音/probe分别 `r1r2-committed-recording-b4500dc7`、`r1r2-committed-probe-a963adcb` 通过。最终父代理全套重跑见下表。

四文件中途快照 `E:/Hermes_data/cache/scratch/voxsub-acceptance-20260929/intermediate-snapshot-20260929-175753/`（含哈希与恢复说明）不是新基线，不替代最初304文件ZIP。

## 3. 测试矩阵与审查

**最终表格 delivery-* 全部在 992790b 上重新执行。** 早期 verified-* 是 d222afc 上的修复前全量记录，不代表独立审查通过。 独立审查虽重跑原录音22/22和窗口46/46通过，但新增两条真实装配复现失败；旧测试绿不能覆盖新遗漏。R1：旧页发出set_recording后新页重建，成功回执被旧controller生命周期丢弃，新页把旧false当已知。R2：旧pause回执可绕过revision保护，覆盖新页已收到的IDLE可修改事件。两项已在第2.6节修复并补默认回归，完整门禁已重跑；最终复审结论独立记录，不覆盖首次失败报告。

执行环境：项目 `.venv/Scripts/python.exe` Python 3.12.4；Node v26.7.0 使用 exact exe；npm 通过该 Node 的 npm-cli.js，不依赖有 TTY 错误的 shell shim。每次独占 `E:/Hermes_data/cache/scratch/voxsub-acceptance-20260929/<label>-<uuid>`，隔离 APPDATA/LOCALAPPDATA/TEMP/TMP/TMPDIR，清除 PYTHONPATH/PYTHONHOME；TZ=UTC，日志专项显式测试各 TZ；pytest `-B -p no:cacheprovider`，npm offline。

执行包装器：`E:/Hermes_data/cache/scratch/voxsub-acceptance-20260929/run.py`，最终归档到上述持久证据目录的 evidence.zip 内 run/run.py。每条 `result.json` 保存命令、HEAD、环境、退出码，`result.log` 保存完整输出。所有下列命令均从仓库根通过该 runner 调用；npm/node 实际 cwd=frontend。

```bash
# 隔离 runner 用法（LABEL 每次独占 UUID，不清理旧结果）
env -u PYTHONPATH -u PYTHONHOME .venv/Scripts/python.exe -B E:/Hermes_data/cache/scratch/voxsub-acceptance-20260929/run.py LABEL pytest -m 'not integration and not hardware_audio'
env -u PYTHONPATH -u PYTHONHOME .venv/Scripts/python.exe -B E:/Hermes_data/cache/scratch/voxsub-acceptance-20260929/run.py LABEL npm run check
env -u PYTHONPATH -u PYTHONHOME .venv/Scripts/python.exe -B E:/Hermes_data/cache/scratch/voxsub-acceptance-20260929/run.py LABEL node tools/test-recording-control.mjs
```

| 层级/命令 | 状态与实际结果 |
|---|---|
| 基线 Python 安全集 | PASS：934 passed / 5 skipped / 17 deselected / 1 xfailed / 1 warning；旧报告944不是本轮基线 |
| 基线 npm check | PASS |
| 最终 Python 单元/行为安全集 | PASS：946 passed / 5 skipped / 17 deselected / 1 xfailed / 1 warning，40.51s；delivery-python-618c4257 |
| 定向 recording_switch/log_timestamps/pipeline_stale_stop | PASS：25 passed，3.17s；delivery-target-6c0140fe；另外含 recording_events 的集合33 passed，与前者重叠 |
| 最终 npm run check | PASS exit 0；delivery-check-8b4106e3；含 typecheck/logic 与真实生产 renderer MiniDOM 门禁 |
| npm run test:acceptance-contracts | PASS：25结构契约+11内存负对照；delivery-acceptance-9c7348af；不是运行时证明 |
| node tools/test-recording-control.mjs | PASS：controller 6组、真实生产 renderer 装配 MiniDOM 46/46；delivery-recording-56b551b9 |
| node tools/test-window-lifecycle.mjs | PASS：46/46；delivery-windows-71d833ff；Main行为 + Electron替身 |
| node tools/test-workspace-scroll.mjs | PASS：5种viewport/DPR/语言组合、25条证据记录；delivery-scroll-f898591d；真实headless Chromium，非Electron |
| node tools/test-log-time.mjs | PASS：5测试组；delivery-log-31ba36b6；逻辑/MiniDOM + 4个显式TZ（含DST） |
| 真实 sidecar：tests/test_ipc_integration.py | PASS：7 passed，3.19s；delivery-sidecar-45020ac8；单列真实进程通信 |
| 静默真实 Electron 生命周期 | NOT_RUN：本轮使用 fail-closed 替身，未建立独立 native 运行证据 |
| 合成音频完整链路 | NOT_RUN：未运行合成语音/ASR/TTS完整链路；fixture PCM/WAV测试不冒充端到端 |
| 真实麦克风 | NOT_RUN |
| 真实系统声音/loopback | NOT_RUN |
| 真实模型、硬件加速 | NOT_RUN |
| 打包版 | NOT_RUN |
| 安装包和 Release | NOT_RUN |
| git diff --check 与基线..HEAD --check | PASS exit 0；最终提交后再核对并写 DELIVERY.json |
| 独立只读审查 | PASS（最终）：独立复审992790b，R1/R2关闭，无范围内阻断。原probe2/2、默认录音controller6组+MiniDOM46/46、typecheck通过；independent-review-final.json。首次FAIL报告保留 |

独立复审证据：`rereview-probe-2d20ddf1` 中R1实际收敛checked=true（不是仅unknown兜底），R2保持新IDLE可修改；`rereview-recording-f6b24c5b`、`rereview-typecheck-a6589830` 均exit0。复审未重跑其他全量，矩阵中的完整门禁由父代理真实执行，不声称这些总数得到独立全量背书。

首次滚动运行 FAIL（final-scroll-3dbcb982）：读取浏览器启动中的 DevToolsActivePort 出现 EBUSY；隔离重新启动后通过，最终代码上再次通过。没有修改生产逻辑/测试断言，也没有隐藏该次失败。已知余留风险：该启动文件读取仍可能瞬时被锁，需独占profile重跑，不把它解释为滚动产品bug。

SKIP 5：ASR/翻译/TTS/诊断缺真实模型各一；符号链接因 WinError 1314 缺权限一。XFAIL 1 是既有预期失败；17条排除项不是通过；唯一warning为既有tar解包未来Python行为弃用提示。没有把旧944数字沿用到新代码。

FAIL 为上文真实 RED/首次回归记录，不是最终门禁结果；SKIP/XFAIL/排除均不得计作 PASS。不同定向集合与全量存在重叠，不累加总数。BLOCKED：当前无未解决的范围内代码阻断；真实环境未执行项仍为 NOT_RUN，不用它隐藏执行失败。

副作用边界：`prebuild` 实际仍调用原地修改源码的 sanitize.mjs；本轮不运行 build/prebuild，不改 dist。旧 HANDOVER 对“默认不写源”的泛化说法已更正。sidecar 仅独立测试进程与隔离配置，包含只读能力/窗口枚举，无音频捕获/播放或用户目录迁移。headless 浏览器使用独占 profile、mute、禁扩展，不占用户浏览器。

## 4. 回滚说明

共同条件：先保存本说明与仓库外交付清单；实际 HEAD 必须等于 DELIVERY.json 的最终 SHA；工作区必须干净；待撤销范围无 merge、目标为祖先。后续有新提交或他人改动时停止照抄。冲突立即停止，保留证据，人工评估后 `git revert --continue` 或 `git revert --abort`，不 reset hard、不强制 ours/theirs、不切换真实分支。

| 路径 | 起点→目标/顺序 | 文档、新增文件与演练 |
|---|---|---|
| 仅本轮文档 | 最终 HEAD → 最终代码 HEAD；revert 最后文档提交 | 删除本轮新报告、恢复旧文档；最终演练见 DELIVERY.json |
| 仅本轮返修代码 | 最终 HEAD 逆序 revert 992790b…、d222afc…、6366841…、c0a89f5…、ee797ed…；保留文档 | 回到 ce74a02 的代码，文档将变成历史说明；新增测试由 revert 删除。NOT_RUN：不宣称完整 tree 等于 ce74a02，因为文档刻意保留 |
| 仅体验代码（含滚动/录音/主进程/日志） | 上行后，再逆序 revert 68c3b4d…、ac957dc…、1d61547… | 保留两轮文档，代码回到7e9e5cb；NOT_RUN，遇依赖冲突停止，不分拆68c3b4d中的滚动/录音 |
| 撤销本轮全部返修 | 最终 HEAD → ce74a02988c3e11cd82c33cbcdd1d119027207cd；范围内全部提交按最新到最旧 revert | 包含本轮文档与代码，完整 tree 应匹配目标 |
| 回到上一轮限定返修完成版 | 最终 HEAD → 7e9e5cbcc895ae8740cbd6532091a09471f3c098；范围内全部提交逆序 | 包含两轮体验及本轮文档/代码，新增 tracked 文件由 inverse diff 处理 |
| 回到更早维护基线 | 最终 HEAD → 6cfbbc7569b4627e3bf81d101f3455939ceb1e89；范围内全部提交逆序 | 同时撤销中间维护批次和文档，完整 tree 应匹配目标 |

完整目标的可执行形式（必须先核实起点并人工审阅提交列表；本说明不授权自动回滚真实工作区）：

```bash
start=$(git rev-parse HEAD)
target=ce74a02988c3e11cd82c33cbcdd1d119027207cd
test -z "$(git status --porcelain=v1)" || exit 1
git merge-base --is-ancestor "$target" "$start" || exit 1
test -z "$(git rev-list --merges "$target..$start")" || exit 1
git log --format='%H %s' "$target..$start"
# 人工核对 DELIVERY.json 的精确起点、目标、顺序后：
git revert --no-edit $(git rev-list --topo-order "$target..$start")
```

仅代码路径精确提交 SHA 从第2节及两个历史体验报告读取；逐条执行 `git revert --no-edit <SHA>`，不包含文档提交。无需手动删除新文件，Git inverse diff 负责对应新增 tracked 文件。

隔离演练必须从含最终文档的 HEAD 新建 scratch clone，分别对文档-only、本轮全部、上一返修、更早基线执行，比较**完整 Git tree 与干净 status**，不操作真实工作区。结果放仓库外 `rollback-final.json` / DELIVERY.json；任何追加提交后需重新演练。交付判据为 DELIVERY.json 中 rollback.pass=true 且 start 等于最终 HEAD；对应四条路径须逐项 tree==target_tree、status为空。没有该精确版本证据时不得宣称演练通过，不引用旧批次演练冒充本轮。

所有回滚仅针对 tracked 源码/文档，不影响用户配置、模型、迁移数据、安装目录、Release、ignored备份或 scratch。手工备份恢复只按 manifest 恢复明确路径，新增文件先对照最终清单，禁止覆盖后来编辑。
