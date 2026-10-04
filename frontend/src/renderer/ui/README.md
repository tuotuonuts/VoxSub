# 公共 UI 组件

## 使用约定（用户要求）

新增 UI 元素前先查本目录。已有组件优先复用；尚无对应组件时，优先把通用结构及基础交互做成公共组件，再由页面组合。不要将页面业务流程硬塞进组件，也不要为了某个特例引入任意参数大杂烩。

- 公共组件不读 store、不调用 IPC、不保存配置、不触发下载或确认框。
- 用户可见文案在调用页面经 `tr()` 翻译后传入；数据内容不改写。
- 页面保留业务动作、异步结果失效保护和 dispose。组件本身不注册全局监听器或定时器。
- 扩展组件或抽取重复结构时补真实组件 DOM/事件测试，不只断言源码中存在组件名。
- CSS 类名及节点语义尽量保持兼容；不为抽取组件改变视觉样式。

## 清单

| 文件 | 接口 | 边界 / 使用场景 |
|---|---|---|
| `button.ts` | `buildButton` | 标准 ghost/primary 按钮，small/block、disabled/hidden/title；页面绑定业务事件 |
| `button.ts` | `buildFilterChip` | 紧凑筛选/切换按钮，初始激活样式与 aria-pressed；页面同步后续选择状态 |
| `card.ts` | `buildCard` / `buildCardFrame` | 标题与正文卡片；Frame 返回 body 引用供增量填充，保留 section/div 语义 |
| `controls.ts` | `buildTextInput` | 文本/密码输入，change 提交值；实时 input 事件由调用方绑定 |
| `controls.ts` | `buildSelect` | 有类型的值/标签列表及初始选择，change 回调可省略 |
| `controls.ts` | `buildRadioGroup` | 单选项互斥、aria-checked、可选说明徽标；徽标不自动禁用选项 |
| `controls.ts` | `buildToggleSwitch` | label + checkbox + track + label；支持页面生命周期管理监听 |
| `tab-nav.ts` | `buildTabNav` | 设置式分页导航，独立激活状态与 aria-selected；页面管理面板生命周期 |
| `shortcut-field.ts` | `buildShortcutField` | 可编辑/可录入的组合键字段；挂起/恢复注册接口由页面注入，组件不直接发 IPC，支持取消/异步失效/清空和状态说明 |
| `field.ts` | `buildField` | 标签、控件、说明和禁用原因 |
| `path-picker.ts` | `buildPathPicker` | 路径字段；选择取消不提交，按钮复用公共组件 |
| `progress.ts` | `buildProgressBar` | 进度展示 |
| `status-row.ts` | `buildStatusRow` | 统一检测结果行 |

## 本轮收编范围

设置、诊断、开发者页、日志容量、模型广场、OCR、字幕工作区、迁移向导、主导航和路径选择字段已使用这些公共基础元素。设置页原来的 5 个私有构造器已迁出（输入、下拉、单选、开关、卡片），两处相同分页导航已合并。

未强行抽象：模型卡片 cell、迁移向导判定/步骤、字幕流条目、OCR 预览和覆盖层等业务结构；原生范围滑杆、数字输入以及动态重建的模型/语言选择仍由各自页面编排，不能机械地把所有 `h()` 都替换为包装函数。后续新增对应通用元素仍遵循组件优先约定。

验证：`npm run test:shared-ui`（真实模块 + MiniDOM），已纳入 `npm run check`；页面生命周期、诊断、模型广场竞态与主页提示由既有生产 DOM 测试继续覆盖。
