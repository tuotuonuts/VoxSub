"""音频设备选择（麦克风 / 系统输出）的测试。

## 覆盖的缺陷

界面下拉里存的设备 id 与后端查找时用的值**不是同一个东西**：

* ``_cmd_list_audio_devices`` 读的是 ``device.device_id``，而
  ``AudioDeviceInfo`` 根本没有这个字段 → 回落成设备**名字**发给界面；
* ``Pipeline._find_device`` 比对的是 ``info.device.id``（WASAPI 端点 ID）。

于是用户在设置里选一次麦克风，写进配置的是名字；开始会话时拿名字去比
WASAPI ID 必然不匹配，直接抛：

    已选择的麦克风当前不可用，请在设置中重新选择

也就是"选设备"这个功能实际是坏的 —— 而且选完才报错，很难联想到是这个原因。

修法：
  · 列表命令改发 ``AudioDeviceInfo.id``（与配置、与查找用的是同一个值）；
  · ``_find_device`` 增加按名字回退，让**已经存了名字的配置**继续可用，
    而不是把用户卡在一个他无法理解的报错上。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "frontend" / "backend"))

import ipc_server  # noqa: E402
from voxsub.pipeline import Pipeline  # noqa: E402

MIC_ENDPOINT = "{0.0.1.00000000}.{859c983d-8e71-462c-b8e9-2192c96f9381}"
LOOP_ENDPOINT = "{0.0.0.00000000}.{966104c2-bbc5-405a-8ad7-badcf10e339d}"
MIC_NAME = "麦克风 (Steam Streaming Microphone)"
LOOP_NAME = "PHL24M1N3200Z (NVIDIA High Definition Audio)"


class _FakeSoundcardDevice:
    """模拟 soundcard 的设备对象：只有 ``id`` / ``name``。"""

    def __init__(self, endpoint_id: str, name: str) -> None:
        self.id = endpoint_id
        self.name = name


class _FakeAudioDeviceInfo:
    """模拟 ``voxsub.audio.AudioDeviceInfo``（NamedTuple + id 属性）。

    刻意**不**提供 ``device_id``：真实类就没有这个字段，而缺陷正源于
    调用方假设它存在。
    """

    def __init__(self, endpoint_id: str, name: str) -> None:
        self.name = name
        self.kind = "mic"
        self.device = _FakeSoundcardDevice(endpoint_id, name)

    @property
    def id(self) -> str:
        return str(getattr(self.device, "id", ""))


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("VOXSUB_ROOT", str(ROOT))
    return tmp_path


@pytest.fixture()
def fake_devices(monkeypatch):
    """把 audio 的设备枚举换成固定的两个设备。"""
    import voxsub.audio as audio

    mics = [_FakeAudioDeviceInfo(MIC_ENDPOINT, MIC_NAME)]
    loops = [_FakeAudioDeviceInfo(LOOP_ENDPOINT, LOOP_NAME)]
    monkeypatch.setattr(audio, "list_microphones", lambda include_loopback=True: list(mics))
    monkeypatch.setattr(audio, "list_loopbacks", lambda: list(loops))
    return mics, loops


class TestListAudioDevices:
    def test_returns_wasapi_endpoint_ids(self, isolated_config, fake_devices):
        """回归：此前返回的是设备**名字**，导致选择功能失效。"""
        service = ipc_server.BackendService()
        result = service._cmd_list_audio_devices({})  # noqa: SLF001

        assert result["microphones"][0]["id"] == MIC_ENDPOINT, result["microphones"][0]
        assert result["loopbacks"][0]["id"] == LOOP_ENDPOINT, result["loopbacks"][0]

    def test_still_exposes_display_name(self, isolated_config, fake_devices):
        """id 是端点 ID，但界面仍要有可读名字。"""
        service = ipc_server.BackendService()
        result = service._cmd_list_audio_devices({})  # noqa: SLF001

        assert result["microphones"][0]["name"] == MIC_NAME
        assert result["loopbacks"][0]["name"] == LOOP_NAME

    def test_id_is_not_the_name(self, isolated_config, fake_devices):
        """直接把缺陷形态钉住：id 与 name 不能相同（本用例里它们本就不同）。"""
        service = ipc_server.BackendService()
        entry = service._cmd_list_audio_devices({})["microphones"][0]  # noqa: SLF001

        assert entry["id"] != entry["name"]

    def test_falls_back_to_name_when_no_endpoint_id(self, isolated_config, monkeypatch):
        """没有端点 ID 的假设备回落用名字，避免 id 全为空而互相撞车。"""
        import voxsub.audio as audio

        monkeypatch.setattr(audio, "list_microphones",
                            lambda include_loopback=True: [_FakeAudioDeviceInfo("", "无 ID 设备")])
        monkeypatch.setattr(audio, "list_loopbacks", lambda: [])

        entry = ipc_server.BackendService()._cmd_list_audio_devices({})["microphones"][0]  # noqa: SLF001
        assert entry["id"] == "无 ID 设备"


class TestFindDevice:
    """``Pipeline._find_device`` 的匹配规则。"""

    def test_matches_by_endpoint_id(self):
        devices = [_FakeAudioDeviceInfo(MIC_ENDPOINT, MIC_NAME)]
        found = Pipeline._find_device(devices, MIC_ENDPOINT, "麦克风")
        assert found is devices[0].device

    def test_falls_back_to_name_for_old_configs(self):
        """旧配置里存的是设备名 —— 必须仍能用，否则用户被卡在报错上。"""
        devices = [_FakeAudioDeviceInfo(MIC_ENDPOINT, MIC_NAME)]
        found = Pipeline._find_device(devices, MIC_NAME, "麦克风")
        assert found is devices[0].device

    def test_raises_when_device_really_gone(self):
        """设备真拔了才报错（让用户重选是合理的）。"""
        devices = [_FakeAudioDeviceInfo(MIC_ENDPOINT, MIC_NAME)]
        with pytest.raises(RuntimeError, match="当前不可用"):
            Pipeline._find_device(devices, "早就没有了的设备", "麦克风")

    def test_id_match_wins_over_name(self):
        """端点 ID 优先：两个设备同名时不能匹配错。"""
        first = _FakeAudioDeviceInfo(MIC_ENDPOINT, "同名设备")
        second = _FakeAudioDeviceInfo("{0.0.1.00000000}.{other}", "同名设备")
        found = Pipeline._find_device([first, second], "{0.0.1.00000000}.{other}", "麦克风")
        assert found is second.device


class TestRoundTrip:
    """列表 → 保存 → 查找：这条闭环必须能通（缺陷就是闭环断了）。"""

    def test_selected_id_is_findable(self, isolated_config, fake_devices):
        """界面上选到的 id，必须能被 _find_device 找到。"""
        service = ipc_server.BackendService()
        entry = service._cmd_list_audio_devices({})["microphones"][0]  # noqa: SLF001

        # 模拟用户选择：配置里存下这个 id，再让 pipeline 按它找设备
        from voxsub.config_store import ConfigStore

        ConfigStore().update({"mic_device_id": entry["id"]})

        pipeline = Pipeline()
        ipc_server.BackendService._apply_saved_config(pipeline)  # noqa: SLF001
        assert pipeline._mic_device_id == entry["id"]  # noqa: SLF001

        found = Pipeline._find_device(fake_devices[0], pipeline._mic_device_id, "麦克风")  # noqa: SLF001
        assert found is fake_devices[0][0].device

    def test_stale_saved_id_raises_clear_error(self, isolated_config, fake_devices):
        """配置里的设备已不存在时，报错要说清怎么办（不是静默用别的设备）。"""
        from voxsub.config_store import ConfigStore

        ConfigStore().update({"mic_device_id": "已经拔掉的设备"})
        pipeline = Pipeline()
        ipc_server.BackendService._apply_saved_config(pipeline)  # noqa: SLF001

        with pytest.raises(RuntimeError, match="请在设置中重新选择"):
            Pipeline._find_device(fake_devices[0], pipeline._mic_device_id, "麦克风")  # noqa: SLF001
