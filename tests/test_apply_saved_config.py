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


def write_config(**overrides):
    """写一份配置到隔离的 LOCALAPPDATA，返回 store。

    模块级而不是某个测试类的方法：多个测试类都要用（提成方法会变成
    "另一个类里调不到"的假失败）。
    """
    from voxsub.config_store import ConfigStore

    store = ConfigStore()
    store.update(overrides)
    return store


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


class TestSetTranslatorHonorsSavedConfig:
    """切换档位/模型时，已保存的模型选择必须传下去。

    用户报告"快档不支持 ja→zh"排查时顺带实测到的第二个问题：
    界面两个入口都传 `config: {}`——
      · 设置页点档位单选（只传 tier）
      · 模型广场选翻译模型（先写 translate_model_id，再传 kind）
    此前合并后的配置只用于"档位→kind"的映射，创建翻译器时仍用参数里的空
    dict，factory 拿不到 translate_model_id 就退回默认 Qwen 模型。实测两条
    路径加载的文件不同：
      启动路径 → hy-mt2-1.8b-q8（用户选的）
      点一次档位 → legacy-llm/qwen2.5-1.5b-instruct-q4_k_m（旧模型）
    也就是说：点一下档位单选就把用户选的模型换掉了；在模型广场选完模型
    还要重启才生效。
    """

    USER_MODEL = "mt-hy-mt2-1.8b-q8"

    @staticmethod
    def _capture_loader(monkeypatch):
        """拦截 _load_translator，记录它收到的 (kind, config)。"""
        import voxsub.pipeline as pipeline_module

        calls: list[tuple[str, dict]] = []

        class _Stub:
            langs = ("zh", "en", "ja", "ko")

            def supports(self, src, dst):
                return True

            def translate(self, text, src, dst, **kw):
                return text

            def close(self):
                pass

        def fake_load(kind, config=None):
            calls.append((kind, dict(config or {})))
            return _Stub(), kind

        monkeypatch.setattr(pipeline_module, "_load_translator", fake_load)
        return calls

    def _write_and_build(self, isolated_config, monkeypatch):
        write_config(translate_tier="quality", translate_model_id=self.USER_MODEL)
        calls = self._capture_loader(monkeypatch)
        pipeline = ipc_server.BackendService().ensure_pipeline()
        calls.clear()
        return pipeline, calls

    def test_tier_path_keeps_saved_model(self, isolated_config, monkeypatch):
        """设置页点档位单选（传 config={}）不能丢掉已选的模型。"""
        pipeline, calls = self._write_and_build(isolated_config, monkeypatch)
        pipeline.set_langs("ja", "zh")
        service = ipc_server.BackendService()
        service._pipeline = pipeline  # noqa: SLF001

        service._cmd_set_translator(pipeline, {"tier": "quality", "config": {}})  # noqa: SLF001
        pipeline._ensure_translator()  # noqa: SLF001

        assert calls, "没有调用翻译器加载"
        kind, config = calls[-1]
        assert kind == "qwen-quality", kind
        assert config.get("translate_model_id") == self.USER_MODEL, (
            f"加载器拿到的配置里没有用户选的模型：{config.get('translate_model_id')!r}")

    def test_kind_path_keeps_saved_model(self, isolated_config, monkeypatch):
        """模型广场选翻译模型（传 kind）也要用刚保存的模型，不必重启。"""
        pipeline, calls = self._write_and_build(isolated_config, monkeypatch)
        pipeline.set_langs("ja", "zh")
        service = ipc_server.BackendService()
        service._pipeline = pipeline  # noqa: SLF001

        service._cmd_set_translator(pipeline, {"kind": "qwen-quality", "config": {}})  # noqa: SLF001
        pipeline._ensure_translator()  # noqa: SLF001

        _, config = calls[-1]
        assert config.get("translate_model_id") == self.USER_MODEL, config.get("translate_model_id")

    def test_explicit_config_wins_over_saved(self, isolated_config, monkeypatch):
        """显式传进来的配置项优先级更高（合并顺序不能反过来）。"""
        pipeline, calls = self._write_and_build(isolated_config, monkeypatch)
        service = ipc_server.BackendService()
        service._pipeline = pipeline  # noqa: SLF001

        service._cmd_set_translator(  # noqa: SLF001
            pipeline, {"tier": "quality", "config": {"translate_model_id": "别的模型"}})
        pipeline._ensure_translator()  # noqa: SLF001

        _, config = calls[-1]
        assert config.get("translate_model_id") == "别的模型", config.get("translate_model_id")

    def test_no_config_arg_still_uses_saved(self, isolated_config, monkeypatch):
        """完全不传 config（None）时同样要用保存的模型。"""
        pipeline, calls = self._write_and_build(isolated_config, monkeypatch)
        service = ipc_server.BackendService()
        service._pipeline = pipeline  # noqa: SLF001

        service._cmd_set_translator(pipeline, {"tier": "quality"})  # noqa: SLF001
        pipeline._ensure_translator()  # noqa: SLF001

        _, config = calls[-1]
        assert config.get("translate_model_id") == self.USER_MODEL
