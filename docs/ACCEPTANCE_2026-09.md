# 三项需求验收方案（隔离版）

以下静默门禁不启动 Electron、不连接真实配置、不写用户数据。静态契约与行为测试分层：静态通过只证明结构连接存在，不代表真实桌面端到端验收完成。

## 1. 透明度边界与保存/恢复

- 输入低于 `0.2` 钳制为 `0.2`，高于 `1.0` 钳制为 `1.0`；非法类型/非有限值使用默认值 `0.92`。
- `src/renderer/views/settings.ts` 的 `appearanceTab`：input 预览，change 保存 `overlay_opacity`。
- `src/main/main.ts` / `preload.ts`：`overlay:set-opacity` 接收，`overlay:opacity` 同步。
- `src/renderer/overlay.ts`：读取配置，在 boot/backend ready 恢复，更新 `--overlay-opacity`。
- 行为专项执行真实 TS 函数与控件回调，隔离 DOM/IPC/配置边界；覆盖边界、保存、恢复、CSS、预览前/中/保存后恢复竞态与中英文字。
- 不将这些模拟边界测试等同于真实关闭/重启 Electron 的持久化端到端验证。

## 2. 语言选择两个 IPC 调用

`src/renderer/index.ts` 两个 change 回调取得当前 store 的另一侧语言，交给 `saveLangPair`，再委托真实导入的 `src/renderer/language-selection.ts` 中的 `persistLanguagePair`。

1. `set_langs` 使用 `{"source":"<source-code>","target":"<target-code>"}`。
2. 成功后 `set_config` 写入 `updates.lang_pair`。
3. 队列防止并发更新乱序；前一调用失败不得保存错误配置，且报告失败。

静态契约检查 import、双 change 回调、委托及两种 payload，后端检查同名字段和配置恢复调用。语言行为专项执行真实 persistence 模块，验证顺序与失败语义；它不覆盖真实下拉框操作或重启 UI。该专项已加入 `test:logic`，因此默认 `npm run check` 会执行。

## 3. 翻译降级矩阵

| 场景 | 预期 | 必验结果 |
|---|---|---|
| 所选档位不支持语言对，但存在可用候选 | 自动选择可用候选 | `effective` 与 `substitutedFrom` 可解释 |
| 所选档位不支持，所有候选均不可用 | 不伪造成功 | 原文保留/明确错误，日志说明无候选 |
| 所选档位支持且候选健康 | 不替换 | effective 等于 selected |
| 翻译器创建成功但健康检查失败 | 尝试后续候选 | 失败候选释放，最终结果可观察 |

静态工具只读解析 `voxsub/translate/factory.py` 和 `voxsub/pipeline.py` 的 AST，检查档位支持决策、候选加载、实际支持/可用性检查与兜底调用；不导入这些模块，不加载模型。**静态工具不声称验证了运行时降级矩阵**。隔离运行时回归位于 `tests/test_translate_tier_language.py`、`tests/test_translation_failure_routing.py`，须另按后端测试环境运行，不能以静态 PASS 代替。

## 安全命令

先安装前端已有开发依赖。在 `frontend/` 目录执行：

```bash
npm run test:acceptance-contracts
npm run test:language-selection
npm run test:overlay-opacity
npm run check
git diff --check
```

- `test:acceptance-contracts`：TypeScript AST + Python 标准库 AST，只读结构检查，然后运行内存变异负对照（错误导入、断开回调/委托、错误 payload、注释/字符串伪实现、缺失保存/恢复等必须失败）。没有文件写回或生产源码变异。
- 静态跨语言检查需要 `python`（3.9+ 的 `ast.unparse`）；可用 `PYTHON` 环境变量指定解释器。缺文件、语法错或解释器失败均红灯。路径由 `import.meta.url` 解析，不依赖当前目录或手工剥离 Windows URL。
- 静态检查是明确的当前结构契约，不是完整控制流/可达性证明。结构重构需同步更新契约与负对照，不能添加关键词或注释“修绿”。
- `check` = TypeScript 类型检查 + 调色板检查 + 默认逻辑专项（含语言与透明度）。跨语言静态契约保持独立，避免给默认纯前端逻辑门禁引入 Python 依赖。
- `npm run test:translate-tiers` 需要已启动的调试 Electron，不属于上述静默门禁，勿在无隔离配置时运行。
