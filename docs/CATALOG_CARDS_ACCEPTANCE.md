# 模型广场卡片验收

## 卡片呈现

1. 左上角不再显示模型 ID 后缀。四档可用评级为 **基础款（灰）/ 推荐（绿）/ 中高负载（橙）/ 高负载（红）**；配置达不到要求单独显示灰色 **配置不足**，探测失败显示 **待评估**，不伪造“推荐”。
2. 保留原有右上角任务标签、标题与模型大小；英文模式只翻译任务文案，不改模型名称。
3. 21 个模型的描述改成普通人能理解的用途、适用场景和必要限制，去掉量化/导出/运行库/验证记录等实现术语。Moonshine 明确英语、轻量、整句出结果、不支持中文。
4. 保留五个能力点，加小字 **能力**；悬停与无障碍说明包含 `N/5` 和“同类模型能力参考，不是运行速度”。沿用现有目录 quality_score，不冒称统一基准测试或实测准确率。
5. 语言标签保留，运行库和许可证代码不再占用卡片标签位；改为响应快、短句识别、离线使用、专业术语等用途标签。运行时/许可证仍保留在结构化模型元数据，不改模型加载契约。
6. 底部硬件标记改成官方仓库图标入口：GitHub 猫形标志/Hugging Face 拥抱表情，图标全部本地渲染，无图标联网请求。21 个模型映射到 11 个独立官方 GitHub 仓库，仓库 URL HTTP 200；NeMo、Fun-ASR、SenseVoice 使用实际重定向后的官方目标。公共组件也覆盖 Hugging Face 回退。
7. 已下载改为整圈细描边 + `✓ 已下载` 小徽标，保留使用/卸载/使用中和下载进度。

## 推荐逻辑与工程边界

- 模型列表调用一次当前进程硬件探测，复用已有 CPU 核数、内存、加速能力和模型资源估算。评级是 **基于本机配置的估算，不是当前利用率或推理实测**。
- 总内存达不到最低要求或预计负载超过 110% → 配置不足；预计负载 85%–110% → 高负载；50%–84% → 中高负载；负载较低但同任务可运行的替代模型能力明显更高 → 基础款；其余 → 推荐。
- 某个模型评估错误只将这一项标为待评估；硬件探测失败不隐藏全部模型，也不影响文件存在状态。诊断错误仍写入 sidecar 日志。
- 推荐、文件扫描、序列化拆成独立函数，架构复杂度门禁通过，未放宽阈值。新增 `official_repo` 追加到 ModelSpec 字段末尾，保持原有位置参数顺序。
- 新增公共 `buildBadge` / `buildRating` / `buildRepositoryLink`。组件不读 store、不调用 IPC、不接配置；原私有 scorePips 已收编到评分组件，无遗留重复构造器。
- 仓库链接只允许 HTTPS 的 GitHub/Hugging Face 仓库根路径，拒绝镜像/下载资源/凭证/危险协议/伪造域名；打开动作经现有主进程外部链接接口，不让应用窗口导航到外网。
- 源码新增橙色主题令牌；浅深色文字对比度 ≥4.5:1。下载海外/大陆/自动切源逻辑、模型文件清单、校验信息、选择模型行为未改。

## 已验证 PASS

- `npm --prefix frontend run check`：完整门禁通过；新增卡片公共组件/生产 DOM/评级状态/安全链接/中英文/已下载行为 **20 项**，既有 catalog 竞态/三种下载源回归保留。
- `npm --prefix frontend run build`：最终源码构建通过。
- Python 安全套件：**1087 passed / 4 skipped / 17 deselected / 1 xfailed / 1 warning**。新增卡片后端 **13 项**；warning 为已有 tarfile DeprecationWarning。
- 隐藏真实 Electron + Python sidecar：**10 项**。真实设备推荐元数据、21 个模型内容、原名/大小不变、能力点、官方图标、已下载、760px 窄窗无横向溢出、英文卡片通过。
- 可视检查采用 **隐藏应用真实 DOM/主题样式导出，再以 Electron offscreen 渲染**。浅色、深色、英文三张图已检查，不是显示用户桌面后截图。直接对从未显示的窗口使用 CDP captureScreenshot 的初次尝试等待超时，已正常退出；此后改成不显示窗口的 offscreen 路径并复验通过。
- 最终真实运行监测 **114 次** + offscreen **28 次**，共 **142 次**，未发现本轮窗口显示或焦点抢占；退出码 0、本轮进程无残留。此前失败的验收脚本尝试也正常清理，无聚焦/显示违规。

证据（仓库外）：`D:\OneDrive\app_dve\catalog-cards-acceptance\checks.json`、`silent-window-delivery.json`、`offscreen-delivery-monitor.json`、`real-ui-acceptance-delivery.log`，以及 `moonshine-light.png` / `moonshine-dark.png` / `moonshine-english.png`。最终门禁日志在父目录 `catalog-cards-delivery-check.log` / `catalog-cards-delivery-build.log` / `catalog-cards-delivery-pytest.log`。

## 已知既有问题与未验边界

- **既有设置页主题即时刷新问题**：`applyThemeChoice()` 只更新 store/dataset，主页面增量刷新没有应用 palette，因此设置页切换浅色可能不立即改变颜色。本轮主题检查使用已有主页面“主题”按钮（它显式应用 palette）。已定位记录，未扩大范围修改。
- 已下载样例仅在隔离目录放置文件存在性 UI 占位文件，没有真实权重；不能据此称 Moonshine 已下载、校验或推理验收通过。此项只验证 `installed` 状态的卡片展示。
- 没有启动浏览器窗口验证外部链接交互（生产组件点击回调和官方 URL 已验），没有下载/卸载/使用模型、音频采集或播放、实际模型推理及性能基准。
- 没有改安装版或打包。推荐不代表任意当前负载下都能实时运行，也不是某一加速设备正在推理的证据。

## 备份与交付

修改前源码与本地 dist 备份：`.backups/catalog-cards-20261005-053519/`，回滚说明在 `RESTORE.md`。将源码按原相对路径恢复，`dist/` 恢复到 `frontend/dist/`；新增文件根据本轮 commit 清单处理，或对该 commit 执行 Git revert。不要恢复或修改用户模型、设置和历史。

本轮本地 commit；**未推送 GitHub、未打包、未发布**。最终提交与工作区状态记录于仓库外 `catalog-cards-acceptance/DELIVERY.json`。
