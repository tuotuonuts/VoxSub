"""Bounded-memory PCM windows and strict bilingual timestamp parsing."""
from __future__ import annotations
import re
import wave
import numpy as np

TS = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)-(\d+):(\d+(?:\.\d+)?)\]$")


def parse_index(text: str, duration: float) -> list[dict]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or len(lines) % 3:
        raise ValueError("模型返回的字幕不完整；未把残缺结果当成成功")
    cues = []
    previous = 0.0
    for i in range(0, len(lines), 3):
        match = TS.fullmatch(lines[i])
        if not match:
            raise ValueError("模型返回的时间戳格式错误")
        m1, s1, m2, s2 = map(float, match.groups())
        start, end = m1 * 60 + s1, m2 * 60 + s2
        if not (s1 < 60 and s2 < 60 and previous <= start < end <= duration + 0.08):
            raise ValueError("模型时间戳超出音频范围或发生重叠")
        cues.append(dict(text=lines[i+1], translation=lines[i+2], start=start, end=min(end, duration)))
        previous = end
    return cues


def pcm_windows(path, seconds=15):
    """Read at most 15 seconds; cut near a quiet boundary, without dropping frames."""
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
            raise ValueError("语音翻译需要 16kHz 单声道 PCM16")
        total = wav.getnframes()
        position = 0
        while position < total:
            wav.setpos(position)
            pcm = np.frombuffer(wav.readframes(seconds * 16000), dtype='<i2').astype(np.float32) / 32768
            size = len(pcm)
            if size == seconds * 16000:
                # Last 3 seconds: choose the quietest 100ms. No VAD omission.
                tail = pcm[-48000:].reshape(-1, 1600)
                energy = np.mean(tail * tail, axis=1)
                if float(energy.min()) < 0.00001:
                    size = len(pcm) - 48000 + (int(energy.argmin()) + 1) * 1600
            yield position / 16000, pcm[:size], total / 16000
            position += size
