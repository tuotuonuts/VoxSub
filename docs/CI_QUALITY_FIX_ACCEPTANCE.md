# Quality CI 修复验收 — 2026-10-05

## 结论与边界

**本地五项检查路径 PASS；GitHub 新提交上的远端运行 NOT_RUN。**
本轮未 push、未重跑旧提交的 workflow、未打包/更新安装版。全程无 GUI 启动、麦克风/回环录音或音频播放。运行时 APPDATA / LOCALAPPDATA 使用仓库外隔离目录；没有改用户配置、模型、历史及应用日志。

## 原始故障与证据

- 仓库 tuotuonuts/VoxSub，Quality run `37239162634`，失败提交 `abca70d3b75ef7e330840bf7b476e474915034ba`。
- 运行创建于 2026-10-04 22:13:02 UTC，即 2026-10-05 06:13:02 UTC+8。
- 本轮源码基线为本地提交 `5caad3d83de33060fc61420d2daf17af8965ccc4`；两个根因仍存在，修复未回退此前本地功能。
- python-tests / ipc-adapters / integration：diagnostics.py 报告汇总 f-string 外层与表达式都用双引号，Python 3.11 报 unmatched '('。Python 3.12 才支持这类写法。真实 3.11 编译全部原始 169 个受跟踪 Python 文件，唯一语法错误在该处。
- frontend / release-gate：build-release.py --check-only 输出中文，Windows 重定向 stdout 为 CP1252 时抛 UnicodeEncodeError。此前远端前端类型/逻辑测试已通过，并不是前端组件报错。
- 原始日志与复现保存在 `D:/OneDrive/app_dve/ci-quality-37239162634/`：run.json、failed.log、python311-compile-reproduction.json、cp1252-reproduction.txt，未覆盖。

## 修复与回归

1. diagnostics.py 仅将 f-string 表达式内的字符串改成单引号，保持状态计数和报告结论语义。
2. 发布 CLI 在 main 首行将 stdout / stderr 配置为 UTF-8；模块导入不修改宿主捕获流，StringIO 等无 reconfigure 的流保持原样，错误退出码仍保留。
3. Quality 工作流显式设置 PYTHONUTF8=1 / PYTHONIOENCODING=utf-8。保留全部五个 job、Python 3.11、锁定依赖、门禁和原有测试命令，没有新增 skip / xfail 或 continue-on-error。
4. 新增 8 个 CI 回归用例：实际当前解释器下 CP1252 严格输出的 --check-only / --help、中文非法参数仍退出 2、双流配置、导入不改捕获流、无 reconfigure 流、混合状态计数和工作流约束。
5. 重复全量验收发现既有去重单元测试偶发失败：只模拟窗口，os.getpid()+10000 恰好撞上真实 WebView PID，psutil 把它提升至真实宿主。`full311-final.log` 保留失败证据。现在模拟进程元数据，保留原断言并新增进程名断言；另加通过真实提升算法对两个子进程去重的用例。生产录音代码未改，无硬件访问。

## 验收环境

- 使用仓库外全新 Git 检出 clean-checkout，先按基线检出，再复制本轮 5 个源码/测试文件；未复制真实模型或用户数据。
- Python 3.11.15，独立 venv311，uv 0.12.17 按未修改的 requirements.lock 安装 52 个锁定依赖。
- Python 3.12.4 为已有本地环境，另行全量回归。
- Node 22.14.0 / npm 10.9.2；在全新检出执行 npm ci，不删除或改动主工作区 node_modules。
- 本地不是 GitHub windows-latest 完全相同的 OS/补丁环境；原远端日志为 Python 3.11.9 / workflow uv 0.12.5。结论为实际同次版本验证与 CI 命令路径通过，不冒充远端验收。

## 最终结果

| 路径 | 实际执行 | 结果 |
| --- | --- | --- |
| python-tests | 3.11 compileall -q voxsub；pytest -q -ra | 1139 passed / 8 skipped / 7 deselected / 1 xfailed，0 failed |
| ipc-adapters | 原 IPC commands、logging、architecture 三文件命令 | 72 passed |
| release-gate | --check-only；原 packaging/release_gate 测试；--manifest-only | CLI 均退出 0；60 passed / 1 xfailed |
| integration | 原 integration and not hardware_audio，独立 basetemp | 8 passed / 5 skipped / 1142 deselected，0 failed |
| frontend | 全新 npm ci；完整 npm run check；3.12 --check-only；npm run build；精确 git status --porcelain 为空 | PASS；无源码改写 |
| 3.12 额外完整回归 | pytest -q -ra | 1139 passed / 8 skipped / 7 deselected / 1 xfailed，0 failed |

新增回归与进程去重专项：15 passed / 2 hardware_audio deselected。前端检查包含类型、设计令牌和完整纯逻辑/组件测试。未通过更改测试标记或解释器版本规避失败。

### 不计作通过的项目

- 全量 8 skipped：6 项缺模型/语音样本（ASR、OPUS 翻译、TTS、真实诊断模型目录、两个 Pipeline 样本）；2 项本机符号链接权限不足。集成的 5 skipped 是其中子集，不能累计为额外通过。
- 全量 7 deselected：既有 hardware_audio 默认排除，为静默要求所需，工作流未改变。
- 1 个既有 strict xfail：installer.iss OutputBaseFilename 仍直接写版本字面量；版本一致性门禁仍在。本轮未修改这条已有标记。
- npm ci 成功但 npm audit 报 3 个依赖漏洞（1 moderate / 2 high）。本轮未自动升级依赖或运行 audit fix；依赖安全处置是独立待办。
- NOT_RUN：GitHub 新提交 CI、真实音频/模型推理、可见 UI、打包及安装版。不声称旧 run 的红灯已变绿。

## 证据位置与回滚

- 外部证据根：`D:/OneDrive/app_dve/ci-quality-37239162634/`。
- 最终证据：verified-full311.log / verified-full311-results.xml、verified-full312.log、verified-ipc311.log、verified-release-check311.log、verified-release-tests311.log、verified-manifest311.log、verified-integration311.log、npm-ci.log、verified-frontend-check.log、verified-frontend-build.log、verified-frontend-clean-tree.txt。
- 源码备份：`D:/OneDrive/app_dve/VoxSub/.backups/ci-quality-fix-20261005-152654/`，含初始三文件、文档、构建清单，以及追加的 test_process_audio.py 原版。RESTORE.md 说明按相对路径还原；也可 revert 本轮交付提交。
- 恢复时只还原本轮源码，新增测试/报告可随 revert 删除；绝不恢复/删除用户模型、配置、日志或历史。
- 本轮最终主仓库提交号和验证文件 SHA256 记录在外部 DELIVERY.json；验证快照中的临时提交不是交付提交。
