"""Device routing / single generation / external runtime boundaries, without loading weights."""
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np
import pytest
from voxsub.speech_worker import select_device, Granite
from voxsub.speech_contract import validate_selection, SPEECH_PAIRS
from voxsub.model_catalog import get_model
from voxsub.subtitles import SubtitleLine, SubtitleExporter
from voxsub.overlay_native import validate_request

@pytest.mark.parametrize('requested,available,bf16,expected', [
 ('auto',True,True,'cuda:0'),('auto',False,False,'cpu'),('cpu',True,True,'cpu'),('cuda',True,True,'cuda:0')])
def test_device_route(requested,available,bf16,expected):
    torch=SimpleNamespace(cuda=SimpleNamespace(is_available=lambda:available,is_bf16_supported=lambda:bf16))
    assert select_device(torch,requested)==expected

@pytest.mark.parametrize('requested,available,bf16',[('cuda',False,False),('cuda',True,False),('invalid',True,True)])
def test_no_silent_cpu_fallback(requested,available,bf16):
    torch=SimpleNamespace(cuda=SimpleNamespace(is_available=lambda:available,is_bf16_supported=lambda:bf16))
    with pytest.raises(ValueError):select_device(torch,requested)

@pytest.mark.parametrize('output,count',[('bilingual',2),('translation',1)])
def test_granite_generation_count_and_target(output,count):
    model=Granite.__new__(Granite);model.source='fr';model.output=output
    model.generate=Mock(return_value='translated')
    cues=model.infer(np.ones(16000),None,'en',[])
    assert model.generate.call_count==count
    assert 'English' in model.generate.call_args.args[1]
    assert bool(cues[0]['text'])==(output=='bilingual')
    if count==2:assert 'French' in model.generate.call_args_list[0].args[1]

@pytest.mark.parametrize('key,value',[('speech_device','npu'),('speech_output','unknown'),('speech_model_id','speech-seamless-streaming')])
def test_external_or_invalid_config_cannot_be_run(key,value):
    with pytest.raises(ValueError):validate_selection({key:value})

def test_seamless_pinned_download_only_with_both_regions():
    model=get_model('speech-seamless-streaming')
    assert model.external_runtime and model.usage_url.startswith('https://github.com/facebookresearch/')
    assert model.license=='CC-BY-NC-4.0'
    assert {s.id for s in model.sources}=={'china','global'}
    assert model.id not in SPEECH_PAIRS
    for s in model.sources:
        assert len(s.files)==len(model.required_paths)==5
        assert all(f.size>0 and len(f.sha256)==64 for f in s.files)
        assert all('/resolve/master/' not in f.url for f in s.files)

@pytest.mark.parametrize('format',['srt','vtt'])
def test_translation_only_export_has_no_blank_line_inside_cue(tmp_path,format):
    p=tmp_path/f'out.{format}'
    getattr(SubtitleExporter,f'write_{format}')([SubtitleLine('','Hello',0,end_ms=1000)],p)
    text=p.read_text(encoding='utf-8-sig')
    assert '\n\nHello' not in text
    assert '\nHello' in text

def test_native_request_rejects_invalid_rectangles():
    valid={'hwnd':'123','pid':123,'shape':[dict(x=0,y=0,width=10,height=20)],'material':True}
    assert validate_request(valid)==valid
    for shape in [[],[dict(x=-1,y=0,width=1,height=1)],[dict(x=0,y=0,width=float('nan'),height=1)]]:
        with pytest.raises(ValueError):validate_request({**valid,'shape':shape})

def test_auto_fallback_is_observable():
    from voxsub.speech_runtime import _observe
    sink=Mock();_observe(sink,'g',{'kind':'loaded','device':'cpu','fallback':'cuda_unavailable'})
    assert sink.call_args.args[0]['fallback_reason']=='cuda_unavailable'
    assert sink.call_args.args[0]['inference_verified'] is False


def test_silent_window_is_not_evidence_of_model_inference():
    from voxsub.speech_runtime import _observe
    sink=Mock();_observe(sink,'g',{'kind':'segment','device':'cuda:0','cues':[]})
    assert sink.call_args.args[0]['inference_verified'] is False
