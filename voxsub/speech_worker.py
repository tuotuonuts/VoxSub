"""Offline, killable single-model worker. Stdout is a bounded NDJSON protocol."""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback

from voxsub.speech_assets import runtime_files
from voxsub.speech_segments import pcm_windows, parse_index


def emit(kind, **fields):
    print(json.dumps(dict(kind=kind, **fields), ensure_ascii=True), flush=True)


def verify_weights(root, label):
    for item in runtime_files(label):
        path = root / item["file"]
        if not path.is_file() or path.stat().st_size != item["size"]:
            raise ValueError("模型文件不完整，请在模型广场重新下载")
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != item["sha256"]:
            raise ValueError("模型校验失败，请在模型广场重新下载")


class Granite:
    def __init__(self, root, torch):
        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
        self.torch = torch
        self.processor = AutoProcessor.from_pretrained(root, local_files_only=True)
        self.model = AutoModelForSpeechSeq2Seq.from_pretrained(
            root, dtype=torch.bfloat16, local_files_only=True).eval().to('cpu')

    def generate(self, pcm, instruction):
        p = self.processor
        prompt = p.tokenizer.apply_chat_template([dict(role='user', content='<|audio|>'+instruction)],
                                                 tokenize=False, add_generation_prompt=True)
        inputs = p(prompt, self.torch.from_numpy(pcm.copy()).unsqueeze(0), device='cpu', return_tensors='pt').to('cpu')
        with self.torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=512, do_sample=False, num_beams=1)
        generated = out[0, inputs['input_ids'].shape[-1]:]
        if len(generated) >= 512:
            raise ValueError("模型输出达到长度上限；未导出截断字幕")
        text = p.tokenizer.decode(generated, skip_special_tokens=True).strip()
        if not text:
            raise ValueError("模型没有返回文字")
        return text

    def infer(self, pcm, wav, target, history):
        # Same weights, two sequential generations; never load an independent MT.
        text = self.generate(pcm, 'Transcribe the English speech into a written format.')
        translation = self.generate(pcm, 'Translate the speech to Chinese.')
        return [dict(text=text, translation=translation, start=0., end=len(pcm)/16000)]


class Index:
    def __init__(self, root, torch):
        from voxsub.speech_index import AudioTransModel
        self.model = AudioTransModel(str(root), device='cpu', dtype=torch.bfloat16)

    def infer(self, pcm, wav, target, history):
        import soundfile as sf
        sf.write(wav, pcm, 16000, subtype='PCM_16')
        raw, _ = self.model.translate_window(str(wav), history, lang=target, max_new_tokens=1024)
        return parse_index(raw, len(pcm)/16000)


def run(job):
    import psutil
    available = psutil.virtual_memory().available / 2**30
    if available < (8 if job['label'] == 'granite' else 10):
        raise MemoryError("可用内存不足，请关闭其他高内存程序后重试")
    try:
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except (AttributeError, OSError, psutil.Error):
        pass
    emit('stage', stage='正在校验语音翻译模型')
    verify_weights(Path(job['weights']), job['label'])
    emit('stage', stage='正在加载语音翻译模型（CPU）')
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        torch.set_num_threads(2)
        torch.set_num_interop_threads(1)
        model = (Granite if job['label'] == 'granite' else Index)(Path(job['weights']), torch)
    emit('loaded', device='cpu', dtype='bfloat16')
    history = []
    for offset, pcm, total in pcm_windows(job['audio']):
        started = time.monotonic()
        emit('stage', stage='正在生成双语字幕（CPU，文件专用）')
        if not pcm.any():
            emit('segment', cues=[], elapsed_ms=0, completed=min(total, offset+len(pcm)/16000), total=total)
            continue
        with contextlib.redirect_stdout(sys.stderr):
            cues = model.infer(pcm, Path(job['chunk']), job['target'], history[-2:])
        for cue in cues:
            cue['start'] += offset
            cue['end'] += offset
        emit('segment', cues=cues, elapsed_ms=round((time.monotonic()-started)*1000),
             completed=min(total, offset+len(pcm)/16000), total=total)
        history.append('\n'.join(c['text']+'\n'+c['translation'] for c in cues)[-2000:])
        history = history[-2:]
    emit('done')


def main():
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_PROGRESS_BARS='1',
                      TOKENIZERS_PARALLELISM='false')
    try:
        job = json.loads(Path(sys.argv[1]).read_text(encoding='utf8'))
        run(job)
    except Exception as exc:
        # No audio, prompts, recognized text, credentials or filesystem paths in logs.
        safe = str(exc) if isinstance(exc, (MemoryError, ValueError)) else '语音翻译运行失败，请查看错误类型并检查模型运行组件'
        frames = traceback.extract_tb(exc.__traceback__)
        origin = ':'.join((Path(frames[-1].filename).stem, frames[-1].name, str(frames[-1].lineno))) if frames else ''
        emit('error', error_type=type(exc).__name__, message=safe, error_code=origin)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
