# OCR 前后端修复与静默验收

## 摘要

本轮用户授权自动诊断、修复及深入优化；不擅自 push。基础版本 `35512f3`。
备份：`.backups/ocr-deep-20261006-005502`，后续增加快捷键测试原件；逐文件恢复对应备份可回滚，勿把整个备份覆盖到仍有用户修改的工作树。
原始日志/合成产物：`D:/OneDrive/app_dve/ocr-deep-acceptance`。最终机器可读结果参见同目录 `verified/result.json`；仓库保留摘要及SHA256证据。

## 根因与验证

| 根因 | 修复 / 证据 |
|---|---|
| main 发 live-frame，缺少识别/译文接收链 | 生产 LiveOcrSession -> ocr_recognize -> ocr:translated，真实调度器隔离测试 |
| handler 忽略所选模型、置信度 | OcrSession按配置创建引擎，替换释放，不擅自切Tiny |
| OCR像素当DIP；thumbnail请求尺寸当实际尺寸 | shared geometry + capture实际尺寸；125/150/200%逻辑映射测试，native仍NOT_RUN |
| 主屏限定、源错配回退第一屏 | 虚拟桌面、副屏/负坐标、最大交集单屏裁切；来源不匹配失败 |
| 同页旧请求覆盖、导出期间换图 | 请求代号/生命周期/固定导出源，真实workspace miniDOM回归 |
| stop重置忙锁、迟到任务串新会话、帧文件堆积 | 不重置在途锁、epoch隔离、finally删帧、stop释放runtime |
| 空/失败帧旧译文永不消失 | 2500ms截止；失败不续截止，计时器测试 |
| EXIF方向、长译文越框、非法框变边缘1px | EXIF统一、独立tile/缩字/换行/省略、非法/越界框拒绝 |
| 缓存根目录未注入ConfigStore | 指定根目录与上限回归；真实独立缓存limit=2验证 |
| D语言中英日韩硬编码 | OCR输入能力+当前MT方向，Tiny中英、Small/Medium中英日、未知fail closed |
| 旧OCR MT缓存未跟配置失效 | 配置signature；新会话独立拥有MT并回收 |
| 过滤行混同失败、无译文伪完成/伪导出 | failedLines与untranslatedLines区分，空译文仅预览原图；只识别真空串 |
| 新IPC字段未列契约、overlay加载失败未处理 | 同步严格契约；加载失败销毁并停止，轮询等ready；fixture回归 |

## 静默纪律

最终后端全量通过仓库外安全包装执行，清除PYTHONPATH/PYTHONHOME，隔离APPDATA/LOCALAPPDATA；`pytest -q -m "not hardware_audio"`，真实子进程CREATE_NO_WINDOW。前端NODE_OPTIONS仅给真实spawn/spawnSync加windowsHide，未改测试断言/模型结果。
前端检查是生产TS逻辑/VM/miniDOM/假Electron，不启动原生Electron，不截图用户桌面，不访问真实音频，不播放声音。真实模型验收仅生成合成图；CPU两线程/已装权重，无模型下载、无云请求，不重启/操控用户应用。

### 候选验收落盘偏差（必须披露）

早期候选脚本的译后图缓存入口漏传ConfigStore，数张合成译后图落入仓库 `Cache/OCR/translated`，而非指定隔离缓存；limit=2的prune也在该目录执行。运行前未记录原目录清单，**无法证明旧缓存未受淘汰影响，也没有依据认定实际删了用户缓存**。已修入口、加回归，并用指定缓存完成verified验收。当前两张该候选合成图保留，未主动清理此目录，且不提交。候选失败日志保留，不计最终PASS。不能宣称本轮所有测试都未接触原缓存。

## PASS：最终自动化

- Python全量：**1216 passed / 8 skipped / 7 deselected / 1既有xfail，80.46秒**，0失败；不是硬件音频验收。
- 前端：`npm run check` + `npm run build`；包含OCR runtime **21项**（真实生产scheduler/workspace/overlay逻辑）、capture **5项**（真实capture模块与隔离Electron/filesystem fixture）。
- 旧快捷键/退出/迁移/窗口测试只补新模块与Backend.command fixture，原断言保留，不放宽门禁。
- `git diff --check`通过；严格IPC契约校验包含实模型handler参数/返回值。

## PASS：真实已装离线模型

工具：`scripts/ocr_silent_acceptance.py`；RapidOCR v6 Small + OPUS en-zh，实际CPU推理。固定1000x240合成两行英文；真实BackendService.handle与ContractRegistry校验，**in-process，不是生产stdio进程或完整桌面app**。

| 场景 | 识别/译文行 | wall ms | 本次新翻译请求 | 结论 |
|---|---|---:|---:|---|
| static | 2 / 2 | 2615.41 | 2 | PASS |
| live-first | 2 / 2 | 3265.49 | 0 | PASS，命中文字缓存 |
| live-identical | 2 / 2 | 2.50 | 0 | PASS，unchanged精确帧复用 |
| live-one-pixel-change | 2 / 2 | 2907.11 | 0 | PASS，必须重识别，文字未变复用译文 |
| live-empty | 0 / 0 | 2471.42 | 0 | PASS |
| recognition-only | 2 / 0 | 3161.44 | 0 | PASS，未调用MT |
| live-after-release | 2 / 2 | 3872.64 | 2 | PASS，新runtime重新加载 |

另：真实导出两块，尺寸1000x240保持，像素确有变化；配置指定managed缓存连续3次导出、limit=2生效。
相同帧返回中ocr_ms保留上一帧数据，**2.50ms是缓存wall，不是新OCR推理速度**。不据两行样本宣称准确率、全语种通过或GPU使用。实模型证据在最终UI收尾前采集；后续修改只涉及UI错误处理/文案、capture加载生命周期，不变动实测推理与导出成功路径。

## NOT_RUN / 未解决的性能边界

- 原生Electron绘制/真实框选、混合DPI多屏、物理屏幕捕获排除、全屏游戏/远程桌面：NOT_RUN，mock不计桌面验收。
- 生产stdio完整进程、用户当前配置组合、云MT、Tiny/Medium/v5真实权重、GPU/NPU、跨语种准确率、长时间视频端到端：NOT_RUN。
- 没做跨屏混合DPI拼接；跨屏选区裁到单屏。不做旋转框/竖排/复杂版式还原，相交块保守跳过，可能少显示。
- 新画面CPU仍约2.5–3.9秒；动态背景会削弱精确像素缓存命中，未承诺实时视频低延迟。慢设备2500ms截止可能导致间歇显示。
- 本轮未安装/打包/发布/自动push，GitHub CI：NOT_RUN。

详见 `OCR_RESEARCH_AND_DESIGN.md` 的后续门槛。提交源码不是宣布上述NOT_RUN全部通过。
