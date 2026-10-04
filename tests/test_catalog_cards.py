"""Catalog presentation assessment and handler tests: no actual hardware/audio/downloads."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlparse
import pytest
from voxsub.model_catalog import CATALOG, get_model
from voxsub.hardware import HardwareProfile
from voxsub.catalog_cards import card_assessment

PROFILE = HardwareProfile("fixture CPU", 4, 8, 16)
BASE = replace(get_model("asr-moonshine-tiny-en-v2"), quality_score=100, min_ram_gb=4, working_ram_gb=.1)

@pytest.mark.parametrize("cost,level", [(0,"recommended"),(31.9,"elevated"),(32,"elevated"),(54.4,"heavy"),(71.1,"insufficient")])
def test_estimated_compute_boundaries(cost, level):
    assert card_assessment(replace(BASE, compute_cost=cost), PROFILE)["level"] == level

def test_low_capability_and_insufficient_memory_are_distinct():
    basic = replace(BASE, quality_score=20, compute_cost=1)
    assert card_assessment(basic, PROFILE)["level"] == "basic"
    assert card_assessment(basic, replace(PROFILE,ram_gb=2))["level"] == "insufficient"

def test_recommendation_changes_on_the_actual_profile_not_install_state():
    model=replace(BASE,compute_cost=60)
    assert card_assessment(model, PROFILE)["level"] == "heavy"
    assert card_assessment(model, replace(PROFILE,physical_cores=16,logical_cores=32))["level"] == "recommended"
    assert card_assessment(model, replace(PROFILE,ram_gb=2))["level"] == "insufficient"

def test_gpu_npu_names_without_provider_do_not_accelerate_cpu_only_model():
    model=replace(BASE,compute_cost=60,gpu_supported=False,npu_supported=False)
    named=replace(PROFILE,gpu_name="fixture GPU",vram_gb=16,npu_name="fixture NPU")
    assert card_assessment(model,named)==card_assessment(model,PROFILE)

def test_all_catalog_models_have_friendly_copy_tags_and_upstream_roots():
    for model in CATALOG:
        assert model.description and not any(s in model.description for s in ("验证", "DirectML", "CPU", "INT8", "量化", "Q4", "Q8"))
        assert 1 <= len(model.tags) <= 3
        assert not any(s in tag for tag in model.tags for s in ("sherpa", "CPU", "GPU", "MIT", "Apache", "Q4", "Q6", "Q8"))
        url=urlparse(model.official_repo)
        assert url.scheme=="https" and url.hostname in {"github.com","huggingface.co"}
        assert len(url.path.strip('/').split('/'))==2

def handler(monkeypatch,tmp_path):
    import voxsub.hardware as hardware
    monkeypatch.setattr(hardware,"detect_hardware",lambda:PROFILE)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/"frontend/backend"))
    from handlers.models import ModelsHandlers
    h=ModelsHandlers();market=SimpleNamespace(models_dir=tmp_path,_lookup_roots=[tmp_path],is_installed=lambda m:m.id==BASE.id,available_model_dir=lambda m:tmp_path)
    h._marketplace=lambda args:market
    return h

def test_handler_supplies_ratings_repositories_tags_without_mutating_install_data(monkeypatch,tmp_path):
    h=handler(monkeypatch,tmp_path)
    result=h._cmd_list_models({})
    assert len(result["models"])==len(CATALOG)
    item=next(m for m in result["models"] if m["id"]==BASE.id)
    assert item["installed"] is True
    assert item["officialRepo"]==get_model(BASE.id).official_repo
    assert item["tags"]==list(get_model(BASE.id).tags)
    assert item["recommendation"]["level"]!="unknown"
    assert item["name"]==get_model(BASE.id).name
    assert item["runtime"]==get_model(BASE.id).runtime
    assert item["license"]==get_model(BASE.id).license

def test_hardware_failure_does_not_hide_models_or_claim_recommendation(monkeypatch,tmp_path):
    h=handler(monkeypatch,tmp_path)
    import voxsub.hardware as hardware
    monkeypatch.setattr(hardware,"detect_hardware",Mock(side_effect=RuntimeError("probe unavailable")))
    result=h._cmd_list_models({})
    assert len(result["models"])==len(CATALOG)
    assert all(m["recommendation"]["level"]=="unknown" for m in result["models"])
    assert result["diagnostics"]
    assert next(m for m in result["models"] if m["id"]==BASE.id)["installed"] is True

def test_per_model_assessment_error_is_isolated(monkeypatch,tmp_path):
    h=handler(monkeypatch,tmp_path)
    import voxsub.catalog_cards as cards
    real=cards.card_assessment
    monkeypatch.setattr(cards,"card_assessment",lambda m,p: (_ for _ in ()).throw(RuntimeError("one error")) if m.id==BASE.id else real(m,p))
    result=h._cmd_list_models({})
    assert result["models"][0]["recommendation"]["level"]=="unknown"
    assert any(m["recommendation"]["level"]!="unknown" for m in result["models"][1:])
    assert result["diagnostics"]


def test_new_repository_metadata_preserves_existing_positional_fields():
    from dataclasses import fields
    from voxsub.model_catalog import ModelSpec
    names = [f.name for f in fields(ModelSpec)]
    assert names[-3:] == ["tts_languages", "tts_speaker_ids", "official_repo"]
