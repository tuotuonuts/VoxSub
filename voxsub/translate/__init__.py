"""voxsub.translate —— 翻译层 (M4)。

三档实现 + 缓存/预取/工厂, 契约见 DESIGN.md「翻译层契约（M4）」:

- base.OpusFastTranslator      快档: ORT 手写 greedy seq2seq 跑 Xenova OPUS-MT int8
- qwen.QwenQualityTranslator  兼容类名: llama-server 加载模型广场所选 Hy-MT2 GGUF
- cloud.CloudTranslator       云: OpenAI 兼容端点 (用户自配 key)
- cache.TranslationCache      LRU 缓存 (key=(norm_text,src,dst))
- prefetch.PrefetchEngine      ASR 碎片预取 + 防抖合并
- factory.TranslatorFactory    按档位创建 / 探测可用性
"""
