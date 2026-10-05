"""Single-model routing, timestamps, cancellation and offline packaging contracts."""
import json
import queue
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import wave
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from voxsub.config_store import ConfigStore
from voxsub.language_capabilities import language_capabilities
from voxsub.model_catalog import CATALOG, get_model
from voxsub.speech_contract import SPEECH_PAIRS, validate_selection
from voxsub.speech_segments import parse_index, pcm_windows
from voxsub.subtitles import SubtitleLine, SubtitleExporter
from voxsub import speech_runtime as runtime


def wav_file(path, samples=8000, rate=16000):
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(rate)
        wav.writeframes(np.arange(samples, dtype='<i2').tobytes())
    return path


@pytest.mark.parametrize('model_id', SPEECH_PAIRS)
def test_speech_catalog_sources_and_integrity(model_id):
    model = get_model(model_id)
    assert model.task == 'speech' and not model.gpu_supported and not model.npu_supported
    assert {s.id for s in model.sources} == {'china', 'global'}
    for source in model.sources:
        assert {f.install_rel for f in source.files} == set(model.required_paths)
        assert all(f.size > 0 and len(f.sha256) == 64 for f in source.files)
        assert all(not f.install_rel.endswith('.py') for f in source.files)
    assert [(f.size, f.sha256) for f in model.sources[0].files] == [(f.size, f.sha256) for f in model.sources[1].files]


def test_directional_languages_and_other_modes_unchanged():
    base = {'asr_model_id': 'asr-sensevoice-small-int8', 'translate_tier': 'fast',
            'translate_model_id': 'mt-opus-fast-builtin'}
    config = {**base, 'file_translation_mode':'single', 'speech_model_id':'speech-index-echo-2b'}
    assert language_capabilities(config, mode='c')['targets'] == {'zh':['en','ja','es']}
    for mode in ('a','b','d'):
        assert language_capabilities(config, mode=mode) == language_capabilities(base, mode=mode)
    config['speech_model_id'] = 'unknown'
    assert not language_capabilities(config, mode='c')['compatible']
    with pytest.raises(ValueError): validate_selection(config)


def test_config_migration_defaults_and_no_dual_setting_loss(tmp_path):
    store = ConfigStore(tmp_path/'config.json')
    assert store.load()['file_translation_mode'] == 'dual'
    store.update({'file_translation_mode':'single','speech_model_id':'speech-index-echo-2b'})
    assert store.load()['asr_model_id'] == 'asr-zipformer-bilingual-fast'
    assert store.load()['translate_model_id'] == 'mt-opus-fast-builtin'
    assert store.load()['speech_model_id'] == 'speech-index-echo-2b'


@pytest.mark.parametrize('text', ['', 'hello', '[00:00-00:01]\n原文', '[00:00-00:08]\n原文\ntranslation',
                                  '[00:02-00:01]\n原文\ntranslation',
                                  '[00:00-00:02]\n原文\ntranslation\n[00:01-00:03]\n第二句\nsecond'])
def test_reject_incomplete_out_of_range_and_overlapping_cues(text):
    with pytest.raises(ValueError): parse_index(text, 4)


def test_valid_cues_and_precise_end_times_export(tmp_path):
    cue = parse_index('[00:00.12-00:03.25]\n你好\nHello', 3.5)[0]
    line = SubtitleLine(cue['text'],cue['translation'],120,end_ms=3250)
    SubtitleExporter.write_srt([line], tmp_path/'sub.srt')
    SubtitleExporter.write_vtt([line], tmp_path/'sub.vtt')
    assert '00:00:00,120 --> 00:00:03,250' in (tmp_path/'sub.srt').read_text(encoding='utf-8-sig')
    assert '00:00:03.250' in (tmp_path/'sub.vtt').read_text(encoding='utf8')


def test_windows_cover_every_frame_with_bounded_memory(tmp_path):
    path = wav_file(tmp_path/'audio.wav', samples=16_000*38+53)
    windows = list(pcm_windows(path))
    assert len(windows) >= 3
    assert sum(len(pcm) for _,pcm,_ in windows) == 16_000*38+53
    assert max(len(pcm) for _,pcm,_ in windows) <= 15*16000
    for previous, current in zip(windows, windows[1:]):
        assert current[0] == pytest.approx(previous[0]+len(previous[1])/16000)


def test_single_pipeline_never_builds_dual_and_preserves_subtitle(tmp_path, monkeypatch):
    from voxsub.pipeline import Pipeline
    wav = wav_file(tmp_path/'clip.wav')
    previous = tmp_path/'clip.voxsub.srt'; previous.write_text('user-owned',encoding='utf8')
    pipeline = Pipeline(models=tmp_path)
    pipeline.set_mode('c'); pipeline.set_file_translation({'file_translation_mode':'single'})
    pipeline.set_langs('en','zh'); pipeline.set_input_file(wav)
    pipeline._ensure_translator = Mock(side_effect=AssertionError('must not load MT'))
    pipeline._build_real_time = Mock(side_effect=AssertionError('must not load ASR'))
    def transcribe(path, model, source, target, stop, progress, present, root, observe):
        assert root == tmp_path and (source,target)==('en','zh')
        cue = SubtitleLine('Hi','你好',100,end_ms=500);present(cue);return [cue]
    monkeypatch.setattr(runtime,'run_speech_file',transcribe)
    cues=[];pipeline.on_file_subtitle(cues.append);pipeline._run_file_mode()
    assert previous.read_text(encoding='utf8') == 'user-owned'
    assert len(list(tmp_path.glob('clip.voxsub.*.srt'))) == 1
    assert cues[0].end_ms == 500
    pipeline._ensure_translator.assert_not_called();pipeline._build_real_time.assert_not_called()
    pipeline.close()


def test_cancel_does_not_export_success(tmp_path,monkeypatch):
    from voxsub.pipeline import Pipeline, PipelineState
    pipeline=Pipeline(models=tmp_path);pipeline.set_mode('c')
    pipeline.set_file_translation({'file_translation_mode':'single'});pipeline.set_input_file(wav_file(tmp_path/'clip.wav'))
    def cancel(*args):raise runtime.SpeechCancelled()
    monkeypatch.setattr(runtime,'run_speech_file',cancel)
    status=[];pipeline.on_status(status.append);pipeline._run_file_mode()
    assert pipeline.state == PipelineState.IDLE and not list(tmp_path.glob('*.srt'))
    assert any('取消' in s for s in status)
    pipeline.close()


def test_file_config_and_language_locked_while_running(tmp_path):
    from voxsub.pipeline import Pipeline, PipelineState
    pipeline=Pipeline(models=tmp_path);pipeline.set_mode('c')
    pipeline._state=PipelineState.RUNNING
    with pytest.raises(RuntimeError):pipeline.set_file_translation({'file_translation_mode':'single'})
    with pytest.raises(RuntimeError):pipeline.set_langs('en','zh')
    pipeline._state=PipelineState.IDLE;pipeline.close()


def fake_worker(monkeypatch,tmp_path,body):
    import voxsub.model_catalog as catalog
    script=tmp_path/'worker.py';script.write_text(body,encoding='utf8')
    monkeypatch.setattr(runtime,'worker_command',lambda:[sys.executable,str(script)])
    monkeypatch.setattr(catalog,'ModelMarketplace',lambda root:SimpleNamespace(is_installed=lambda s:True,available_model_dir=lambda s:tmp_path))
    return wav_file(tmp_path/'input.wav')


def test_real_subprocess_protocol_and_cancellation(tmp_path,monkeypatch):
    path=fake_worker(monkeypatch,tmp_path,"import time\ntime.sleep(30)\n")
    stop=threading.Event();timer=threading.Timer(.3,stop.set);timer.start()
    start=time.monotonic()
    try:
        with pytest.raises(runtime.SpeechCancelled):
            runtime.run_speech_file(path,'speech-granite-4-1b','en','zh',stop,lambda *a:None,lambda *a:None)
    finally:timer.cancel()
    assert time.monotonic()-start < 5


def test_real_subprocess_does_not_treat_crash_as_success(tmp_path,monkeypatch):
    path=fake_worker(monkeypatch,tmp_path,'raise SystemExit(7)')
    with pytest.raises(RuntimeError,match='退出'):
        runtime.run_speech_file(path,'speech-granite-4-1b','en','zh',threading.Event(),lambda *a:None,lambda *a:None)


def test_real_subprocess_exports_media_times(tmp_path,monkeypatch):
    events=[{'kind':'segment','cues':[{'text':'Hello','translation':'你好','start':.1,'end':.4}],
             'elapsed_ms':23,'completed':.5,'total':.5},{'kind':'done'}]
    body='import json\n'+''.join(f'print(json.dumps({e!r}),flush=True)\n' for e in events)
    path=fake_worker(monkeypatch,tmp_path,body);shown=[]
    lines=runtime.run_speech_file(path,'speech-granite-4-1b','en','zh',threading.Event(),lambda *a:None,shown.append)
    assert lines==shown and lines[0].ts_ms==100 and lines[0].end_ms==400


def test_packaged_runtime_has_independent_owner_and_dependencies():
    root=Path(__file__).resolve().parents[1]
    requirements=(root/'requirements-speech.txt').read_text(encoding='utf-8-sig')
    assert 'torchaudio==' in requirements and 'transformers==' in requirements
    build=(root/'frontend/tools/build-release.py').read_text(encoding='utf8')
    assert 'def build_speech_runtime' in build and 'speech-runtime' in build
    assert 'CREATE_NO_WINDOW' in (root/'voxsub/speech_runtime.py').read_text(encoding='utf8')

def test_file_selection_is_idempotent(tmp_path):
    from voxsub.pipeline import Pipeline
    pipeline=Pipeline(models=tmp_path)
    config={'file_translation_mode':'single','speech_model_id':'speech-index-echo-2b'}
    pipeline.set_file_translation(config); generation=pipeline.config_generation
    pipeline.set_file_translation(config)
    assert pipeline.config_generation == generation
    pipeline.close()


def test_resource_guard_timeout_and_low_memory(monkeypatch):
    with pytest.raises(TimeoutError):runtime._guard_resources(threading.Event(),time.monotonic()-1,0)
    monkeypatch.setattr(runtime.psutil,'virtual_memory',lambda:SimpleNamespace(available=1))
    with pytest.raises(MemoryError):runtime._guard_resources(threading.Event(),time.monotonic()+10,0)


def test_trace_reports_handled_file_failure_not_completion(tmp_path, monkeypatch):
    from voxsub.pipeline import Pipeline
    from voxsub.diagnostic_trace import snapshot
    pipeline=Pipeline(models=tmp_path);pipeline.set_mode('c')
    pipeline.set_file_translation({'file_translation_mode':'single'});pipeline.set_input_file(wav_file(tmp_path/'clip.wav'))
    def fail(*args):raise ValueError('fixture failure')
    monkeypatch.setattr(runtime,'run_speech_file',fail)
    pipeline._run_file_mode()
    assert [event for event in snapshot()['events'] if event['stage']=='file_session'][-1]['outcome']=='failed'
    pipeline.close()


@pytest.mark.skipif(os.name!='nt',reason='Windows job ownership')
def test_abnormal_parent_exit_kills_only_owned_worker(tmp_path):
    import psutil
    script=tmp_path/'parent.py'
    root=Path(__file__).resolve().parents[1]
    script.write_text(f"import sys,subprocess,time\nsys.path.insert(0,{str(root)!r})\nfrom voxsub.process_owner import own_process\np=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],creationflags=subprocess.CREATE_NO_WINDOW)\nh=own_process(p)\nprint(p.pid,flush=True)\ntime.sleep(60)\n",encoding='utf8')
    parent=subprocess.Popen([sys.executable,str(script)],stdout=subprocess.PIPE,text=True,creationflags=subprocess.CREATE_NO_WINDOW)
    child=None
    try:
        child=psutil.Process(int(parent.stdout.readline()))
        parent.kill();parent.wait(timeout=5)
        child.wait(timeout=5)
    finally:
        if parent.poll() is None:parent.kill();parent.wait(timeout=5)
        if child is not None and child.is_running():child.kill();child.wait(timeout=5)
        parent.stdout.close()

@pytest.mark.skipif(os.name!='nt',reason='Windows inherited-pipe startup regression')
def test_worker_does_not_inherit_idle_backend_stdin(tmp_path):
    root=Path(__file__).resolve().parents[1]
    script=tmp_path/'pipe_parent.py'
    script.write_text(f"import sys,subprocess,threading,queue,json\nsys.path.insert(0,{str(root)!r})\nfrom voxsub import speech_runtime as rt\nfrom types import SimpleNamespace\nfrom pathlib import Path\nimport voxsub.model_catalog as catalog\nrt.worker_command=lambda:[sys.executable,'-c',\"import json;print(json.dumps(dict(kind='done')),flush=True)\"]\nrt._audio_file=lambda *a:Path('fixture.wav')\ncatalog.ModelMarketplace=lambda *a:SimpleNamespace(is_installed=lambda s:True,available_model_dir=lambda s:Path('.'))\ndef run():\n try:rt.run_speech_file('fixture.wav','speech-granite-4-1b','en','zh',threading.Event(),lambda *a:None,lambda *a:None)\n except ValueError:print('WORKER_REACHED_DONE',flush=True)\nthreading.Thread(target=run,daemon=True).start()\nsys.stdin.buffer.readline()\n",encoding='utf8')
    parent=subprocess.Popen([sys.executable,str(script)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,creationflags=subprocess.CREATE_NO_WINDOW)
    from voxsub.process_owner import own_process, release_process_job
    job=own_process(parent)
    try:
        output=queue.Queue(maxsize=1)
        thread=threading.Thread(target=lambda:output.put(parent.stdout.readline()),daemon=True);thread.start()
        assert 'WORKER_REACHED_DONE' in output.get(timeout=12)
    finally:
        parent.stdin.write('\n');parent.stdin.flush()
        try:parent.wait(timeout=5)
        except subprocess.TimeoutExpired:parent.kill();parent.wait(timeout=5)
        release_process_job(job)
        parent.stdin.close();parent.stdout.close();parent.stderr.close()
