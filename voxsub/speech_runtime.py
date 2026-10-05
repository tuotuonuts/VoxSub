"""Own isolated speech/ffmpeg processes; cancellation never touches other sessions."""
from __future__ import annotations
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import wave
import psutil

from voxsub.process_owner import own_process, release_process_job
from voxsub.file_io import write_text_atomically
from voxsub.subtitles import SubtitleLine
from voxsub.speech_contract import SPEECH_PAIRS
from voxsub.diagnostic_trace import record as trace_record


class SpeechCancelled(Exception):
    """User cancellation, not successful completion or model failure."""


def worker_command():
    if getattr(sys, 'frozen', False):
        worker = Path(sys.executable).parent / 'speech-runtime' / 'VoxSubSpeechWorker.exe'
        if worker.is_file():
            return [str(worker)]
    else:
        root = Path(__file__).resolve().parent.parent
        python = root / '.venv-speech' / 'Scripts' / 'python.exe'
        if python.is_file():
            return [str(python), '-m', 'voxsub.speech_worker']
    raise RuntimeError('缺少语音翻译运行组件，请安装含文件语音翻译组件的完整版本；源码版先运行 scripts/setup-speech-runtime.ps1')


def runtime_status():
    try:
        worker_command()
        return dict(status='not_run', detail='独立语音翻译组件已找到；实际推理未验证')
    except RuntimeError:
        return dict(status='fail', detail='缺少独立语音翻译组件，请安装完整版本')


def _stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _audio_file(path, directory, stop):
    from voxsub.file_transcriber import FileAudioDecoder
    try:
        with wave.open(str(path), 'rb') as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2):
                return path
    except (wave.Error, EOFError):
        pass
    ffmpeg = FileAudioDecoder.find_ffmpeg()
    if ffmpeg is None:
        raise RuntimeError('缺少音视频提取组件 ffmpeg')
    out = directory / 'audio.wav'
    command = [str(ffmpeg), '-nostdin', '-y', '-loglevel', 'error', '-i', str(path),
               '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(out)]
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as process:
        job_handle = None
        try:
            job_handle = own_process(process)
            deadline = time.monotonic() + 1800
            while process.poll() is None:
                if stop.wait(.1):
                    raise SpeechCancelled()
                if time.monotonic() > deadline:
                    raise TimeoutError('音视频提取超时')
            if process.returncode:
                raise RuntimeError('音视频提取失败，请检查文件是否完整')
        finally:
            _stop_process(process)
            release_process_job(job_handle)
    return out


def _reader(stream, events, closed):
    try:
        while not closed.is_set():
            line = stream.readline(65537)
            if not line:
                return
            if len(line) > 65536:
                line = '{"kind":"error","message":"模型返回内容超出安全长度"}'
            while not closed.is_set():
                try:
                    events.put(line, timeout=.1)
                    break
                except queue.Full:
                    continue
    finally:
        stream.close()


def _segment(event, lines, progress, present, model_id):
    for cue in event['cues']:
        start, end = round(cue['start']*1000), round(cue['end']*1000)
        if not (0 <= start < end) or (lines and start < lines[-1].end_ms):
            raise ValueError('语音翻译返回无效的全局时间戳')
        line = SubtitleLine(cue['text'], cue['translation'], start, end_ms=end)
        lines.append(line)
        present(line)
    progress(10 + int(85*event['completed']/max(.001, event['total'])), 100, '正在生成双语字幕')
    trace_record('speech_translation', 'segment_complete', model=model_id, provider='cpu',
                 duration_ms=event['elapsed_ms'], cue_count=len(event['cues']))


def _observe(observe, model_id, event):
    if observe is None:
        return
    kind = event.get('kind')
    if kind == 'stage':
        observe(dict(model=model_id, phase=event['stage']))
        return
    observe(dict(model=model_id, phase=kind, loaded=kind in ('loaded', 'segment'),
                 declared_provider='cpu' if kind in ('loaded', 'segment') else 'unverified',
                 inference_verified=kind == 'segment'))


def _guard_resources(stop, deadline, next_memory_check):
    if stop.is_set():
        raise SpeechCancelled()
    now = time.monotonic()
    if now > deadline:
        raise TimeoutError('语音翻译模型长时间无响应，已停止并释放模型')
    if now >= next_memory_check:
        if psutil.virtual_memory().available < 1024**3:
            raise MemoryError('系统可用内存过低，已停止语音翻译并释放模型')
        return now + 1
    return next_memory_check


def _consume(process, events, reader, stop, progress, present, model_id, observe=None):
    lines = []
    done = False
    completed = 5
    deadline = time.monotonic() + 600
    next_memory_check = 0.0
    while reader.is_alive() or not events.empty():
        next_memory_check = _guard_resources(stop, deadline, next_memory_check)
        try:
            raw = events.get(timeout=.1)
        except queue.Empty:
            continue
        event = json.loads(raw)
        deadline = time.monotonic() + 600
        kind = event.get('kind')
        _observe(observe, model_id, event)
        if kind == 'error':
            trace_record('speech_translation', 'failed', model=model_id, error_type=event.get('error_type', 'Unknown'), error_code=event.get('error_code', ''))
            raise RuntimeError(f"{event.get('message')} ({event.get('error_type', 'RuntimeError')})")
        if kind == 'segment':
            _segment(event, lines, progress, present, model_id)
            completed = 10 + int(85*event['completed']/max(.001, event['total']))
        elif kind == 'stage':
            progress(completed, 100, event['stage'])
        elif kind == 'loaded':
            trace_record('speech_translation', 'loaded', model=model_id, provider='cpu', runtime='torch-bf16')
        elif kind == 'done':
            done = True
    if stop.is_set():
        raise SpeechCancelled()
    if not done or process.wait(timeout=5) != 0:
        raise RuntimeError('语音翻译进程意外退出；未导出不完整字幕')
    if not lines:
        raise ValueError('音频中没有可识别的内容，未生成字幕文件')
    return lines


def run_speech_file(path, model_id, source, target, stop, progress, present, models_root=None, observe=None):
    from voxsub.model_catalog import get_model, ModelMarketplace
    if (source, target) not in SPEECH_PAIRS.get(model_id, ()):
        raise ValueError('此语音翻译模型不支持所选语言方向')
    command = worker_command()
    spec = get_model(model_id)
    marketplace = ModelMarketplace(models_root)
    if not marketplace.is_installed(spec):
        raise RuntimeError('请先在模型广场下载完整的语音翻译模型')
    with tempfile.TemporaryDirectory(prefix='voxsub-speech-') as temp:
        directory = Path(temp)
        audio = _audio_file(Path(path), directory, stop)
        if stop.is_set():
            raise SpeechCancelled()
        job = dict(label=spec.runtime.removeprefix('speech-'), weights=str(marketplace.available_model_dir(spec)),
                   audio=str(audio), chunk=str(directory/'chunk.wav'), source=source, target=target)
        write_text_atomically(directory/'job.json', json.dumps(job), encoding='utf8')
        env = {k:v for k,v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
        env.update(PYTHONIOENCODING='utf-8', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
        process = subprocess.Popen(command+[str(directory/'job.json')], cwd=str(Path(__file__).resolve().parent.parent),
                                   env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf8',
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        events = queue.Queue(maxsize=32)
        closed = threading.Event()
        reader = threading.Thread(target=_reader, args=(process.stdout, events, closed), daemon=True)
        job_handle = None
        reader.start()
        try:
            job_handle = own_process(process)
            return _consume(process, events, reader, stop, progress, present, model_id, observe)
        finally:
            closed.set()
            _stop_process(process)
            release_process_job(job_handle)
            reader.join(timeout=3)
            _observe(observe, model_id, {"kind": "released"})
