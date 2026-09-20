"""迁移台账与清理授权 —— 删除规则的逐条测试。

这个文件的定位：把"什么情况下可以删一个目录"**逐条钉死**。每一条拒绝理由
都对应一个测试，将来有人放宽规则会立刻红。

安全不变式（与 migration_ledger 的文档字符串一一对应）：
  1. 清理只认台账记录，不认调用方给的路径。
  2. 只有"迁移完成且三层校验通过"的记录才有资格。
  3. 必须显式 confirm。
  4. 空路径 / 卷根 / 受保护目录 / 重解析点 / 网络路径 一律拒绝。
  5. 源与目标必须不同、且互不包含。
  6. 源不能是、也不能包含应用正在使用的数据根。

测试纪律：所有删除只发生在 pytest 的 tmp_path 里，绝不碰真实目录。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "frontend" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import migration_ledger  # noqa: E402


@pytest.fixture()
def ledger_home(tmp_path, monkeypatch):
    """把台账指到临时目录，避免污染真实 LOCALAPPDATA。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    return tmp_path


@pytest.fixture()
def app_roots(tmp_path):
    """应用"正在使用"的根 —— 测试显式传入，不让它读真实机器配置。"""
    root = tmp_path / "in-use"
    root.mkdir()
    return [root]


def _record(source: Path, target: Path, *, verified: bool = True,
            key: str = "models") -> dict:
    return migration_ledger.build_record(
        key=key, source=str(source), target=str(target), mode="copy",
        verified=verified,
    )


# --------------------------------------------------------------- 台账本身

def test_build_record_has_unique_ids():
    a = _record(Path("C:/a"), Path("C:/b"))
    b = _record(Path("C:/a"), Path("C:/b"))
    assert a["id"] and b["id"] and a["id"] != b["id"]


def test_find_record_matches_only_exact_id(ledger_home):
    record = _record(Path("C:/a"), Path("C:/b"))
    migration_ledger.record_migrations([record])

    assert migration_ledger.find_record(record["id"])["id"] == record["id"]
    assert migration_ledger.find_record(record["id"][:-1]) is None
    assert migration_ledger.find_record("") is None
    assert migration_ledger.find_record(None) is None
    assert migration_ledger.find_record("does-not-exist") is None


def test_ledger_is_bounded(ledger_home):
    """台账不能无界增长 —— 只保留最近 MAX_RECORDS 条。"""
    for index in range(migration_ledger.MAX_RECORDS + 12):
        migration_ledger.record_migrations([_record(Path(f"C:/s{index}"),
                                                    Path(f"C:/t{index}"))])
    kept = migration_ledger.read_ledger()
    assert len(kept) == migration_ledger.MAX_RECORDS
    assert kept[-1]["source"].endswith(f"s{migration_ledger.MAX_RECORDS + 11}")
    assert not any(item["source"].endswith("s0") for item in kept)


def test_corrupt_ledger_reads_as_empty(ledger_home):
    """损坏的台账文件不能让应用起不来 —— 当作空台账。"""
    path = migration_ledger.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    assert migration_ledger.read_ledger() == []


def test_ledger_write_is_atomic_no_tmp_left(ledger_home):
    migration_ledger.record_migrations([_record(Path("C:/a"), Path("C:/b"))])
    target = migration_ledger.ledger_path()
    assert target.is_file()
    json.loads(target.read_text(encoding="utf-8"))  # 必须是完整 JSON
    # 复用了共享的原子写入：既不该留下固定名 .tmp，也不该留下 .part 残骸。
    assert not target.with_suffix(".json.tmp").exists()
    assert not list(target.parent.glob(".migration-ledger.json.*.part"))


def test_active_app_roots_never_comes_from_caller(ledger_home):
    """护栏取值必须来自环境/配置，不能由调用方参数自证。"""
    got = migration_ledger.active_app_roots()
    assert got, "至少要有缓存/日志根，否则护栏形同虚设"
    assert all(str(item) for item in got)


# --------------------------------------------------------------- 逐条拒绝

def test_refuses_missing_record(app_roots):
    with pytest.raises(migration_ledger.CleanupRefused, match="没有对应的迁移记录"):
        migration_ledger.validate_cleanup_target(
            None, confirm=True, active_roots=app_roots)


def test_refuses_without_confirm(tmp_path, app_roots):
    source = tmp_path / "old"
    source.mkdir()
    record = _record(source, tmp_path / "new")
    with pytest.raises(migration_ledger.CleanupRefused, match="缺少用户确认"):
        migration_ledger.validate_cleanup_target(
            record, confirm=False, active_roots=app_roots)


def test_refuses_unverified_record(tmp_path, app_roots):
    """校验没通过的迁移，源目录绝不能进删除流程。"""
    source = tmp_path / "old"
    source.mkdir()
    record = _record(source, tmp_path / "new", verified=False)
    with pytest.raises(migration_ledger.CleanupRefused, match="未通过迁移校验"):
        migration_ledger.validate_cleanup_target(
            record, confirm=True, active_roots=app_roots)


@pytest.mark.parametrize("key", ["", "config_dir", "logs", "whatever"])
def test_refuses_non_migratable_keys(tmp_path, app_roots, key):
    """只有真正会被搬的那几类记录能进删除流程（默认拒绝）。"""
    source = tmp_path / "old"
    source.mkdir()
    record = _record(source, tmp_path / "new", key=key)
    with pytest.raises(migration_ledger.CleanupRefused, match="不可清理"):
        migration_ledger.validate_cleanup_target(
            record, confirm=True, active_roots=app_roots)


@pytest.mark.parametrize("field", ["source", "target"])
def test_refuses_empty_paths(tmp_path, app_roots, field):
    source = tmp_path / "old"
    source.mkdir()
    record = _record(source, tmp_path / "new")
    record[field] = "   "
    with pytest.raises(migration_ledger.CleanupRefused, match="源或目标为空"):
        migration_ledger.validate_cleanup_target(
            record, confirm=True, active_roots=app_roots)


def test_refuses_identical_source_and_target(tmp_path, app_roots):
    source = tmp_path / "same"
    source.mkdir()
    record = _record(source, source)
    with pytest.raises(migration_ledger.CleanupRefused, match="同一目录"):
        migration_ledger.validate_cleanup_target(
            record, confirm=True, active_roots=app_roots)


def test_refuses_volume_root(app_roots):
    """卷根永远不能删 —— 这是"手滑把整个盘删了"的兜底。"""
    record = _record(Path("C:\\"), Path("D:\\VoxSub\\models"))
    with pytest.raises(migration_ledger.CleanupRefused):
        migration_ledger.validate_cleanup_target(
            record, confirm=True, active_roots=app_roots)


@pytest.mark.parametrize("protected", ["C:\\", "C:\\Windows", "C:\\Users",
                                       "C:\\Program Files", "C:\\ProgramData"])
def test_refuses_protected_directories(protected, app_roots):
    record = _record(Path(protected), Path("D:\\VoxSub\\models"))
    with pytest.raises(migration_ledger.CleanupRefused):
        migration_ledger.validate_cleanup_target(
            record, confirm=True, active_roots=app_roots)


@pytest.mark.parametrize("hostile", [r"\\evil-server\share\models",
                                     "\\\\?\\C:\\Windows",
                                     r"\\.\PhysicalDrive0"])
def test_refuses_network_and_device_paths(tmp_path, app_roots, hostile):
    """解析不出可信本地位置的路径一律拒绝（"无法确认安全性"）。"""
    record = _record(Path(hostile), tmp_path / "new")
    with pytest.raises(migration_ledger.CleanupRefused, match="网络/设备路径"):
        migration_ledger.validate_cleanup_target(
            record, confirm=True, active_roots=app_roots)


def test_refuses_source_inside_target(tmp_path, app_roots):
    """源被搬进了目标之内（套娃），按"源"清掉会连带删掉新数据。"""
    target = tmp_path / "new"
    target.mkdir()
    source = target / "src"
    source.mkdir()
    record = _record(source, target)
    with pytest.raises(migration_ledger.CleanupRefused, match="位于目标之内"):
        migration_ledger.validate_cleanup_target(
            record, confirm=True, active_roots=app_roots)


def test_refuses_target_inside_source(tmp_path, app_roots):
    source = tmp_path / "parent"
    source.mkdir()
    target = source / "new"
    target.mkdir()
    record = _record(source, target)
    with pytest.raises(migration_ledger.CleanupRefused, match="目标是源的子目录"):
        migration_ledger.validate_cleanup_target(
            record, confirm=True, active_roots=app_roots)


def test_refuses_source_that_contains_an_active_app_root(tmp_path):
    """核心护栏：不能把应用正在使用的模型根/缓存/日志连锅端掉。"""
    in_use = tmp_path / "VoxSub" / "models"
    in_use.mkdir(parents=True)
    source = tmp_path
    record = _record(source, tmp_path.parent / "new-models")
    with pytest.raises(migration_ledger.CleanupRefused, match="正在使用的数据目录"):
        migration_ledger.validate_cleanup_target(
            record, confirm=True, active_roots=[in_use])


def test_refuses_source_equal_to_an_active_app_root(tmp_path):
    in_use = tmp_path / "VoxSub" / "models"
    in_use.mkdir(parents=True)
    record = _record(in_use, tmp_path / "new-models")
    with pytest.raises(migration_ledger.CleanupRefused, match="正在使用的数据目录"):
        migration_ledger.validate_cleanup_target(
            record, confirm=True, active_roots=[in_use])


def test_refuses_missing_source(tmp_path, app_roots):
    record = _record(tmp_path / "nope", tmp_path / "new")
    with pytest.raises(migration_ledger.CleanupRefused, match="不存在"):
        migration_ledger.validate_cleanup_target(
            record, confirm=True, active_roots=app_roots)


def test_refuses_source_that_is_a_file(tmp_path, app_roots):
    source = tmp_path / "afile"
    source.write_bytes(b"x")
    record = _record(source, tmp_path / "new")
    with pytest.raises(migration_ledger.CleanupRefused, match="不是目录"):
        migration_ledger.validate_cleanup_target(
            record, confirm=True, active_roots=app_roots)


def test_refuses_reparse_point(tmp_path, app_roots):
    """symlink/junction 能把"删 A"变成"删 A 指向的 B"，必须拒绝。"""
    real = tmp_path / "real-target"
    real.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError) as error:  # pragma: no cover
        pytest.skip(f"本机不允许创建符号链接：{error}")

    assert migration_ledger.is_reparse_point(link) is True
    record = _record(link, tmp_path / "new")
    with pytest.raises(migration_ledger.CleanupRefused, match="重解析点"):
        migration_ledger.validate_cleanup_target(
            record, confirm=True, active_roots=app_roots)


def test_reparse_point_detection_is_false_for_plain_dir(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert migration_ledger.is_reparse_point(plain) is False
    assert migration_ledger.is_volume_root(plain) is False


def test_is_inside_string_semantics(tmp_path):
    """is_inside 必须按路径段判断，不能靠前缀字符串（C:\\a 与 C:\\ab 是两回事）。"""
    assert migration_ledger.is_inside(tmp_path / "a" / "b", tmp_path / "a") is True
    assert migration_ledger.is_inside(tmp_path / "ab", tmp_path / "a") is False
    assert migration_ledger.is_inside(tmp_path / "a", tmp_path / "a") is False
    assert migration_ledger.is_inside("", tmp_path) is False


def test_unc_detection(tmp_path):
    assert migration_ledger.is_unc_or_device_path(r"\\server\share") is True
    assert migration_ledger.is_unc_or_device_path("//server/share") is True
    assert migration_ledger.is_unc_or_device_path(tmp_path) is False


# --------------------------------------------------------------- 通过路径

def test_accepts_well_formed_record(tmp_path, app_roots):
    """旧模型目录在用户自己的盘上 —— 这是清理的**主要**用法，必须放行。"""
    source = tmp_path / "old-models"
    source.mkdir()
    (source / "f.bin").write_bytes(b"x")
    target = tmp_path / "new-models"
    target.mkdir()

    resolved = migration_ledger.validate_cleanup_target(
        _record(source, target), confirm=True, active_roots=app_roots)
    assert resolved == source
    assert source.is_dir(), "校验阶段绝不允许真的删东西"


def test_accepts_before_deletion_happens(tmp_path, app_roots):
    """校验通过 ≠ 已经删了：授权与实际删除必须是分开的两步。"""
    source = tmp_path / "old-cache"
    source.mkdir()
    migration_ledger.validate_cleanup_target(
        _record(source, tmp_path / "new", key="cache"),
        confirm=True, active_roots=app_roots)
    assert source.exists()


def test_cleanup_end_to_end_only_removes_recorded_source(tmp_path, app_roots):
    """端到端：只有记录里的那条源被删，旁边的目录必须毫发无损。"""
    import shutil

    source = tmp_path / "old-tools"
    source.mkdir()
    (source / "f.bin").write_bytes(b"x")
    bystander = tmp_path / "bystander"
    bystander.mkdir()
    (bystander / "keep.bin").write_bytes(b"y")
    target = tmp_path / "new-tools"
    target.mkdir()

    resolved = migration_ledger.validate_cleanup_target(
        _record(source, target, key="tools"), confirm=True,
        active_roots=app_roots)
    shutil.rmtree(resolved)

    assert not source.exists()
    assert bystander.is_dir() and (bystander / "keep.bin").exists()
