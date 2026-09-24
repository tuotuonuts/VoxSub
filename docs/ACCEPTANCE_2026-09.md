# 三项需求验收方案（隔离版）

本方案只新增 `frontend/tools/test-acceptance-contracts.mjs`，不改生产代码和既有测试文件；不会启动 GUI、不会连接真实配置、不会写用户数据。

## 1. 透明度边界与保存/恢复

- 输入低于 `0.2`：应钳制为 `0.2`。
- 输入高于 `1.0`：应钳制为 `1.0`。
- 非数字/空值：应走明确默认值，不得产生 `NaN`。
- 设置后的值应通过 `overlay:opacity` 同步到 renderer；renderer 使用 `--overlay-opacity`。
- 关闭/重启后应从持久化配置恢复同一值；恢复失败时使用默认值且不污染配置。
- 验收重点：边界值、非法值、保存后恢复、恢复后的 CSS 值与 IPC 回读一致。

当前静态门禁只能验证 IPC 边界和同步骨架；若“设置字段 + 启动恢复”尚未合入，门禁会明确失败，不替实现 Agent 补生产代码。

## 2. 语言选择两个 IPC 调用

分别改变“识别语言”和“翻译为”下拉框，均必须调用 `set_langs`，payload 统一为：

```json
{"source": "<source-code>", "target": "<target-code>"}
```

两条路径都要验证：调用次数为 1、字段名一致、另一侧语言取自当前 store、配置 `lang_pair` 同步更新；刷新/重启后 UI 与后端配置一致。

## 3. 翻译降级矩阵

| 场景 | 预期 | 必验结果 |
|---|---|---|
| 所选档位不支持语言对，但存在可用候选 | 自动选择可用候选 | `effective` 与 `substitutedFrom` 可解释 |
| 所选档位不支持，所有候选均不可用 | 不伪造成功 | 原文保留/明确错误，日志说明无候选 |
| 所选档位支持且候选健康 | 不替换 | effective 等于 selected |
| 翻译器创建成功但健康检查失败 | 尝试后续候选 | 失败候选释放，最终结果可观察 |

真实运行时矩阵应在实现合入后用隔离 fake translator/临时模型目录执行；禁止使用用户模型目录和真实配置。

## 安全命令

在 `frontend/` 目录执行：

```bash
node tools/test-acceptance-contracts.mjs
npm run typecheck
```

前者为只读静态契约门禁；两者均不启动 Electron。`npm run test:translate-tiers` 需要已启动的调试 Electron，不属于本次静默门禁，勿在无隔离配置时运行。
