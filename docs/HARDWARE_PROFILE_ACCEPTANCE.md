# 硬件型号画像验收

日期：2026-10-05。范围：真实型号采集、诊断硬件卡片、环境诊断快照与公共多行数据组件。用户示例只作格式参考，实际内容来自本机系统；本轮不包含此前日志里的 SenseVoice 下载大小/大陆源问题（该修复方案尚待确认）。

## 改动

- Windows 处理器优先读取注册表产品名及 CIM Win32_Processor.Name，不再用 platform.processor 的 Family/Model/Stepping 架构描述作型号；保留物理核/线程数，读取失败不猜型号。
- 硬件页显示：处理器、主板、每根内存条、全部系统报告的显卡/显示适配器、显示器、磁盘、声卡、网卡、操作系统和 BIOS。保留 NPU 与“可用推理后端（非实际运行设备）”。
- 内存展示装机总容量、每条容量、Manufacturer/PartNumber、DDR 类型、ConfiguredClockSpeed；用 MT/s 表示数据速率，优先当前配置值，只有额定值时明确标称。未知品牌代码不猜品牌。
- 显卡显存使用 nvidia-smi 的专用内存查询，不采用可能截断/不准确的 AdapterRAM；同名但不同容量的 GPU 无法配对时标为未确认，非 NVIDIA 或查询失败也不伪造显存。驱动版本来自系统。
- 显示器型号与厂商/产品码来自 EDID；尺寸由系统提供的物理尺寸计算，标“约 … 英寸（EDID）”，不是营销规格或精确量测。
- 磁盘容量按十进制 GB 四舍五入展示，内存按 GiB 常用装机标法展示为 GB；完整字节值在诊断快照保留。不把 NVMe 驱动报告的 SCSI 接口字段展示为物理接口结论。
- 系统可能同时报告虚拟显示/声卡/网卡；卡片明确提示可能包含虚拟设备，不声称每项都是物理硬件或可以推理。主板只显示实际厂商和产品号，不从 B760 等型号字符串推断芯片组。
- 公共 `buildKeyValueRow` 收编诊断页两处 kv 行，支持安全文本与多行清单；无业务逻辑/IPC，CSS 仅增加公共列表样式。中英文标签同步；真实产品名称不翻译。显示采集时间、加载提示及失败说明。

## 架构与隐私

- 独立核心 `voxsub/hardware_inventory.py`：一次隐藏、只读 PowerShell CIM 批量采集，15 秒限时，各类别独立失败；NVIDIA 容量探针另限 3 秒。
- 状态区分 ok / not_detected / unavailable，损坏成功载荷不当作“没有设备”。每类最多 256 行，驱动清单最多 256 项；60 秒内存缓存、返回深拷贝，无持久化写入。重进硬件页可重新读取（缓存有效期内保留原检查时间）。
- 只选择并输出型号、容量、速率、OS/BIOS/驱动字段。不采集序列号、MAC、用户名、主机名和网卡 ID；显示器 InstanceName 仅在查询进程内匹配尺寸，不输出它或完整 EDID。
- `hardware_profile` IPC 新增可选 inventory 字段，基本字段与旧前端兼容；contracts/commands.json、TypeScript 类型与行为 schema 测试同步。
- 开发者环境诊断快照复用同一采集器，避免第二份型号/设备扫描；仍经既有 safe_snapshot 脱敏。既有 CPU/GPU/NPU 路由和推荐计算未改，只改善 CPU 身份与诊断数据。
- 未改用户配置、日志、历史、模型、安装目录；测试/验收 APPDATA 与 LOCALAPPDATA 独立，未播放/录制音频、加载真实推理模型或下载权重。

## 已验证

1. 硬件画像新 Python 测试（型号与状态、隐私白名单、注册表 CPU、超时/非 Windows、缓存、GPU 容量/同名歧义、IPC schema、环境快照）19 项，连同架构与诊断专项 **58 passed**，1.79 秒。
2. 新生产 UI/格式化/公共组件测试 **13 项通过**：每条内存与多设备、未知参数、中文/英文、legacy 载荷、文本注入安全、诊断真实视图、销毁后的迟到响应隔离；加入完整 npm check。
3. 最终完整前端 `npm run check`、`npm run build` 通过。
4. 最终隔离 Python 安全回归：**1122 passed, 6 skipped, 17 deselected, 1 xfailed**，69 秒。6 项跳过为缺少真实模型与 Windows 符号链接权限；17 项排除硬件音频/集成标记。既有安装器版本宏问题 xfail 未在本轮扩改；复杂度/依赖方向门禁未放宽。
5. 最新 dist 的真实 Electron + 真实 Python IPC + 本机 CIM/注册表/nvidia-smi **7 项通过**：CPU 产品名、11 类状态、无敏感标识、各类实际型号完整渲染、推理能力与设备存在区分、窄窗不溢出。不是用用户示例或模拟数据替代真机。
6. 实测产品名：`13th Gen Intel Core i5-13600KF`（14 核 / 20 线程）；`MAG B760M MORTAR WIFI II (MS-7E13)`；两根 Gloway `VGM5UH60C30AG-DTACWM`，各 16 GB DDR5、6000 MT/s；NVIDIA RTX 4060 专用内存约 8 GB；`PHL24M1N3200Z`，EDID 约 24 英寸。四个命名磁盘与声卡/网卡均在卡片；另有系统报告的虚拟设备、无容量 USB 设备，未隐藏或伪造。
7. 宽/窄布局用实际 DOM 和最新 CSS 离屏渲染检查；多行数据换行、不横向溢出。窄屏内容更长，真实页面可滚动；离屏窄截图固定高度不是全部条目的完整截图，完整数据另有 DOM/JSON 与宽图。
8. 最后一轮真实运行 197 次、离屏渲染 16 次窗口监测，合计 **213 次**：0 可见窗口/自有窗口聚焦，正常退出，无验收自有进程残留。全程静默，没有在桌面展示截图或打开音频设备。

证据目录：`D:\OneDrive\app_dve\hardware-profile-20261005`。

- frontend-delivery-check.log、frontend-delivery-build.log
- python-final-regression.log、focused-delivery.log
- real-profile.json、real-acceptance.json/log、real-monitor.json、real-delivery-runner.log
- hardware-page.json、hardware-wide/narrow.png、offscreen-delivery-monitor.json
- before-display-review 保存前一轮真实采集与布局证据；首轮 UI 测试未初始化测试后端 ready，已修正测试接线后通过所有最终门禁，不作为产品缺陷。
- 最终 commit、源码/dist SHA256 和干净状态见仓库外 DELIVERY.json。

## NOT_RUN / 限制

- 不同主板/OEM 品牌、AMD/Intel 显存专用查询、无 EDID/远程显示器、Windows 10 和非 Windows 实机；模拟了缺失与失败，不等于这些硬件都做过实测。
- 系统未提供的芯片组、品牌、SPD 参数/营销尺寸不能保证显示；DDR 数据速率和显示器尺寸是系统报告值，不是硬件性能实测。
- 全部驱动/设备数量超过 256 的性能与完整性、设备热插拔长期行为、可见桌面实际交互、实际模型加载/推理、音频、安装器/安装版。
- 缓存/时间戳用于减少阻塞，不构成硬件实时监控；设备检测不证明当前模型实际运行在该设备。

## 备份与回滚

初始备份：`D:\OneDrive\app_dve\VoxSub\.backups\hardware-models-20261005-150032`（源码/契约/文档/dist + RESTORE.md）；review/display-review 子目录保留本轮修补前版本。

首选 git revert 本轮交付提交后重建；也可按 RESTORE.md 同路径恢复并移除仅本轮新增文件后重建。不要删除/恢复用户模型、配置、日志或历史。本轮仅本地提交，未 push、打包或更新安装版。
