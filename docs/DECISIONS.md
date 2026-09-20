# 语幕 VoxSub —— 重要技术决策记录（ADR 简版）

> 任务书要求"重要技术决策及理由"作为最终交付物之一。本文件只记**决策 + 理由 +
> 代价**，不记流水账。每条都对应代码里的注释或测试，想深挖顺着文件:行号去读。
>
> 记录原则：**写明我们放弃了什么**。只说"我们选了 X"没有价值，说清"为什么不是 Y"
> 才能让后来者不再重走一遍。

---

## 1. 删除授权：以"台账记录"为准，而不是路径白名单

**决策**：迁移后的源目录清理只接受台账记录标识（`record_id`），路径由后端从
已验证的迁移记录解析；调用方**无法**指定路径。

**为什么不是"目录黑名单"**：黑名单永远会漏。历史实现用 4 个写死的系统路径做比较，
实测确认它接受任意存在的目录 —— 只要会写一行 IPC 就能删掉任何东西。

**为什么也不是"可删路径白名单"**：合法要删的恰恰是**旧位置**（老模型目录、老缓存、
老安装目录），它们天然在应用当前数据根之外。一张"好目录"名单要么写得很宽（等于没有），
要么写得很窄（功能不可用）。所以授权依据换成"这件事发生过、且校验通过"。

**代价**：本地有写权限的攻击者可以改台账 JSON。威胁模型是"协议层攻击者无法指定路径"，
这一点已在代码注释里写明，不在本轮防御范围。剩下的护栏（卷根/受保护目录/重解析点/
包含正在使用的数据根/UNC 与设备路径）仍然拦住最危险的那几类。

**代码**：`frontend/backend/migration_ledger.py`、`handlers/migration.py::_cmd_cleanup_migrated_source`
**测试**：`tests/test_migration_ledger.py`（27 条，每个拒绝理由一条）

---

## 2. 长任务用"单 worker + 有界队列"，不改 asyncio

**决策**：耗时命令进一个单线程 worker，队列有界（`MAX_PENDING=16`），满了明确拒绝；
控制命令（`ping`/`state`/`shutdown`/`job_*`/`cancel_job`）**在读循环线程上就地执行**。

**为什么不是 asyncio**：任务书明确"不要默认把全部代码改成 asyncio，优先延续当前线程模型"。
而且真正的痛点不是并发模型，是"读循环被长任务占住"—— 这一点用"分离控制通道"就解决了，
改动面小一个数量级。

**为什么是单 worker 而不是线程池**：OCR 的原生引擎不能被并发调用；迁移与目录破坏性
操作必须互斥。串行是最省心的正确起点，等真有多路并行需求再显式声明并发能力。

**为什么队列必须有界**：无界执行器会把积压藏起来 —— 上游一直塞、下游慢慢处理，
表现是内存涨和"点了没反应"，而没有任何一处报错。

**代码**：`frontend/backend/job_runner.py`、`frontend/backend/ipc_loop.py`
**测试**：`tests/test_job_runner.py`、`tests/test_ipc_loop.py`、
`tests/test_ipc_integration.py`（真管道下证明控制命令不被队列堵住）

---

## 3. 一个状态只允许一个字段名（`action` → `status`）

**决策**：任务事件里描述状态只用 `status`，删掉同义的 `action`。

**为什么**：这两个名字曾经同时存在 —— 后端发 `action`，前端 TS 声明的是可选字段
`status`。字段可选 ⇒ 读不到就是 `undefined` ⇒ **不报错、不崩**，只是"任务跑完了
界面不显示结果、日志里也没有原因"。这类静默漂移比崩溃难查得多。

**代价**：一次跨语言的改动。但换来一条可机械验证的规则，值。

**代码**：`frontend/backend/job_runner.py::_emit`、`frontend/src/shared/request-outcome.ts`
**测试**：`tests/test_job_runner.py::test_job_event_uses_one_field_name_for_state`
（断言事件里**不再出现** `action`）、`tests/test_contracts.py` 的双向字段比对 + 棘轮清单

---

## 4. 契约运行时校验**默认只告警**（`CONTRACT_ENFORCE = False`）

**决策**：`contracts/` 是单一来源、有一致性测试；运行时也接了线（唯一出站口
`IpcLoop._send()`），但默认只把不一致记成 WARNING，**不拒绝请求**。

**为什么**：契约写错不该比没有契约更糟。如果这一版契约有偏差就硬拒绝，代价是用户功能
直接不可用。所以按任务书 §3.6 的"分两步实施"：第一步集中定义 + 一致性测试（已完成），
第二步运行时强制。

**翻成 `True` 的条件**（写在代码注释里）：连续跑通全量测试 + 一次打包版冒烟，且出站零告警。

**已知限制**：`contracts/` 不在 PyInstaller bundle 内，所以运行时校验在**打包版上是静默
关闭的**。要让它在打包版生效，需要把 `contracts/` 一并打进 bundle。

**代码**：`frontend/backend/ipc_loop.py`（`CONTRACT_ENFORCE`、`_send`、`_load_contracts`）

---

## 5. `stop()` 超时后保持"停止中"，而不是落 IDLE

**决策**：`stop()` 的 8 秒 join 是"UI 别卡住"的预算，不是"原生推理一定能在这之内停"
的承诺。超时后**保持 STOPPING**，由一个有界观察者等到 worker 真退出才落 IDLE。

**为什么不能落 IDLE**：两个后果 ——
1. 界面显示"已停止"而任务还在收尾，**状态在撒谎**；
2. `set_translator`/`set_asr_model`/`set_models_dir` 这些只判断 `self._running` 的入口
   以为"已经空了"，把仍在被使用的实例关掉或换掉。

**为什么需要观察者**：没有它，超时的 pipeline 会永远停在"停止中"，用户只能重启应用 ——
那就把"诚实的状态"变成了"卡死"。观察者有界（30s）、单例、只等本实例自己的线程。

**为什么观察者用轮询而不是再次 join**：join 的超时账本归 `stop()` 主路径所有；两条路径
各算一套超时就会互相污染（实测：观察者再 join 一次会让既有的"共享截止时间"测试失败）。

**代码**：`voxsub/pipeline.py`（`_STOP_JOIN_SECONDS`、`_SETTLE_WATCH_SECONDS`、
`_may_replace_resources`、`_is_settling`、`_watch_settlement`）
**测试**：`tests/test_pipeline_settling.py`（12 条）

---

## 6. IPC 拆分用 mixin，不引入依赖注入框架

**决策**：命令实现按业务域拆进 `handlers/*.py` 的 mixin 类，`BackendService` 多重继承；
不引入 service locator 或 DI 框架。

**为什么不是依赖注入**：任务书明确"不要引入全局 service locator 或重型依赖注入框架"。
mixin 的价值在于**方法仍在同一个 self 上**，于是 `getattr(self, f"_cmd_{name}")` 这条
动态分派、`has_command()`、以及测试里的 `service._cmd_xxx(...)` 全部**不用改** ——
这就是"现有公共入口保留为兼容 facade"。

**搬移方式**：**按行原样切片**，不重新打印 AST。注释、docstring、内部 import 逐字节不变，
把"搬移"这一步的出错面压到最小。

**连带成本（必须一起做，否则门禁假阳性）**：静态取证类测试要扫全部源文件
（`BACKEND_SOURCES`），打桩点要从"再导出的名字"改到真正的实现处
（`ipc_server._emit` → `ipc_protocol._emit`）。

**代码**：`frontend/backend/handlers/`、`ipc_protocol.py`、`ipc_support.py`

---

## 7. 复杂度门禁用"棘轮基线"，不推倒重写存量

**决策**：函数分支复杂度预算 15。存量超标函数登记在 `COMPLEXITY_BASELINE` 里，
**不允许变得更差、不允许新增**，且**只允许变短**（`test_complexity_baseline_only_shrinks`
会在修好后强制你删掉条目）。

**为什么不是一刀切**：一刀切会逼着在整顿里顺手重写十几个函数，那正是任务书禁止的
"推倒重写"。棘轮能立刻拦住**新增**的复杂度，同时不动存量。

**代价**：基线表可能被当成"允许超标清单"。所以加了"只能变短"的强制测试。

**代码/测试**：`tests/test_architecture.py`

---

## 8. 公共规则只允许一处实现（原子写入、`ts`、资源门禁）

**决策**：凡是"以前写错过一次"的公共规则，都收敛成唯一实现，并加门禁防复发。

| 规则 | 唯一实现 | 防复发 |
|---|---|---|
| 原子写入（含 Windows 瞬时占用重试） | `voxsub/file_io.write_text_atomically` | 台账与 migration state 改为复用它，不再各写一套 `*.json.tmp` |
| 日志事件必带 `ts` | `IpcLoop._event` 统一 `setdefault` | 契约棘轮曾经一次抓出 5 个漏传的调用点；门禁断言注入点存在 |
| 资源能否替换 | `Pipeline._may_replace_resources` | 所有替换资源的入口都走它 |
| 出站协议写入 | `IpcLoop._send`（唯一出站口） | 少一个出口就少一处遗忘 |
| 测试临时目录 | `scripts/run_tests.py` 固定 basetemp | 门禁断言入口存在且被 gitignore 覆盖 |

**为什么值得单独列**：这类问题的共性是"**靠人记得**"。`STATUS.md` 里早就写着"遇到
os error 5 就等 1-2 秒重试"，但那是口头约定 —— 口头约定会在交接时蒸发。转成代码里的
机制才是真的修好。

---

## 9. 破坏性/打扰性测试标记必须**显式**排除，不能只靠 addopts

**决策**：任何显式 `-m integration` 都必须写成 `-m "integration and not hardware_audio"`。

**为什么**：`pytest.ini` 的 `addopts = -m "not hardware_audio"` 只保护**默认**那条路径；
命令行显式给的 `-m` 会**覆盖** addopts 的 `-m`。实测踩过一次：跑 `-m integration` 时
`test_loopback_closure_sine` 真的从扬声器播了 2 秒 440Hz 正弦 —— 而任务书明令
"测试不得弹窗、抢焦点或播放音频"。

**为什么加机械门禁而不是写进文档**：文档挡不住复制粘贴。门禁扫 CI 与文档里所有
**可执行**的 `-m` 行（跳过注释/表格/行内代码），缺排除就红；另扫 `tests/`，凡函数体里
调 `.play(` 的用例必须挂 `hardware_audio` 标记。两条都带"一个调用点都没找到就失败"的
自检，防止规则被自己的过滤条件悄悄架空。

**代码/测试**：`tests/test_pytest_policy.py`、`.github/workflows/quality.yml`

---

## 10. 有界重试，不无限重试

**决策**：所有"偶发失败"都用有界重试（`os.replace` 5 次 × 50ms），且**重试次数是可断言的
常量**。

**为什么**：无限重试会把"环境有问题"变成"程序卡死"；而完全不重试会把"同步盘瞬时占用"
变成"测试随机失败"。后者的真实代价是**最消耗排查时间**（实测：全量跑偶发红，单独重跑一定过）。

**代码**：`voxsub/file_io.py`（`_REPLACE_ATTEMPTS`、`_REPLACE_DELAY_SECONDS`）
**测试**：`tests/test_file_io.py`（含"一直被占用要有界放弃且旧文件完好"）

---

## 11. 跨语言（Python ↔ TS）的字段漂移靠双向比对，不靠人

**决策**：`contracts/` 定义命令与事件；测试**双向**比对 —— 代码里有的契约里必须有，
契约里有的代码里必须发。已知的不一致必须逐条登记在棘轮清单里（`knownGaps`/`uiFieldDrift`），
**多一笔会红、修好不删登记也会红**。

**为什么**：见第 3 条 —— 字段名对不上是静默的。而且"两边一起改"这件事本身不可靠，
所以让机器来要求两边一致。

**代价**：改协议要先改契约，多一步。这一步换来的是"不会漏"。

---

## 12. 不引入重依赖（契约校验器自己写标准库子集）

**决策**：`contract_validation.py` 只用标准库实现 JSON Schema 的一个子集，
不引入 `jsonschema`/`ajv`/`zod`/`pydantic`。

**为什么**：任务书要求"新增依赖必须先说明必要性、许可证和打包影响"。这里的校验需求是
窄的（信封 + 参数 + 事件形状），标准库子集够用；而重依赖会进打包产物、增加体积与
供应链面。不支持远程 `$ref`，因此契约文件之间禁止互相 `$ref`（由测试断言共享形状逐字段相同）。

**代价**：不是完整 JSON Schema 合规。这一点在模块文档字符串里写明，不假装合规。
