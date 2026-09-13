"""启动时应用保存配置的测试，以及调优键名归一化。

## 覆盖的用户报告

日志里日→中每一句都失败（快档不支持该语言对）。查下来根因不止一个，
这个文件守住其中两个：

### 1. 保存的档位从不生效

设置只在用户**点击控件**时才推给后端（前端各处的 ``CMD.set*``），启动时
前端只把配置读进界面，从不回推。于是重启后 pipeline 用自己硬编码的默认值跑。

实测证据：配置里 ``translate_tier=quality``、``translate_model_id=mt-hy-mt2-1.8b-q8``，
而会话实际用 opus-fast 快档 → 日→中每句抛"快档不支持语言对"。

### 2. 识别调优保存后不生效

``set_asr_tuning`` 直接存原始 dict，而 ``resolve_tuning_values`` 只读**短名**；
界面/配置发的是 ``asr_`` **前缀**名。于是无论选哪档都落到 auto 预设。

实测：发 context 档的完整参数 → 生效 vad=0.5/silence=350/max=4500（auto 的值），
而 context 应当是 0.32/500/18000。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "frontend" / "backend"))

import ipc_server  # noqa: E402
from voxsub.pipeline import Pipeline, tuning_from_config  # noqa: E402

# 与界面「识别调优」页保存时发来的载荷完全一致（asr_ 前缀）
UI_TUNING_PAYLOAD = {
    "asr_tuning_profile": "context",
    "asr_vad_threshold": 0.32,
    "asr_silence_ms": 500,
    "asr_max_utterance_ms": 18000,
    "asr_beam_paths": 6,
    "asr_max_new_tokens": 512,
    "asr_hotwords": "",
    "asr_context_hold_ms": 1800,
    "asr_live_draft_enabled": True,
    "asr_context_correction": True,
    "asr_filler_mode": "light",
}


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("VOXSUB_ROOT", str(ROOT))
    return tmp_path


# ------------------------------------------------------- 调优键名归一化


class TestTuningKeyNormalization:
    def test_prefixed_keys_are_normalized(self):
        """界面发 asr_ 前缀的键必须被识别 —— 这是"保存了但没用"的根因。"""
        pipeline = Pipeline()
        pipeline.set_asr_tuning(UI_TUNING_PAYLOAD)
        effective = pipeline.effective_tuning()

        assert effective["asr_vad_threshold"] == 0.32, effective
        assert effective["asr_silence_ms"] == 500, effective
        assert effective["asr_max_utterance_ms"] == 18000, effective
        assert effective["asr_beam_paths"] == 6, effective

    def test_context_only_keys_take_effect(self):
        """context 档独有的几项也要真的进去（它们只在 context 档被消费）。"""
        pipeline = Pipeline()
        pipeline.set_asr_tuning(UI_TUNING_PAYLOAD)
        internal = pipeline._asr_tuning  # noqa: SLF001

        assert internal["profile"] == "context"
        assert internal["context_hold_ms"] == 1800
        assert internal["live_draft_enabled"] is True
        assert internal["filler_mode"] == "light"

    def test_short_names_still_accepted(self):
        """内部短名也必须继续可用（不要为了修界面而破坏内部调用方）。"""
        pipeline = Pipeline()
        pipeline.set_asr_tuning({"profile": "accuracy"})
        assert pipeline.effective_tuning()["asr_vad_threshold"] == 0.25

    def test_tuning_from_config_matches_direct_payload(self):
        """两条入口（配置转换 / 界面直发）必须得到同样的结果。"""
        a = Pipeline()
        a.set_asr_tuning(UI_TUNING_PAYLOAD)
        b = Pipeline()
        b.set_asr_tuning(tuning_from_config(UI_TUNING_PAYLOAD))

        assert a.effective_tuning() == b.effective_tuning()

    @pytest.mark.parametrize("profile", ["responsive", "balanced", "accuracy", "context"])
    def test_every_profile_is_applied(self, profile):
        """每个预设档都要真生效 —— 此前它们全都落到 auto。"""
        from voxsub.pipeline import ASR_TUNING_PRESETS

        pipeline = Pipeline()
        pipeline.set_asr_tuning({"asr_tuning_profile": profile})
        effective = pipeline.effective_tuning()
        expected = ASR_TUNING_PRESETS[profile]

        assert effective["asr_vad_threshold"] == expected["vad_threshold"], profile
        assert effective["asr_silence_ms"] == expected["silence_ms"], profile

    def test_custom_profile_uses_user_values(self):
        """custom 档才走用户存的基础参数。"""
        pipeline = Pipeline()
        pipeline.set_asr_tuning({
            "asr_tuning_profile": "custom",
            "asr_vad_threshold": 0.41,
            "asr_silence_ms": 777,
            "asr_max_utterance_ms": 9000,
            "asr_beam_paths": 3,
        })
        effective = pipeline.effective_tuning()
        assert effective["asr_vad_threshold"] == 0.41
        assert effective["asr_silence_ms"] == 777
        assert effective["asr_max_utterance_ms"] == 9000
        assert effective["asr_beam_paths"] == 3


# --------------------------------------------------- 启动时应用保存的配置


class TestApplySavedConfig:
    """``_apply_saved_config`` 必须把配置真正推到 pipeline 上。"""

    @staticmethod
    def _write_config(tmp_path, **overrides):
        """写一份配置到隔离的 LOCALAPPDATA，返回 store。"""
        from voxsub.config_store import ConfigStore

        store = ConfigStore()
        store.update(overrides)
        return store

    def test_translate_tier_is_applied(self, isolated_config):
        """核心回归：保存的质量档必须变成 pipeline 的实际档位。

        此前 pipeline 永远停在 opus-fast 默认值，于是 quality 配置形同虚设。
        """
        self._write_config(isolated_config, translate_tier="quality",
                           translate_model_id="mt-hy-mt2-1.8b-q8")
        pipeline = Pipeline()

        ipc_server.BackendService._apply_saved_config(pipeline)  # noqa: SLF001

        assert pipeline._requested_trans_kind == "qwen-quality", (  # noqa: SLF001
            f"配置是 quality，pipeline 实际 {pipeline._requested_trans_kind!r}")

    def test_lang_pair_is_applied(self, isolated_config):
        """语言对存在配置里，重启后必须恢复。"""
        self._write_config(isolated_config, lang_pair="ja-zh")
        pipeline = Pipeline()

        ipc_server.BackendService._apply_saved_config(pipeline)  # noqa: SLF001

        assert (pipeline._src_lang, pipeline._dst_lang) == ("ja", "zh")  # noqa: SLF001

    def test_auto_source_lang_pair(self, isolated_config):
        """auto-zh 这种带通配符的写法也要能解析。"""
        self._write_config(isolated_config, lang_pair="auto-zh")
        pipeline = Pipeline()

        ipc_server.BackendService._apply_saved_config(pipeline)  # noqa: SLF001

        assert (pipeline._src_lang, pipeline._dst_lang) == ("auto", "zh")  # noqa: SLF001

    def test_asr_tuning_is_applied_with_config_keys(self, isolated_config):
        """配置里的 asr_ 前缀键要经过转换后生效（不是原样塞进去）。"""
        self._write_config(isolated_config, **UI_TUNING_PAYLOAD)
        pipeline = Pipeline()

        ipc_server.BackendService._apply_saved_config(pipeline)  # noqa: SLF001

        effective = pipeline.effective_tuning()
        assert effective["asr_vad_threshold"] == 0.32, effective
        assert effective["asr_silence_ms"] == 500, effective

    def test_mode_and_stt_provider_are_applied(self, isolated_config):
        self._write_config(isolated_config, mode="b", stt_provider="cloud")
        pipeline = Pipeline()

        ipc_server.BackendService._apply_saved_config(pipeline)  # noqa: SLF001

        assert pipeline._mode == "b"  # noqa: SLF001
        assert pipeline._requested_stt_provider == "cloud"  # noqa: SLF001

    def test_capture_target_is_applied(self, isolated_config):
        self._write_config(isolated_config, capture_process_id=4242,
                           capture_window_title="某应用")
        pipeline = Pipeline()

        ipc_server.BackendService._apply_saved_config(pipeline)  # noqa: SLF001

        assert pipeline._capture_process_id == 4242  # noqa: SLF001
        assert pipeline._capture_window_title == "某应用"  # noqa: SLF001

    def test_broken_value_does_not_block_other_settings(self, isolated_config):
        """一个坏值不能让整套设置失效（逐项应用的意义）。"""
        self._write_config(isolated_config, translate_tier="quality",
                           translate_model_id="mt-hy-mt2-1.8b-q8",
                           capture_process_id="不是数字", mode="c")
        pipeline = Pipeline()

        ipc_server.BackendService._apply_saved_config(pipeline)  # noqa: SLF001

        # 坏值那一项失败，但其它项照常生效
        assert pipeline._requested_trans_kind == "qwen-quality"  # noqa: SLF001
        assert pipeline._mode == "c"  # noqa: SLF001

    def test_quality_tier_with_builtin_model_maps_to_opus(self, isolated_config):
        """质量档 + 默认的 mt-opus-fast-builtin 模型 → 后端刻意用 OPUS 兼容。

        这是既有的兼容设计（见 TranslatorFactory.create），不是 bug；
        钉住它是为了让"为什么我选了质量档却是快档"有据可查。配套的界面提示
        会告诉用户这一档实际支持哪些语言。
        """
        self._write_config(isolated_config, translate_tier="quality",
                           translate_model_id="mt-opus-fast-builtin")
        pipeline = Pipeline()

        ipc_server.BackendService._apply_saved_config(pipeline)  # noqa: SLF001

        assert pipeline._requested_trans_kind == "opus-fast"  # noqa: SLF001

    def test_missing_config_does_not_raise(self, tmp_path, monkeypatch):
        """配置读不到时退回 pipeline 默认值，不能抛异常。"""
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "空目录"))
        monkeypatch.setenv("VOXSUB_ROOT", str(ROOT))
        pipeline = Pipeline()

        ipc_server.BackendService._apply_saved_config(pipeline)  # noqa: SLF001

        assert pipeline._requested_trans_kind == "opus-fast"  # noqa: SLF001

    def test_ensure_pipeline_applies_config(self, isolated_config):
        """入口处就要应用 —— 不能只在单独调用时才对。

        这条守住"接线"：_apply_saved_config 写好了但没被 ensure_pipeline 调用，
        用户侧的表现与没修完全一样。
        """
        self._write_config(isolated_config, translate_tier="quality",
                           translate_model_id="mt-hy-mt2-1.8b-q8")
        service = ipc_server.BackendService()

        pipeline = service.ensure_pipeline()

        assert pipeline._requested_trans_kind == "qwen-quality"  # noqa: SLF001
