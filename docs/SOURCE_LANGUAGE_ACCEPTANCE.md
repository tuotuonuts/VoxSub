# 识别语言提示与非破坏性源文保留修复

记录时间：2026-10-05T19:23:36.794937+00:00；基础提交bf7b52d。源码修复和本地验收，未打包发布、未push、未重启用户应用。

## 日志与根因

读取用户指定的本机voxsub.log，未复制或上传日志正文。最新run=9e6384de2365（UTC 2026-10-05T18:30:38.073+00:00 至 2026-10-05T18:54:20.311+00:00）记录模型asr-qwen3-0.6b-int8、source=en、target=zh。92条识别结果中69条进入正常路径，23条被语言门禁拦截，另有23次“已忽略当前片段”状态。不能据此称这23段全是外语：旧门禁仅依赖字符系统和约40个英文常见词。

- Qwen适配器虽然保存source_lang，却未给实际OfflineStream设置language选项。本机sherpa-onnx=1.13.5，底层读取的是stream.GetOption("language")，而非from_qwen3_asr构造参数。英文名English用于预填language前缀；hotwords不是设置语言的替代接口。
- 英语判定用常见词交集，Thanks/Okay/Good morning/NASA会被误拒绝；用户给的句子含单独的i，反而符合旧英语判定。拉丁字母不能证明是英语，短文本更不适合做硬门禁。
- 实时partial、终句、上下文提交、最终翻译、草稿翻译及本地/云文件识别有多处重复源语言拦截，仅改一个入口仍会丢字幕。
- 音频已有入队语言快照，但本地创建stream以及识别完的_on_sentence没有贯通快照，切语言时可能把旧音频/结果按新配置处理。
- 该run还出现2条低音量提示；没有原始视频音频，不能从这些元数据确定用户给定句子的具体误识别成因，也不能从日志恢复被忽略的正文。

## 已实施

1. OfflineGenerativeASR每条stream保存源语言；Qwen创建原生stream后、送入音频/解码前设置language=English/Chinese/Japanese/Korean，auto不设置。通过create_asr_stream复用入口连接实时和文件识别；不修改全局配置，不重复加载模型。
2. 本地/云识别输出沿用音频入队快照；日志也记录该快照的源语言，防止排队中切语言改变旧句。无快照的流式回调仍在提交点绑定配置。
3. 源语言选择是识别指令，而非允许用户看到哪些内容的过滤器。所有非空源文（短词、数字、专名、混合文字、疑似别的语言）继续进入草稿/上下文/翻译链；删除上述源文硬门禁，统一只做空白整理。
4. 翻译仍按用户选择的语言对尝试，不自动改模型/切云/改配置。译文目标验证和失败时原文+空译文的回退保持，不能把原文冒充翻译成功。能力不足/模型失败不伪造结果。
5. 移除微型英文词表。text_matches_language仅作字符系统的可接受性检查，不声称可区分英语与瑞典语；目标验证也不再错误拒绝正常英文短词。
6. 元数据追踪增加recognition_language的hint_applied/auto/hint_unavailable；API不支持时明确记录fallback=unconstrained_decode并保留可识别结果，不静默失效。hint_applied证明参数交付，不证明每个词都正确。
7. 源文脚本不匹配标为language_uncertain、fallback=source_retained，不计作翻译失败；状态提示改为“识别语言可能与设置不同，已保留内容并继续翻译”，每配置代次/会话限一次，避免刷屏。日志不存识别/翻译正文。

没有给每句话增加二次识别或第二个模型。没有增加将已识别外语文字再改写成英文以伪装合规的后处理，也不承诺消灭模型幻觉；难音频仍可能听错，但不再凭语言猜测删除已经得到的信息。

## 验收

- 原始新增30条回归修复前28 failed/2 passed；覆盖短词、数字、混合脚本、用户样例、Qwen原生选项、local/cloud文件、排队配置。修复后扩展到34项全部通过，另补元数据不泄露/源文不确定非失败测试。
- 语言/流水线/快照/文件/诊断/延迟/架构定向套件：195 passed / 2 skipped。队列测试稳定性＋架构＋新测试复核68 passed。
- 最终安全Python全量：1251 passed / 8 skipped / 7 deselected / 1既有xfail，77.36秒。显式-m "not hardware_audio"、清除Python路径注入、隔离APPDATA/LOCALAPPDATA、隐藏子进程；无实际录音/播放/桌面操作。
- npm run check、npm run build通过。git diff --check通过。
- 真实已安装Qwen3-ASR 0.6B INT8＋sherpa1.13.5，CPU单线程，5组公开包内WAV片段，每组6秒：auto对照、English、English+15%幅度、噪声英语、多语混说。4个English请求均在native decode边界读回English，auto无language选项；5/5返回非空拉丁文字结果，每段仅一次推理。decode约3.19–4.05秒；这是单线程样本观测，不是实时端到端速度结论。
- 不加载用户翻译服务，不碰用户音频；真实测试仅读取安装包test_wavs，来源由其README标记为Qwen官方demo。脚本scripts/verify_qwen_language_hint.py已保留，报告只记录样本哈希/选项/字符数/耗时，不记录转写内容。

### 验收过程中发现并解决

初次全量指出_decode_recognition_audio分支分数超预算：合并本地/云重复语言快照选择，没有提高复杂度阈值。随后一次全量暴露既有队列测试竞态：has_active_jobs同时包含queued/running，不能保证first已出队；MAX_PENDING仅计待执行。用entered事件同步实际执行，再填满全部待执行槽，finally释放worker；只改测试，不改生产队列容量/逻辑。最终全量通过。

原生验证脚本最初使用环境未安装的scipy重采样，未装新依赖，改为复用项目现有resample_16k后完整复验通过。

## NOT_RUN / 边界

用户原视频/该句对应音频、WER/语义准确率、真正外语与识别幻觉的自动区分、物理音频采集、前台GUI/悬浮窗渲染、真实Qwen翻译服务完整端到端、其它ASR型号的实机推理、GPU、安装版、GitHub CI。其它模型原先的配置/语言提示方式不变；本轮真实原生语言选项验收仅涵盖Qwen。源文为空或识别服务真正失败仍按原错误路径处理，不承诺恢复本来就没有识别出的内容。

## 依据与证据

已核对本机Python签名/OfflineStream API以及同版本上游实现（不是凭最新master猜API）：

```text
https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/v1.13.5/sherpa-onnx/csrc/offline-recognizer-qwen3-asr-impl.cc
https://github.com/k2-fsa/sherpa-onnx/pull/3472
```

外部证据目录：`D:\OneDrive\app_dve\source-language-acceptance`（before.log、after-focused.log、targeted-final.log、review-check.log、python-final.log、frontend-check.log、frontend-build.log、native-language.json/log）。汇总与哈希见SOURCE_LANGUAGE_ACCEPTANCE_EVIDENCE.json。旧的全量失败记录不算通过，最终门禁以上述python-final为准。

## 回滚与生效

修改前备份`.backups/source-language-20261006-030347`（UTC+08本地日期）；队列测试首次修改前也已追加备份。优先git revert本轮提交；手工恢复需还原备份中的既有文件，并仅撤销本轮新增测试、验证脚本和报告。不要触碰既有Cache/。

已构建前端；用户需自行重启源码版/新构建，正在运行的旧Python进程不会热更新。没有改用户配置、历史或模型文件，没有自动push。
