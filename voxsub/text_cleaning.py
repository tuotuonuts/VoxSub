"""Small, shared cleanup for model control tokens.

ASR and translation models sometimes expose their end-of-sequence markers in
the decoded string.  Those markers are protocol data, not user-visible text.
Keeping the cleanup in one dependency-free helper prevents each pipeline from
handling a different subset of tokens.
"""
from __future__ import annotations


# Once an EOS marker appears, anything after it is decoder spillover rather
# than part of the recognized/translated sentence.
_EOS_TOKENS = (
    "<|endoftext|>",
    "<|end_of_text|>",
    "<|eos|>",
    "<|eos_token|>",
    "<|end|>",
    "<|eot_id|>",
    "<|im_end|>",
    "</s>",
)
_INLINE_TOKENS = (
    "<|im_start|>",
    "<|assistant|>",
    "<|user|>",
    "<|system|>",
)


def strip_model_control_tokens(value: object) -> str:
    """Remove model protocol markers and normalize surrounding whitespace."""
    text = str(value or "")
    positions = [position for token in _EOS_TOKENS
                 if (position := text.find(token)) >= 0]
    if positions:
        text = text[:min(positions)]
    for token in _INLINE_TOKENS:
        text = text.replace(token, "")
    return " ".join(text.split()).strip()


__all__ = ["strip_model_control_tokens"]
