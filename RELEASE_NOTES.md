# 语幕 VoxSub — 发布说明 (RELEASE_NOTES)

## 版本: v0.1.0-beta (2026-08-17)

第一个端到端可运行内测版：三模式实时/离线翻译已全部打通真实模型链路。

### 功能
- **A 模式 · 麦克风同传**：实时识别说话 → 双语字幕（原文+译文），可选 TTS 朗读
- **B 模式 · 系统声音字幕**：抓取会议/网课/视频播放的系统声音 → 实时字幕
- **C 模式 · 文件字幕**：导入音频/视频 → 分句识别 → 翻译 → 导出 .srt/.vtt/.txt
- **双模翻译**：快档 OPUS-MT（~0.5s/句，离线）/ 质量档 Qwen-1.5B（~0.7s/句，离线，混合语言更准）/ 云端 API（可选，用户填 key）
- **全本地运行**：不联网可用；无输入音频落盘（隐私优先）
- **硬件自适应**：DirectML 自动用 GPU；有 NPU 的机器可路由
- **诊断中心**：设备清单、自检 6 项、**实时日志页签**（首版 debug 关键）
- **UI**：Soft Premium 风格，三档主题（浅/深/跟随系统），托盘常驻

### 性能 (i5-13600KF / RTX 4060, CPU 推理)
- 识别：流式，边说边出
- 快档翻译 573ms/句 · 质量档 690ms/句（均 <1s）

### 安装
- 运行 `VoxSub-Setup.exe`（开发中：当前提供 onedir 目录版 `dist\VoxSub\`）
- **模型需单独获取**（安装包不含 2.4GB 模型）：首次运行在"诊断页/设置"点下载，自动走 ModelScope 主源 + 断点续传 + SHA256 校验
- 需要微软 VC++ 运行库（安装器自动装）

### 已知问题 / 限制
1. **英文名 VoxSub 在 GitHub 有同名同类项目**（待用户确认保留/改名，见 STATUS.md）
2. self-signed 版 SmartScreen 会提示"已保护你的电脑"→ 点"更多信息"→"仍要运行"；正式版将用 OV 证书签名
3. B 模式 loopback 依赖声卡兼容，个别虚拟声卡（如视频会议软件自带的）可能拿不到流——诊断页会提示换源
4. 非 wav 文件模式依赖 ffmpeg（随包未含，需用户 PATH 有 ffmpeg，后续内嵌）
5. 快档 OPUS 对中英混合句翻译质量有限（会灌音乐符号）——混合语言场景请用质量档
6. 语言对当前固定中⇄英，多语言（马来语等）在 roadmap

### SHA256（发布物校验）
> 安装包/主程序 SHA256 由 build 后 `sign.ps1 verify` / Get-FileHash 生成，
> 随正式发布物附上，用户可用 `certutil -hashfile <file> SHA256` 自校验。

### 发布物（正式版统一输出到 D:\OneDrive\app_dve\Release，用户约定）

> 正式版安装包/发布物编译到 `D:\OneDrive\app_dve\Release`（2026-08-17 用户指定）。
> 每个正式版 = 安装包 + SHA256 + 签名 + 本文件更新。

**内测版安装包 (dist\VoxSub-Setup.exe, v0.1.0-beta, 92.8MB)：**
```
SHA256: 4EF7914448394C54AFFA11E55F0D3721AEC980B60266D65FE563BE7875DEAC6B
签名  : VoxSub Dev (self-signed) + DigiCert RFC3161 时间戳 (2026-08-17)
```

**内测版主程序 (dist\VoxSub\VoxSub.exe, v0.1.0-beta, 290MB onedir)：**
```
SHA256: BB21B365F319A551FE6D77A46155ACB195ADE37CDA7AFCF8AA4AF334C0D8279D
签名  : VoxSub Dev (self-signed) + DigiCert RFC3161 时间戳 (2026-08-17 重建)
```

### Roadmap
- M9 正式发布：InnoSetup 安装包 + OV 证书签名 + 微软商店上架（$19/年）
- 语言对扩展（马来语等）· 多显示器字幕位置记忆 · 字幕时间轴微调
- 开机自启（托盘）· 热词表（人名/术语优先识别）
