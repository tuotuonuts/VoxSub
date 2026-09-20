"""配置版本与未知字段的兼容性测试（工作单 §3.5 / 缺陷 #12）。

要钉死的三件事：

  1. **只能迁移已知的旧版本。**遇到比程序更新的配置版本，必须拒绝改写 ——
     静默降级会把新版本写入的字段抹掉，用户看到的是"设置莫名其妙丢了"。
  2. **未知字段保留在原始持久化数据里**，不作为已生效字段执行，也不能因为
     一次保存就被丢掉。
  3. 配置损坏时先备份再回落，别让下一次保存把用户设置永久覆盖掉。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voxsub.config_store import (  # noqa: E402
    CONFIG_VERSION, ConfigStore, ConfigVersionTooNew,
)


@pytest.fixture()
def store(tmp_path):
    return ConfigStore(tmp_path / "config.json")


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------- 版本比程序新：拒绝

def test_future_version_puts_store_into_readonly(store):
    _write(store.path, {"config_version": CONFIG_VERSION + 3, "theme": "dark"})

    data = store.load()

    assert data["config_version"] == CONFIG_VERSION
    assert store.locked_reason, "版本过新必须进入只读保护"
    assert str(CONFIG_VERSION + 3) in store.locked_reason


def test_future_version_save_is_refused_and_file_untouched(store):
    """核心：不静默降级、不破坏性保存 —— 文件必须一个字节都没变。"""
    original = {"config_version": CONFIG_VERSION + 3, "theme": "dark",
                "future_only_key": {"nested": [1, 2, 3]}}
    _write(store.path, original)
    before = store.path.read_bytes()

    with pytest.raises(ConfigVersionTooNew):
        store.set("theme", "light")
    with pytest.raises(ConfigVersionTooNew):
        store.update({"theme": "light"})
    with pytest.raises(ConfigVersionTooNew):
        store.save(store.load())

    assert store.path.read_bytes() == before, "禁止把配置降级写坏"


def test_future_version_does_not_read_its_values(store):
    """不认识的版本，其值也不能被当成已生效配置执行。"""
    _write(store.path, {"config_version": CONFIG_VERSION + 1, "theme": "dark"})
    defaults = dict(ConfigStore.DEFAULTS)
    assert store.load()["theme"] == defaults["theme"]


def test_newer_version_is_detected_even_without_other_keys(store):
    _write(store.path, {"config_version": CONFIG_VERSION + 1})
    store.load()
    assert store.locked_reason


# ------------------------------------------------------- 已知旧版本：正常迁移

def test_missing_version_is_treated_as_oldest(store):
    _write(store.path, {"api_key": "k", "base_url": "https://x.example",
                        "model": "m"})

    data = store.load()

    assert data["config_version"] == CONFIG_VERSION
    assert data["translate_api_key"] == "k"
    assert data["translate_base_url"] == "https://x.example"
    assert data["translate_model"] == "m"
    assert store.locked_reason == ""


@pytest.mark.parametrize("bad", ["2", 2.5, True, None, [], {}])
def test_version_of_wrong_type_is_treated_as_oldest(store, bad):
    _write(store.path, {"config_version": bad, "api_key": "k"})
    data = store.load()
    assert data["config_version"] == CONFIG_VERSION
    assert data["translate_api_key"] == "k"


def test_current_version_round_trips(store):
    store.set("theme", "dark")
    assert store.load()["theme"] == "dark"
    assert store.load()["config_version"] == CONFIG_VERSION
    assert store.locked_reason == ""


def test_version_zero_migrates_and_saves_at_current_version(store):
    _write(store.path, {"api_key": "legacy"})
    store.load()
    store.set("theme", "light")
    on_disk = json.loads(store.path.read_text(encoding="utf-8"))
    assert on_disk["config_version"] == CONFIG_VERSION
    assert on_disk["translate_api_key"] == "legacy"


# ------------------------------------------------------- 未知字段保留

def test_unknown_fields_survive_a_save(store):
    """当前版本不认识的字段：不执行，但绝不能因为一次保存就被抹掉。"""
    _write(store.path, {
        "config_version": CONFIG_VERSION,
        "theme": "dark",
        "brand_new_option": "keep-me",
        "another_future_key": {"a": [1, 2]},
    })

    store.set("theme", "light")

    on_disk = json.loads(store.path.read_text(encoding="utf-8"))
    assert on_disk["theme"] == "light"
    assert on_disk["brand_new_option"] == "keep-me"
    assert on_disk["another_future_key"] == {"a": [1, 2]}


def test_unknown_fields_are_not_effective_configuration(store):
    """未知字段保留在原始数据里，但不作为已生效字段返回。"""
    _write(store.path, {"config_version": CONFIG_VERSION,
                        "brand_new_option": "keep-me"})

    data = store.load()

    assert "brand_new_option" not in data
    assert store.load_raw()["brand_new_option"] == "keep-me"


def test_load_raw_exposes_the_untouched_file(store):
    _write(store.path, {"config_version": CONFIG_VERSION, "extra": 1})
    assert store.load_raw() == {"config_version": CONFIG_VERSION, "extra": 1}


def test_load_raw_is_empty_for_broken_file(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ not json", encoding="utf-8")
    assert store.load_raw() == {}


# ------------------------------------------------------- 损坏配置

def test_corrupt_config_is_backed_up_before_falling_back(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text('{"theme": "dark"', encoding="utf-8")

    data = store.load()

    assert data["theme"] == ConfigStore.DEFAULTS["theme"], "坏文件要回落默认值"
    backups = list(store.path.parent.glob("config.json.corrupt-*"))
    assert len(backups) == 1, "回落前必须留一份，否则下次保存会把用户设置永久覆盖"
    assert backups[0].read_text(encoding="utf-8") == '{"theme": "dark"'


def test_corrupt_backup_happens_only_once(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ broken", encoding="utf-8")

    for _ in range(5):
        store.load()

    backups = list(store.path.parent.glob("config.json.corrupt-*"))
    assert len(backups) == 1, "反复读不该堆出一堆备份文件"


def test_corrupt_config_can_still_be_written_after_backup(store):
    """备份之后要能正常恢复使用，不能把程序永久卡在只读。"""
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ broken", encoding="utf-8")

    store.set("theme", "dark")

    assert store.load()["theme"] == "dark"
    assert store.locked_reason == ""


def test_empty_file_falls_back_to_defaults(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("", encoding="utf-8")
    assert store.load()["config_version"] == CONFIG_VERSION


def test_direct_save_on_a_future_version_file_is_refused(store):
    """**裸 save 也必须被拒**（回归：这条以前是个洞）。

    审查发现：原来的用例写的是 `store.save(store.load())` —— `load()` 先把实例置入
    只读态，于是用例只验证了"load 过的实例拒绝 save"。而 `save()` 是公开写入口，
    新建实例直接 save 时 `_locked_reason` 还是空的，会把未来版本的配置按旧 schema
    归一化后写回 —— 正是 #12 要禁止的静默降级，绿色却没覆盖到。
    """
    _write(store.path, {"config_version": CONFIG_VERSION + 3, "theme": "dark",
                        "future_only_key": "keep-me"})
    before = store.path.read_bytes()

    with pytest.raises(ConfigVersionTooNew):
        store.save({"theme": "light"})   # 注意：不先 load

    assert store.path.read_bytes() == before, "裸 save 把未来版本的配置降级写坏了"


def test_direct_save_still_works_for_a_current_version_file(store):
    """修好以后正常路径不能坏：当前版本的配置，裸 save 照样能写。"""
    store.save({"theme": "dark"})

    on_disk = json.loads(store.path.read_text(encoding="utf-8"))
    assert on_disk["theme"] == "dark"
    assert on_disk["config_version"] == CONFIG_VERSION
