"""Executable language contracts, not marketing language counts.

Model contracts, prompt routing and canonical configuration share a registry.
No weights are loaded. Targets are directional, not intersected with ASR inputs.
"""
from __future__ import annotations

from collections.abc import Mapping, Iterable
from voxsub.language_guard import LANGUAGE_NAMES, normalize_language

from voxsub.language_registry import ASR_LANGUAGES, LANGUAGE_HINT_MODES, LANGUAGE_LABELS

# Auto is offered only if every possible decoded source has a translation path.
_ASR_CONTRACTS = {runtime: (langs, langs if len(langs) > 1 else ())
                  for runtime, langs in ASR_LANGUAGES.items()}


# Declared per-weight contracts, not the framework's multilingual model family.
# PP-OCRv6 Tiny excludes Japanese; no v6 unified tier includes Korean.
# v5 document stays at the narrower currently integrated/catalogued contract.
_OCR_CONTRACTS = {
    "rapidocr-v6-small": ("zh", "en", "ja"),
    "rapidocr-v6-tiny": ("zh", "en"),
    "rapidocr-v6-medium": ("zh", "en", "ja"),
    "rapidocr-v5-server": ("zh", "en"),
}


def build_options(asr_languages: Iterable[str], pairs: Iterable[tuple[str, str]],
                  auto_languages: Iterable[str] = ()) -> dict:
    """Intersect only sources; targets remain directional translator outputs."""
    supported = set(LANGUAGE_NAMES) - {"auto"}
    recognized = set(asr_languages) & supported
    edges = {(src, dst) for src, dst in pairs if src in supported and dst in supported}
    targets = {src: [dst for dst in LANGUAGE_NAMES if (src, dst) in edges]
               for src in LANGUAGE_NAMES if src in recognized}
    targets = {src: values for src, values in targets.items() if values}
    automatic = set(auto_languages)
    if automatic and automatic <= recognized:
        safe = [dst for dst in LANGUAGE_NAMES if dst != "auto" and
                all(src == dst or (src, dst) in edges for src in automatic)]
        if safe:
            targets = {"auto": safe, **targets}
    sources = list(targets)
    return {"sources": sources, "targets": targets,
            "compatible": bool(sources),
            "reason": "" if sources else "当前识别模型与翻译模型不兼容"}


def _translation_pairs(config: Mapping, kind: str | None) -> tuple[tuple[str, str], ...]:
    from voxsub.model_catalog import get_model
    from voxsub.translate.factory import kind_for_tier, kind_languages

    effective = kind or kind_for_tier(str(config.get("translate_tier") or "fast"), dict(config))
    if effective != "cloud":
        model_id = str(config.get("translate_model_id") or "")
        model = get_model(model_id) if model_id else None
        if model_id and (model is None or model.task != "translate"):
            return ()
        # The fast tier always runs OPUS, even when a quality model is selected.
        if effective == "opus-fast" or (model is not None and model.runtime == "opus-onnx"):
            return tuple((src, dst) for src in ("zh", "en") for dst in ("zh", "en"))
    langs = kind_languages(effective, config)
    return tuple((src, dst) for src in langs for dst in langs)


def language_capabilities(config: Mapping, *, mode: str = "a",
                          translation_kind: str | None = None) -> dict:
    from voxsub.model_catalog import get_model

    model = None
    if mode == "d":
        model = get_model(str(config.get("ocr_model_id") or "ocr-rapidocr-v6-small-builtin"))
        explicit = _OCR_CONTRACTS.get(model.runtime if model is not None and model.task == "ocr" else "", ())
        automatic = ()  # OCR can recognize Latin languages outside current translation routing.
    elif str(config.get("stt_provider") or "local") == "cloud":
        # Cloud adapter exposes the application's four-language routing contract.
        explicit = ("zh", "en", "ja", "ko")
        automatic = ()  # custom OpenAI-compatible APIs have no safe detection declaration
    else:
        model = get_model(str(config.get("asr_model_id") or "asr-zipformer-bilingual-fast"))
        explicit, automatic = _ASR_CONTRACTS.get(
            model.runtime if model is not None and model.task == "asr" else "", ((), ()))
    result = build_options(explicit, _translation_pairs(config, translation_kind), automatic)
    result["labels"] = {code: {"zh": LANGUAGE_LABELS[code], "en": LANGUAGE_NAMES[code]}
                        for code in LANGUAGE_NAMES}
    runtime = model.runtime if model is not None else "cloud"
    result["sourceLanguageHint"] = LANGUAGE_HINT_MODES.get(runtime, "provider")
    return result


def validate_pair(capabilities: Mapping, source: str, target: str) -> None:
    src = normalize_language(source, strict=True)
    dst = normalize_language(target, strict=True)
    if dst not in capabilities.get("targets", {}).get(src, ()):
        raise ValueError(f"当前模型不支持语言组合 {src} → {dst}；请选择兼容的识别/翻译语言")
