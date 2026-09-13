"""识别调优元数据（置灰规则）的测试。

## 为什么需要

界面按后端给的 `controlled` / `editable` 决定哪些项置灰。这个规则**必须与
后端实际行为一致**，否则会出现两种都很难查的问题：

  · 该灰的没灰 —— 用户改了参数，后端却用预设值覆盖，改了等于没改；
  · 不该灰的灰了 —— 用户想调的项被锁住，功能白做。

这类"规则与实际行为脱节"的 bug 从界面上完全看不出来（置灰了、值也显示了，
看起来一切正常）。所以这里用后端**真实解析结果**反推每个键是否生效，
再与元数据声明的规则逐一比对。
"""

from __future__ import annotations

import pytest

from voxsub.pipeline import (
    ASR_TUNING_PRESETS,
    PROFILE_CONTROLLED_KEYS,
    PROFILE_EDITABLE_KEYS,
    Pipeline,
    asr_tuning_metadata,
)

PROFILES = ["responsive", "balanced", "accuracy", "context", "custom"]

# 基础参数：预设档下由 ASR_TUNING_PRESETS 覆盖，只有 custom 读用户值
BASE_KEYS = {
    "vad_threshold": (0.30, 0.70),
    "silence_ms": (300, 1500),
    "max_utterance_ms": (4000, 30000),
    "beam_paths": (2, 8),
}

# 上下文参数：只作用于 ContextualTextProcessor，而它仅在 context 档创建
CONTEXT_KEYS = {
    "context_hold_ms": (500, 3000),
    "context_correction": (True, False),
    "live_draft_enabled": (True, False),
    "filler_mode": ("off", "light"),
}

# 与档位无关、任何档位都可改的键
ALWAYS_KEYS = ("hotwords", "max_new_tokens")


def _resolve(profile: str, overrides: dict) -> dict:
    """用给定档位与用户参数解析出实际生效值。"""
    pipeline = Pipeline()
    pipeline.set_asr_tuning({"profile": profile, **overrides})
    return pipeline._effective_asr_tuning(generative=False)  # noqa: SLF001


def _affects(profile: str, key: str, pair: tuple) -> bool:
    """该键在这个档位下改用户值是否影响解析结果。"""
    low, high = pair
    return _resolve(profile, {key: low}) != _resolve(profile, {key: high})


# --------------------------------------------------------------- 规则与行为一致


def test_base_keys_only_editable_in_custom() -> None:
    """基础参数只在「自定义」档读用户值 —— 其余档位改了不生效。

    这是置灰规则的核心依据。逐档位、逐键用两个不同的用户值解析一次：
    解析结果相同 = 被预设覆盖 = 应该置灰。
    """
    for profile in PROFILES:
        for key, pair in BASE_KEYS.items():
            affects = _affects(profile, key, pair)
            declared_editable = key in PROFILE_EDITABLE_KEYS[profile]
            assert affects == declared_editable, (
                f"档位 {profile} 下 {key}："
                f"实际{'生效' if affects else '被预设覆盖'}，"
                f"但元数据声明{'可改' if declared_editable else '不可改'}"
            )


def test_context_keys_are_only_used_in_context_profile() -> None:
    """上下文参数只在「智能上下文」档真正参与识别。

    判断依据不是"解析结果变没变"（这几个值任何档位都会被读出来），
    而是 `context_enabled` —— 它为假时 ContextualTextProcessor 根本不会创建，
    这几个参数无处可用。
    """
    for profile in PROFILES:
        enabled = bool(_resolve(profile, {})["context_enabled"])
        assert enabled == (profile == "context"), (
            f"档位 {profile} 的 context_enabled 应为 {profile == 'context'}"
        )
        for key in CONTEXT_KEYS:
            declared_editable = key in PROFILE_EDITABLE_KEYS[profile]
            assert declared_editable == (profile == "context"), (
                f"档位 {profile} 下 {key} 的可改声明与 context_enabled={enabled} 不一致"
            )


def test_always_editable_keys_are_not_controlled() -> None:
    """hotwords 与 max_new_tokens 任何档位都不该锁。

    两者直接传给生成式 ASR（见 asr.py 的 OfflineGenerativeASR），与档位无关。
    若误放进受控列表，用户在预设档下就失去了热词能力 —— 而界面上看不出原因。
    """
    for key in ALWAYS_KEYS:
        assert key not in PROFILE_CONTROLLED_KEYS, (
            f"{key} 不应受档位控制（它在任何档位都生效）"
        )
        for profile in PROFILES:
            assert key not in PROFILE_EDITABLE_KEYS[profile], (
                f"{key} 不该出现在档位 {profile} 的可改列表里"
                f"（它不在受控列表，本来就总是可改）"
            )


def test_controlled_set_covers_exactly_the_dependent_keys() -> None:
    """受控集合必须精确等于"受档位影响"的键。

    多一个 → 某个一直生效的项被误锁；
    少一个 → 某个改了不生效的项仍可改（用户白改）。
    """
    expected = set(BASE_KEYS) | set(CONTEXT_KEYS)
    assert set(PROFILE_CONTROLLED_KEYS) == expected, (
        f"多出 {set(PROFILE_CONTROLLED_KEYS) - expected}，"
        f"缺少 {expected - set(PROFILE_CONTROLLED_KEYS)}"
    )


def test_every_controlled_key_is_editable_somewhere() -> None:
    """每个受控键至少要在某个档位可改。

    否则它就是个"永远灰着"的控件 —— 与其留着，不如直接从界面拿掉。
    """
    for key in PROFILE_CONTROLLED_KEYS:
        editable_somewhere = [
            profile for profile in PROFILES
            if key in PROFILE_EDITABLE_KEYS[profile]
        ]
        assert editable_somewhere, (
            f"{key} 在所有档位都被锁 —— 它不该出现在界面上"
        )


# ------------------------------------------------------------------- 数据形状


def test_presets_cover_the_base_keys() -> None:
    """每个预设档位都要给出全部四个基础参数。

    缺一个的话该参数会退回用户值 —— 而界面仍会置灰它（因为不在 editable 里），
    用户就既改不了、又不知道实际用的是什么。
    """
    for profile, values in ASR_TUNING_PRESETS.items():
        missing = set(BASE_KEYS) - set(values)
        assert not missing, f"档位 {profile} 的预设缺少 {missing}"


def test_metadata_shape_and_prefixes() -> None:
    """元数据的键名必须带 asr_ 前缀（跨层统一，界面直接用配置键名）。"""
    meta = asr_tuning_metadata()

    assert set(meta) == {"presets", "controlled", "editable"}
    for profile, values in meta["presets"].items():
        assert profile in PROFILES
        for key in values:
            assert key.startswith("asr_"), f"预设键名缺前缀: {key}"
    for profile, keys in meta["editable"].items():
        assert profile in PROFILES
        for key in keys:
            assert key.startswith("asr_"), f"editable 键名缺前缀: {key}"
    for key in meta["controlled"]:
        assert key.startswith("asr_"), f"controlled 键名缺前缀: {key}"


def test_editable_keys_are_known_config_keys() -> None:
    """元数据里出现的键必须是真实存在的配置键（拼错会导致界面静默不置灰）。"""
    from voxsub.config_store import APP_CONFIG_SCHEMA

    known = set(APP_CONFIG_SCHEMA.defaults)
    for profile, keys in PROFILE_EDITABLE_KEYS.items():
        for key in keys:
            assert f"asr_{key}" in known, (
                f"档位 {profile} 的 editable 里出现未知键 {key}"
                f"（应为 asr_{key}，且必须在配置 schema 中）"
            )
    for key in PROFILE_CONTROLLED_KEYS:
        assert f"asr_{key}" in known, f"受控键 {key} 不在配置 schema 中"


# --------------------------------------------------------------- 生效值报告


def test_effective_tuning_reports_preset_values() -> None:
    """effective_tuning() 必须报告预设值，而不是用户存的值。

    界面在置灰项上显示的就是这个返回值。若它回显用户值（例如 0.35），
    用户看到的就是一个不生效的数字 —— 比置灰更糟。
    """
    pipeline = Pipeline()
    # 故意存一个与 context 预设不同的值
    pipeline.set_asr_tuning({
        "profile": "context",
        "vad_threshold": 0.99,
        "silence_ms": 3000,
        "max_utterance_ms": 60000,
        "beam_paths": 1,
    })
    reported = pipeline.effective_tuning()

    preset = ASR_TUNING_PRESETS["context"]
    assert reported["asr_vad_threshold"] == preset["vad_threshold"]
    assert reported["asr_silence_ms"] == preset["silence_ms"]
    assert reported["asr_max_utterance_ms"] == preset["max_utterance_ms"]
    assert reported["asr_beam_paths"] == preset["beam_paths"]

    # 用户存的值一个都不该出现在报告里
    assert reported["asr_vad_threshold"] != 0.99


def test_effective_tuning_in_custom_reports_user_values() -> None:
    """「自定义」档下报告用户值 —— 那时它们才是真正生效的。"""
    pipeline = Pipeline()
    pipeline.set_asr_tuning({
        "profile": "custom",
        "vad_threshold": 0.42,
        "silence_ms": 1234,
        "max_utterance_ms": 22222,
        "beam_paths": 7,
    })
    reported = pipeline.effective_tuning()
    assert reported["asr_vad_threshold"] == pytest.approx(0.42)
    assert reported["asr_silence_ms"] == 1234
    assert reported["asr_max_utterance_ms"] == 22222
    assert reported["asr_beam_paths"] == 7


def test_effective_tuning_for_uses_config_not_instance_state() -> None:
    """IPC 用的 effective_tuning_for() 必须按**配置**算，不读实例状态。

    实测踩到的 bug：配置里是 context，但 pipeline 实例从未启动过，
    `_asr_tuning` 还是构造时的默认值（auto 档），于是界面被告知
    vad=0.5/beam=4（auto 的值），而实际会用 0.32/6（context 预设）。
    用户看到的就是一组不生效的数字。
    """
    from voxsub.pipeline import effective_tuning_for

    # 模拟配置：context 档，且用户存了与预设不同的基础参数
    config = {
        "asr_tuning_profile": "context",
        "asr_vad_threshold": 0.99,
        "asr_silence_ms": 3000,
        "asr_max_utterance_ms": 60000,
        "asr_beam_paths": 1,
    }
    reported = effective_tuning_for(config)

    preset = ASR_TUNING_PRESETS["context"]
    assert reported["asr_vad_threshold"] == preset["vad_threshold"], (
        f"应报预设值 {preset['vad_threshold']}，实际 {reported['asr_vad_threshold']}"
    )
    assert reported["asr_silence_ms"] == preset["silence_ms"]
    assert reported["asr_max_utterance_ms"] == preset["max_utterance_ms"]
    assert reported["asr_beam_paths"] == preset["beam_paths"]


def test_effective_tuning_for_handles_empty_config() -> None:
    """配置为空（首次运行）时不能崩，要给出 auto 档的合理默认值。"""
    from voxsub.pipeline import effective_tuning_for

    reported = effective_tuning_for({})
    assert reported["asr_vad_threshold"] > 0
    assert reported["asr_beam_paths"] >= 1
    assert "asr_hotwords" in reported


def test_tuning_key_map_covers_every_config_key() -> None:
    """配置键映射必须覆盖全部调优相关的配置键。

    漏一个的后果：界面改了它、保存到配置，但 effective_tuning_for 看不到它，
    显示的值与实际值不一致。
    """
    from voxsub.config_store import APP_CONFIG_SCHEMA
    from voxsub.pipeline import TUNING_KEY_MAP

    config_tuning_keys = {
        key for key in APP_CONFIG_SCHEMA.defaults if key.startswith("asr_")
    }
    # asr_model_id 不是"调优参数"（它是模型选择），单独排除
    config_tuning_keys.discard("asr_model_id")

    missing = config_tuning_keys - set(TUNING_KEY_MAP)
    assert not missing, f"映射缺少配置键: {sorted(missing)}"


def test_effective_tuning_covers_every_ui_key() -> None:
    """报告要覆盖界面上出现的每一个键，缺一个界面就会显示空值。"""
    from voxsub.pipeline import effective_tuning_for

    reported = effective_tuning_for({})
    expected = {
        "asr_vad_threshold", "asr_silence_ms", "asr_max_utterance_ms",
        "asr_beam_paths", "asr_max_new_tokens", "asr_hotwords",
        "asr_context_hold_ms", "asr_live_draft_enabled",
        "asr_context_correction", "asr_filler_mode",
    }
    assert set(reported) == expected, f"缺少 {expected - set(reported)}"
