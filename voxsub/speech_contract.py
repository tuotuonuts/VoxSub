"""Single-model file speech contracts. Never used by live audio or OCR."""
from __future__ import annotations

SPEECH_PAIRS = {
    "speech-granite-4-1b": (("en", "zh"),),
    "speech-index-echo-2b": (("zh", "en"), ("zh", "ja"), ("zh", "es")),
}
# Deliberately admitted pairs, not a claim to cover the model family's full abilities.
DEFAULT_SPEECH_MODEL = "speech-granite-4-1b"


def single_model(config, mode="c") -> bool:
    return mode == "c" and config.get("file_translation_mode") == "single"


def validate_selection(config) -> None:
    if config.get("file_translation_mode", "dual") not in ("dual", "single"):
        raise ValueError("无效的文件翻译方式")
    if config.get("speech_model_id", DEFAULT_SPEECH_MODEL) not in SPEECH_PAIRS:
        raise ValueError("请选择支持的文件语音翻译模型")


def speech_options(config) -> dict:
    from voxsub.language_capabilities import build_options
    from voxsub.language_registry import LANGUAGE_LABELS, LANGUAGE_NAMES
    pairs = SPEECH_PAIRS.get(str(config.get("speech_model_id", DEFAULT_SPEECH_MODEL)), ())
    result = build_options((src for src, _ in pairs), pairs)
    result["labels"] = {code: {"zh": LANGUAGE_LABELS[code], "en": name}
                        for code, name in LANGUAGE_NAMES.items()}
    result["sourceLanguageHint"] = "prompt"
    return result
