"""Index-Echo adapter derived from IndexTeam/Index-Echo-S2TT-2B infer.py.
Upstream revision bc45ecb31f3fcd78ec0340851fe21c71458d4a75, Apache-2.0.
Only model forward/generation is included; no upstream CLI, shell or downloader.
"""
import json
import os
import torch
import torch.nn as nn
HERE = os.path.dirname(__file__)
AUDIO_PAD, AUDIO_START, AUDIO_END = '<|audio_pad|>', '<|audio_start|>', '<|audio_end|>'
CTX_MARK, GLOSS_MARK = '[Context]', '[Glossary]'
INSTR = {lang: f'For each sentence, output three lines: the [MM:SS.CC-MM:SS.CC] timestamp, the transcript, then the {name} translation.' for lang, name in (('en', 'English'), ('ja', 'Japanese'), ('es', 'Spanish'))}
class AudioConnector(nn.Module):
    """h' = exp(log_alpha) · (h + beta · W2 σ(W1 h))"""
    def __init__(self, dim):
        super().__init__()
        self.log_alpha = nn.Parameter(torch.zeros(()))
        self.w1 = nn.Linear(dim, dim, bias=False)
        self.w2 = nn.Linear(dim, dim, bias=False)
        self.beta = nn.Parameter(torch.zeros(()))

    def forward(self, h):
        return torch.exp(self.log_alpha) * (h + self.beta * self.w2(torch.nn.functional.gelu(self.w1(h))))


class AudioTransModel:
    def __init__(self, root=HERE, device='cuda:0', dtype=torch.bfloat16):
        from safetensors.torch import load_file
        from transformers import AutoModelForCausalLM, AutoTokenizer, WhisperFeatureExtractor
        from transformers.models.qwen3_omni_moe.modeling_qwen3_omni_moe import (
            Qwen3OmniMoeAudioEncoder, Qwen3OmniMoeAudioEncoderConfig)
        self.device, self.dtype = device, dtype
        llm_dir = os.path.join(root, 'llm')
        self.tok = AutoTokenizer.from_pretrained(llm_dir, local_files_only=True)
        self.llm = AutoModelForCausalLM.from_pretrained(llm_dir, dtype=dtype, local_files_only=True).eval().to(device)
        with open(os.path.join(root, 'audio_config.json'), encoding='utf8') as stream:
            cfg = Qwen3OmniMoeAudioEncoderConfig(**json.load(stream))
        self.tower = Qwen3OmniMoeAudioEncoder(cfg).eval()
        self.tower.load_state_dict(load_file(os.path.join(root, 'audio_tower.safetensors')), strict=True)
        self.tower = self.tower.to(device, dtype)
        self.connector = AudioConnector(self.llm.config.hidden_size)
        self.connector.load_state_dict(load_file(os.path.join(root, 'connector.safetensors')), strict=True)
        self.connector = self.connector.to(device, dtype)
        self.fe = WhisperFeatureExtractor(feature_size=128, hop_length=160, n_fft=400, sampling_rate=16000,
                                          padding_value=0.0, return_attention_mask=True)
        self.fe.n_samples, self.fe.nb_max_frames = 4800000, 30000  # 默认 30s 会截断, 放开到 300s
        self.pad_id = self.tok.convert_tokens_to_ids(AUDIO_PAD)
        self.im_end = self.tok.convert_tokens_to_ids('<|im_end|>')

    @torch.inference_mode()
    def encode_audio(self, wav16k_path):
        import librosa
        wav, _ = librosa.load(wav16k_path, sr=16000)
        f = self.fe(wav, sampling_rate=16000, return_tensors='pt', return_attention_mask=True)
        T = int(f.attention_mask.sum(-1)[0])
        feats = f.input_features[0][:, :T].to(self.device, self.dtype)
        out = self.tower(input_features=feats, feature_lens=torch.tensor([T], device=self.device))
        h = out.last_hidden_state if hasattr(out, 'last_hidden_state') else out[0]  # (T', 2048), 无 batch 维
        return self.connector(h.to(self.dtype))

    @torch.inference_mode()
    def translate_window(self, wav16k_path, ctx_parts, terms=(), lang='en', max_new_tokens=2000, temperature=0.0):
        """ctx_parts: 前几窗的 'zh\\ntgt\\nzh\\ntgt…' 文本列表(可空); terms: parse_glossary 产物; lang: en|ja|es。
        返回 (模型原始输出字符串, 实际喂入的 [Context] 文本)。"""
        emb = self.encode_audio(wav16k_path)
        ctx = '\n'.join(p for p in ctx_parts if p)  # 与训练 render_v6_full.ctx_block 一致(逐行平排, 无窗分割线)
        slots = []
        if ctx:
            slots.append(f'{CTX_MARK}\n{ctx}')
        if terms:
            slots.append(f'{GLOSS_MARK}\n' + '\n'.join(terms))
        audio_block = AUDIO_START + AUDIO_PAD * emb.shape[0] + AUDIO_END
        content = f'{audio_block}\n' + ''.join(s + '\n\n' for s in slots) + INSTR[lang]
        # 训练时 assistant 带空 think 骨架, 推理 prefill 之以对齐分布
        prompt = f'<|im_start|>user\n{content}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'
        ids = self.tok(prompt, return_tensors='pt').input_ids.to(self.device)
        x = self.llm.get_input_embeddings()(ids).clone()
        mask = ids == self.pad_id
        assert int(mask.sum()) == emb.shape[0]
        x[mask] = emb.to(x.dtype)
        out = self.llm.generate(inputs_embeds=x, attention_mask=torch.ones_like(ids), max_new_tokens=max_new_tokens,
                                do_sample=temperature > 0, temperature=temperature if temperature > 0 else None,
                                eos_token_id=[self.tok.eos_token_id, self.im_end],
                                pad_token_id=self.tok.eos_token_id)
        if out.shape[-1] >= max_new_tokens:
            raise ValueError('模型输出达到长度上限；未导出截断字幕')
        return self.tok.decode(out[0], skip_special_tokens=True).strip(), ctx
