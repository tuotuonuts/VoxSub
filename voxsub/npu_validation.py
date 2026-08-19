"""Model-level Intel NPU compatibility evidence shown by Model Hub."""
from __future__ import annotations

from dataclasses import dataclass


NPU_STATUS_VERIFIED = "verified"
NPU_STATUS_PENDING = "pending"
NPU_STATUS_UNSUPPORTED = "unsupported"
NPU_STATUS_FAILED = "failed"
NPU_STATUS_LIMITED = "limited"

NPU_STATUS_COLORS = {
    NPU_STATUS_VERIFIED: "#34D399",
    NPU_STATUS_PENDING: "#FBBF24",
    NPU_STATUS_UNSUPPORTED: "#6B7280",
    NPU_STATUS_FAILED: "#F87171",
    NPU_STATUS_LIMITED: "#F87171",
}


@dataclass(frozen=True)
class NpuCompatibility:
    status: str
    label_zh: str
    label_en: str
    reason_zh: str
    reason_en: str
    device: str = ""
    driver: str = ""
    runtime: str = ""
    validated_at: str = ""

    @property
    def color(self) -> str:
        return NPU_STATUS_COLORS[self.status]


_ASR_UNSUPPORTED = NpuCompatibility(
    NPU_STATUS_UNSUPPORTED,
    "NPU 不可用",
    "NPU unavailable",
    "当前 sherpa-onnx 语音识别运行时不支持 Intel NPU；此模型会使用 CUDA、CoreML 或 CPU。",
    "The current sherpa-onnx speech runtime does not support Intel NPU; this model uses CUDA, CoreML, or CPU.",
)

_OPUS_UNSUPPORTED = NpuCompatibility(
    NPU_STATUS_UNSUPPORTED,
    "NPU 不可用",
    "NPU unavailable",
    "当前安装包使用 ONNX Runtime DirectML，未包含 Intel OpenVINO NPU 执行后端；OPUS-MT 会使用显卡、核显或 CPU。",
    "The packaged ONNX Runtime uses DirectML and does not include the Intel OpenVINO NPU provider; OPUS-MT uses a GPU, integrated GPU, or CPU.",
)

_GGUF_PENDING = NpuCompatibility(
    NPU_STATUS_PENDING,
    "NPU 待验证",
    "NPU pending",
    "模型格式与随包 OpenVINO NPU 运行时兼容，但该量化文件尚未完成真机推理验证。",
    "The model format is compatible with the packaged OpenVINO NPU runtime, but this quantized file has not completed hardware inference validation.",
)

_GGUF_1_8B_VERIFIED = NpuCompatibility(
    NPU_STATUS_VERIFIED,
    "NPU 已验证",
    "NPU verified",
    "已在 Intel AI Boost 真机通过 VoxSub 自动 NPU 调度，以及禁用 CPU 回退的强制 NPU 推理。",
    "Passed VoxSub's automatic NPU route and forced-NPU inference with CPU fallback disabled on Intel AI Boost hardware.",
    device="Intel(R) AI Boost (Core Ultra 5 225H)",
    driver="32.0.100.4841",
    runtime="llama.cpp OpenVINO / NPU",
    validated_at="2026-08-20",
)


# Hardware-probe results are promoted from pending only after both the explicit
# NPU probe and VoxSub's automatic application route pass for the exact file.
NPU_COMPATIBILITY: dict[str, NpuCompatibility] = {
    "asr-funasr-nano-2512-int8": _ASR_UNSUPPORTED,
    "asr-qwen3-0.6b-int8": _ASR_UNSUPPORTED,
    "asr-sensevoice-small-int8": _ASR_UNSUPPORTED,
    "asr-zipformer-bilingual-fast": _ASR_UNSUPPORTED,
    "mt-hy-mt2-7b-q4": _GGUF_PENDING,
    "mt-hy-mt2-7b-q6": _GGUF_PENDING,
    "mt-hy-mt2-7b-q8": _GGUF_PENDING,
    "mt-hy-mt2-1.8b-q4": _GGUF_1_8B_VERIFIED,
    "mt-hy-mt2-1.8b-q6": _GGUF_1_8B_VERIFIED,
    "mt-hy-mt2-1.8b-q8": _GGUF_1_8B_VERIFIED,
    "mt-opus-fast-builtin": _OPUS_UNSUPPORTED,
}


def npu_compatibility(model_id: str) -> NpuCompatibility:
    """Return explicit evidence; unknown catalog entries are never called verified."""
    return NPU_COMPATIBILITY.get(
        model_id,
        NpuCompatibility(
            NPU_STATUS_PENDING,
            "NPU 待验证",
            "NPU pending",
            "尚无此模型的 NPU 兼容性证据。",
            "No NPU compatibility evidence is available for this model.",
        ),
    )


__all__ = [
    "NPU_COMPATIBILITY", "NPU_STATUS_COLORS", "NPU_STATUS_FAILED",
    "NPU_STATUS_LIMITED", "NPU_STATUS_PENDING", "NPU_STATUS_UNSUPPORTED",
    "NPU_STATUS_VERIFIED", "NpuCompatibility", "npu_compatibility",
]
