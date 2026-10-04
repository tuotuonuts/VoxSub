"""Executable language contracts, not marketing language counts.

Current normalization/translation adapters expose zh/en/ja/ko. Broader model
claims do not silently expand that application contract. No weights are loaded.
"""
from __future__ import annotations

from collections.abc import Mapping, Iterable
from voxsub.language_guard import LANGUAGE_NAMES, normalize_language

# (explicit selectable languages, safe automatic source languages).
# Auto is withheld when unconstrained decoding can emit languages outside the
# current translator contract (Qwen3, SenseVoice incl. Cantonese, Parakeet).
_ASR_CONTRACTS = {
    "sherpa-streaming-transducer": (("zh", "en"), ("zh", "en")),
    "sherpa-funasr-nano": (("zh", "en", "ja"), ("zh", "en", "ja")),
    "sherpa-qwen3-asr": (("zh", "en", "ja", "ko"), ()),
    "sherpa-sense-voice": (("zh", "en", "ja", "ko"), ()),
    "sherpa-moonshine-v2": (("en",), ()),
    "sherpa-parakeet-tdt": (("en",), ()),
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
    from voxsub.translate.factory import kind_for_tier, _class_for_kind

    effective = kind or kind_for_tier(str(config.get("translate_tier") or "fast"), dict(config))
    if effective != "cloud":
        model_id = str(config.get("translate_model_id") or "")
        model = get_model(model_id) if model_id else None
        if model_id and (model is None or model.task != "translate"):
            return ()
        # The fast tier always runs OPUS, even when a quality model is selected.
        if effective == "opus-fast" or (model is not None and model.runtime == "opus-onnx"):
            return tuple((src, dst) for src in ("zh", "en") for dst in ("zh", "en"))
    cls = _class_for_kind(effective)
    langs = tuple(getattr(cls, "langs", ()) or ())
    return tuple((src, dst) for src in langs for dst in langs)


def language_capabilities(config: Mapping, *, mode: str = "a",
                          translation_kind: str | None = None) -> dict:
    from voxsub.model_catalog import get_model

    if mode == "d":
        # OCR has no ASR dependency; its text routing shares the four-language guard.
        explicit = tuple(code for code in LANGUAGE_NAMES if code != "auto")
        automatic = ()
    elif str(config.get("stt_provider") or "local") == "cloud":
        # Cloud adapter exposes the application's four-language routing contract.
        explicit = tuple(code for code in LANGUAGE_NAMES if code != "auto")
        automatic = ()  # custom OpenAI-compatible APIs have no safe detection declaration
    else:
        model = get_model(str(config.get("asr_model_id") or "asr-zipformer-bilingual-fast"))
        explicit, automatic = _ASR_CONTRACTS.get(
            model.runtime if model is not None and model.task == "asr" else "", ((), ()))
    return build_options(explicit, _translation_pairs(config, translation_kind), automatic)


def validate_pair(capabilities: Mapping, source: str, target: str) -> None:
    src = normalize_language(source, strict=True)
    dst = normalize_language(target, strict=True)
    if dst not in capabilities.get("targets", {}).get(src, ()):
        raise ValueError(f"当前模型不支持语言组合 {src} → {dst}；请选择兼容的识别/翻译语言")
