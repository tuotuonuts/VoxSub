from pathlib import Path

import pytest

from voxsub.realtime_builder import RealtimeBuildSpec, build_realtime_components


class _Resource:
    def __init__(self, name: str, closed: list[str]) -> None:
        self.name = name
        self._closed = closed

    def close(self) -> None:
        self._closed.append(self.name)


class _StreamingASR(_Resource):
    runtime = "sherpa-streaming-transducer"
    provider = "cpu"


class _CloudSTT(_Resource):
    def ready(self) -> bool:
        return True


def _spec(models_dir: Path, *, cloud: bool) -> RealtimeBuildSpec:
    return RealtimeBuildSpec(
        models_dir=models_dir,
        stt_provider="cloud" if cloud else "local",
        stt_config=object(),
        asr_model_id="streaming-model",
        asr_provider="cpu",
        source_lang="auto",
        tuning={
            "vad_threshold": 0.5,
            "silence_ms": 300,
            "max_utterance_ms": 4000,
            "context_enabled": False,
            "live_draft_enabled": True,
        },
        generative=False,
    )


def _builders(closed: list[str], *, streaming_asr: bool = True):
    vad = _Resource("vad", closed)

    def asr_factory(*_args, **_kwargs):
        return _StreamingASR("asr", closed)

    def cloud_factory(_config):
        return _CloudSTT("cloud", closed)

    def segmenter_factory(*_args, **_kwargs):
        if not streaming_asr:
            raise RuntimeError("segmenter construction failed")
        return _Resource("segmenter", closed)

    return {
        "queue_audio": lambda *_args: None,
        "on_sentence": lambda *_args: None,
        "on_partial": lambda *_args: None,
        "ensure_vad": lambda _root: Path("vad.onnx"),
        "vad_factory": lambda *_args, **_kwargs: vad,
        "asr_factory": asr_factory,
        "cloud_factory": cloud_factory,
        "audio_segmenter_factory": segmenter_factory,
        "streaming_segmenter_factory": segmenter_factory,
        "select_device": lambda *_args, **_kwargs: None,
        "semantic_boundary": None,
    }


def test_cloud_readiness_failure_closes_created_resources_in_reverse_order(
    tmp_path: Path,
) -> None:
    closed: list[str] = []
    builders = _builders(closed)
    cloud = builders["cloud_factory"](None)
    cloud.ready = lambda: False
    builders["cloud_factory"] = lambda _config: cloud

    with pytest.raises(RuntimeError, match="云 STT 尚未就绪"):
        build_realtime_components(_spec(tmp_path, cloud=True), **builders)

    assert closed == ["cloud", "vad"]


def test_segmenter_failure_closes_asr_and_vad_in_reverse_order(
    tmp_path: Path,
) -> None:
    closed: list[str] = []
    builders = _builders(closed, streaming_asr=False)

    with pytest.raises(RuntimeError, match="segmenter construction failed"):
        build_realtime_components(_spec(tmp_path, cloud=False), **builders)

    assert closed == ["asr", "vad"]
