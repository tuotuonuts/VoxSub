"""Presentation assessment only; no model selection, downloads or configuration writes."""
from __future__ import annotations
from voxsub.hardware import HardwareProfile
from voxsub.model_catalog import CATALOG, ModelSpec, assess_model

def card_assessment(model: ModelSpec, profile: HardwareProfile) -> dict[str, object]:
    estimate = assess_model(model, profile, CATALOG)
    if profile.ram_gb + 0.05 < model.min_ram_gb or estimate.load_percent > 110:
        level = "insufficient"
    elif estimate.load_percent >= 85:
        level = "heavy"
    elif estimate.load_percent >= 50:
        level = "elevated"
    elif estimate.level == "不推荐":
        # Able to run, but this computer can handle much more capable alternatives.
        level = "basic"
    else:
        level = "recommended"
    return {"level": level, "loadPercent": estimate.load_percent, "reason": estimate.reason}
