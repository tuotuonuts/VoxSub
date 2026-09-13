"""配置默认值的测试。

## 为什么需要

界面上"识别调优"分页的默认值直接来自配置 schema。之前这个默认值被改过
（auto → context，并把四个数值对齐 pipeline 里 context 预设），但**没有任何
测试守住它**。

而界面测试若断言"当前值等于默认值"，就会依赖用户设置 —— 用户一改就报假失败
（实测踩到：用户人工测试时把识别切成了云端，界面测试写死的断言就失败了）。
所以把"默认值是什么"放在这里测（纯配置层，不受用户设置影响），
界面测试只负责"控件能正确显示当前值"。
"""

from __future__ import annotations

import pytest

from voxsub.config_store import APP_CONFIG_SCHEMA, ConfigStore


def test_asr_tuning_default_is_context() -> None:
    """默认档位是"智能上下文"。

    它是一个真实存在的档（会开启上下文纠错），而不是旧版那个隐藏的自适应
    "auto" 档 —— 用户选了什么就该是什么，不该有一个看不出实际参数的选项。
    """
    assert APP_CONFIG_SCHEMA.defaults["asr_tuning_profile"] == "context"


def test_asr_tuning_defaults_match_context_preset() -> None:
    """默认数值必须与 pipeline 里 context 预设一致。

    不一致的后果：新装用户看到的"默认值"和实际生效的参数对不上 ——
    界面显示 0.32/500/18000/6，跑起来却是别的。
    """
    from voxsub.pipeline import Pipeline

    defaults = APP_CONFIG_SCHEMA.defaults
    expected = {
        "asr_vad_threshold": 0.32,
        "asr_silence_ms": 500,
        "asr_max_utterance_ms": 18000,
        "asr_beam_paths": 6,
    }
    for key, value in expected.items():
        assert defaults[key] == value, f"{key} 默认值应为 {value}，实际 {defaults[key]}"

    # 与 pipeline 的实际解析结果对齐：用默认档位解析出的就是这四个数
    p = Pipeline()
    p.set_asr_tuning({"profile": defaults["asr_tuning_profile"], "hotwords": ""})
    effective = p._effective_asr_tuning(generative=False)
    assert effective["vad_threshold"] == expected["asr_vad_threshold"]
    assert effective["silence_ms"] == expected["asr_silence_ms"]
    assert effective["max_utterance_ms"] == expected["asr_max_utterance_ms"]
    assert effective["beam_paths"] == expected["asr_beam_paths"]


def test_ui_profiles_are_all_accepted_by_backend() -> None:
    """界面列出的五个档位，后端必须全部接受。

    键名是易错点：界面曾写成 accurate（少一个 y），后端只认 accuracy ——
    选"准确优先"会被校验直接拒绝，而界面看不出来。
    """
    allowed = APP_CONFIG_SCHEMA.choices["asr_tuning_profile"]
    ui_profiles = {"responsive", "balanced", "accuracy", "context", "custom"}

    assert ui_profiles <= set(allowed), (
        f"界面列出的档位必须都被后端接受；缺少 {ui_profiles - set(allowed)}"
    )
    assert "accuracy" in allowed, "后端应接受 accuracy"
    assert "accurate" not in allowed, "accurate 是错的拼写，不该被接受"


def test_old_auto_profile_still_loads() -> None:
    """老配置里的 "auto" 仍要能载入（不能因为改了默认值就让老用户配置报错）。

    界面会把 auto 显示成"智能上下文"（见 settings.ts 的 normalizeProfile），
    但配置层必须能读进来。
    """
    assert "auto" in APP_CONFIG_SCHEMA.choices["asr_tuning_profile"]

    # normalize 不该抛错
    assert APP_CONFIG_SCHEMA.normalize("asr_tuning_profile", "auto") == "auto"


def test_invalid_profile_falls_back_to_default_silently() -> None:
    """非法档位不会报错，而是**静默回退到默认值**。

    这是界面必须发正确键名的根本原因：拼错（accurate vs accuracy）不会得到
    任何错误提示，用户只会发现"选了准确优先，下次打开又变回智能上下文"。
    实测确认：normalize("accurate") == "context"，写入后读回也是 "context"。
    """
    assert APP_CONFIG_SCHEMA.normalize("asr_tuning_profile", "accurate") == "context"
    assert APP_CONFIG_SCHEMA.normalize("asr_tuning_profile", "accuracy") == "accuracy"
    # 老配置的 auto 原样保留（界面负责把它显示成"智能上下文"）
    assert APP_CONFIG_SCHEMA.normalize("asr_tuning_profile", "auto") == "auto"


def test_config_store_roundtrips_tuning_profile(tmp_path, monkeypatch) -> None:
    """写进去再读出来要一致（ConfigStore 是界面读写的实际路径）。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    store = ConfigStore()
    store.update({"asr_tuning_profile": "accuracy"})
    assert ConfigStore().load()["asr_tuning_profile"] == "accuracy"

    # 非法值不抛错，但会静默变成默认值 —— 这正是拼错键名的危害
    store.update({"asr_tuning_profile": "accurate"})
    assert ConfigStore().load()["asr_tuning_profile"] == "context", (
        "非法档位应静默回退到默认值（而不是保留非法值或抛错）"
    )
