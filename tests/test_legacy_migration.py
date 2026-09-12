"""旧版迁移模块的单元测试。

覆盖 frontend/backend/legacy_migration.py —— 检测旧版安装、评估数据风险、
规划迁移、校验复制、模型快照。

为什么这些测试重要：迁移功能直接动用户的模型库（数 GB）。逻辑错了不是
"功能不可用"，而是"数据被搬坏"。所以每个判定分支都必须有测试钉住。

测试全部用 tmp_path 造假数据，绝不触碰真实模型库。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# legacy_migration.py 在 frontend/backend/ 下，不是包的一部分
BACKEND_DIR = Path(__file__).resolve().parents[1] / "frontend" / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import legacy_migration as lm  # noqa: E402


# ------------------------------------------------------------------ 辅助

def make_fake_models(root: Path, *, with_manifest: bool = True) -> dict[str, str]:
    """造一个结构与真实模型库一致的样本，返回 {相对路径: sha256}。"""
    import hashlib

    root.mkdir(parents=True, exist_ok=True)
    layout = {
        "stt/asr-model.bin": b"A" * 4096,
        "translate/mt-model.bin": b"B" * 8192,
        # 故意放在清单之外：真实库里 ocr/ 就不在 manifest 里，
        # 用来验证"只查 manifest 会漏"这个已知事实。
        "ocr/weights.bin": b"D" * 1024,
    }
    digests: dict[str, str] = {}
    files: dict[str, dict] = {}
    for rel, payload in layout.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        digests[rel] = digest
        if with_manifest and not rel.startswith("ocr/"):
            files[rel] = {"size": len(payload), "sha256": digest, "status": "ready"}

    if with_manifest:
        (root / "manifest.json").write_text(
            json.dumps({"version": 1, "files": files}), encoding="utf-8")
    return digests


def storage(key: str, path: Path, *, risk: str, exists: bool = True,
            size: int = 1024, inside: bool = True) -> lm.StorageCheck:
    return lm.StorageCheck(
        key=key, path=str(path), exists=exists, bytes=size, file_count=1,
        inside_install=inside, risk=risk, purpose="", detail="",
    )


# ------------------------------------------------------------------ 路径工具

def test_is_inside_detects_child_and_rejects_sibling(tmp_path):
    parent = tmp_path / "install"
    parent.mkdir()
    child = parent / "Models"
    child.mkdir()
    sibling = tmp_path / "elsewhere"
    sibling.mkdir()

    assert lm._is_inside(child, parent) is True
    # 边界：自己算在自己之内（用于判定"目标是源的子目录"这类场景）
    assert lm._is_inside(parent, parent) is True
    assert lm._is_inside(sibling, parent) is False


def test_is_inside_rejects_prefix_collision(tmp_path):
    """`Models-old` 不该被当成 `Models` 的子目录 —— 纯字符串前缀会误判。"""
    install = tmp_path / "app"
    install.mkdir()
    real = install / "Models"
    real.mkdir()
    decoy = install / "Models-old"
    decoy.mkdir()

    assert lm._is_inside(real, install) is True
    assert lm._is_inside(decoy, install) is True   # 都在 install 下
    # 关键：decoy 不在 real 之内（前缀相同但不是子路径）
    assert lm._is_inside(decoy, real) is False


def test_same_volume_compares_drive(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    assert lm._same_volume(a, b) is True  # 同一 tmp 目录必然是同一卷


def test_dir_stats_counts_files_and_bytes(tmp_path):
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "one.bin").write_bytes(b"x" * 100)
    (root / "sub" / "two.bin").write_bytes(b"y" * 250)

    total, count = lm._dir_stats(root)
    assert count == 2
    assert total == 350


def test_dir_stats_returns_zero_for_missing_path(tmp_path):
    total, count = lm._dir_stats(tmp_path / "does-not-exist")
    assert (total, count) == (0, 0)


# ------------------------------------------------------------------ 风险判定

def test_overall_risk_ignores_rebuildable_directories():
    """tools 在安装器的删除名单里，但程序能自行重建 —— 不该让整体判成 exposed。

    这条规则来自实测：早期版本把 tools 算作关键，导致向导对所有用户喊
    "必须先迁移"，而真正重要的模型库其实只是 conditional。
    """
    checks = [
        storage("tools", Path("D:/app/tools"), risk="exposed"),
        storage("models", Path("D:/app/Models"), risk="conditional"),
    ]
    assert lm.overall_risk(checks) == "conditional"


def test_overall_risk_reports_exposed_for_real_data():
    checks = [
        storage("models", Path("D:/app/Models"), risk="exposed"),
    ]
    assert lm.overall_risk(checks) == "exposed"


def test_overall_risk_safe_when_everything_outside_install():
    checks = [storage("models", Path("D:/Models"), risk="safe", inside=False)]
    assert lm.overall_risk(checks) == "safe"


def test_overall_risk_takes_highest_among_real_data():
    checks = [
        storage("models", Path("D:/app/Models"), risk="conditional"),
        storage("cache", Path("D:/app/Cache"), risk="exposed"),
    ]
    assert lm.overall_risk(checks) == "exposed"


def test_assess_storage_flags_exposed_for_install_delete_dirs(tmp_path, monkeypatch):
    """位于 [InstallDelete] 名单内的目录必须判为 exposed。"""
    install = tmp_path / "VoxSub"
    (install / "tools").mkdir(parents=True)
    (install / "tools" / "llama.exe").write_bytes(b"x" * 1000)
    (install / "Models").mkdir(parents=True)
    (install / "Models" / "m.bin").write_bytes(b"y" * 2000)

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))

    legacy = lm.LegacyInstall(found=True, install_location=str(install))
    checks = lm.assess_storage(legacy)
    by_key = {c.key: c for c in checks}

    assert by_key["tools"].risk == "exposed", "tools 在删除名单里，必须判 exposed"
    assert by_key["models"].risk == "conditional", "Models 不在名单里，是 conditional"


def test_assess_storage_marks_outside_install_as_safe(tmp_path, monkeypatch):
    install = tmp_path / "VoxSub"
    install.mkdir()
    outside = tmp_path / "D_drive" / "Models"
    outside.mkdir(parents=True)
    (outside / "m.bin").write_bytes(b"z" * 500)

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.setattr(lm, "read_legacy_config",
                        lambda: {"models_root": str(outside)})

    legacy = lm.LegacyInstall(found=True, install_location=str(install))
    checks = lm.assess_storage(legacy)
    by_key = {c.key: c for c in checks}

    assert by_key["models"].risk == "safe"
    assert by_key["models"].inside_install is False


# ------------------------------------------------------------------ 迁移规划

def test_plan_migration_never_moves_shared_config(tmp_path):
    """共享配置目录永不迁移 —— 搬走它两版都读不到配置，属灾难性错误。"""
    checks = [
        storage("models", tmp_path / "src" / "Models", risk="conditional"),
        storage("config_dir", tmp_path / "src" / "config", risk="safe"),
        storage("logs", tmp_path / "src" / "logs", risk="safe"),
    ]
    for check in checks:
        Path(check.path).mkdir(parents=True)
        (Path(check.path) / "f.bin").write_bytes(b"x" * 100)

    steps = lm.plan_migration(checks, tmp_path / "target")
    keys = {s.key for s in steps}

    assert "config_dir" not in keys, "共享配置目录绝不能迁移"
    assert "logs" not in keys, "共享日志目录绝不能迁移"
    assert "models" in keys


def test_plan_migration_marks_rebuildable_directories(tmp_path):
    """程序能自行重建的目录要标出来，供界面默认不勾选。"""
    src = tmp_path / "src" / "tools"
    src.mkdir(parents=True)
    (src / "llama-server.exe").write_bytes(b"x" * 5000)

    steps = lm.plan_migration([storage("tools", src, risk="exposed")], tmp_path / "target")
    assert len(steps) == 1
    assert steps[0].rebuildable is True
    assert "默认不迁移" in steps[0].note


def test_plan_migration_chooses_atomic_move_for_same_volume(tmp_path):
    src = tmp_path / "src" / "Models"
    src.mkdir(parents=True)
    (src / "m.bin").write_bytes(b"x" * 100)

    steps = lm.plan_migration([storage("models", src, risk="conditional")],
                              tmp_path / "target")
    assert steps[0].mode == "move_same_volume"


def test_plan_migration_skips_source_already_under_target(tmp_path):
    """源已在目标之下时不该再规划 —— 否则会规划出把目录搬进自己的操作。"""
    target = tmp_path / "target"
    src = target / "Models"
    src.mkdir(parents=True)
    (src / "m.bin").write_bytes(b"x" * 100)

    steps = lm.plan_migration([storage("models", src, risk="conditional")], target)
    assert steps == []


def test_plan_migration_sorts_by_size_descending(tmp_path):
    small = tmp_path / "src" / "cache"
    small.mkdir(parents=True)
    (small / "s.bin").write_bytes(b"x" * 100)
    big = tmp_path / "src" / "Models"
    big.mkdir(parents=True)
    (big / "b.bin").write_bytes(b"y" * 100000)

    steps = lm.plan_migration([
        storage("cache", small, risk="conditional", size=100),
        storage("models", big, risk="conditional", size=100000),
    ], tmp_path / "target")

    assert steps[0].key == "models", "大项应排前面，便于早暴露磁盘空间不足"


# ------------------------------------------------------------------ 校验

def test_capture_expectation_reads_manifest_hashes(tmp_path):
    digests = make_fake_models(tmp_path / "Models")
    expectation = lm.capture_expectation(tmp_path / "Models")

    # 3 个模型文件 + manifest.json 本身 = 4
    assert expectation["files"] == 4
    assert expectation["bytes"] > 0
    # manifest 里只有两个（ocr 不在清单内）
    assert len(expectation["hashes"]) == 2
    assert expectation["hashes"]["stt/asr-model.bin"] == digests["stt/asr-model.bin"]


def test_verify_against_expectation_detects_missing_file(tmp_path):
    root = tmp_path / "Models"
    make_fake_models(root)
    expectation = lm.capture_expectation(root)

    # 删掉一个清单内文件
    (root / "translate" / "mt-model.bin").unlink()

    result = lm.verify_against_expectation(expectation, root)
    assert result["ok"] is False
    assert "translate/mt-model.bin" in result["layers"]["manifest"]["missing"]


def test_verify_against_expectation_detects_tampered_content(tmp_path):
    """字节数相同但内容不同 —— 只有哈希层能发现。"""
    root = tmp_path / "Models"
    make_fake_models(root)
    expectation = lm.capture_expectation(root)

    # 保持长度不变，只换内容
    (root / "stt" / "asr-model.bin").write_bytes(b"Z" * 4096)

    result = lm.verify_against_expectation(expectation, root)
    assert result["layers"]["totals"]["ok"] is True, "字节数没变，层1看不出来"
    assert result["layers"]["manifest"]["ok"] is False
    assert "stt/asr-model.bin" in result["layers"]["manifest"]["mismatch"]
    assert result["ok"] is False


def test_verify_against_expectation_passes_for_intact_copy(tmp_path):
    src = tmp_path / "src"
    make_fake_models(src)
    expectation = lm.capture_expectation(src)

    dst = tmp_path / "dst"
    import shutil
    shutil.copytree(src, dst)

    result = lm.verify_against_expectation(expectation, dst)
    assert result["ok"] is True
    assert result["layers"]["totals"]["ok"] is True
    assert result["layers"]["manifest"]["ok"] is True


def test_verify_against_expectation_rejects_missing_target(tmp_path):
    src = tmp_path / "src"
    make_fake_models(src)
    expectation = lm.capture_expectation(src)

    result = lm.verify_against_expectation(expectation, tmp_path / "nonexistent")
    assert result["ok"] is False
    assert result["layers"]["totals"]["ok"] is False


def test_verify_copy_compares_two_live_trees(tmp_path):
    src = tmp_path / "src"
    make_fake_models(src)
    dst = tmp_path / "dst"
    import shutil
    shutil.copytree(src, dst)

    assert lm.verify_copy(src, dst)["ok"] is True

    (dst / "stt" / "asr-model.bin").unlink()
    assert lm.verify_copy(src, dst)["ok"] is False


def test_verify_copy_tolerates_missing_manifest(tmp_path):
    """没有 manifest 的目录不该让校验崩溃，只是层2无从校验。"""
    src = tmp_path / "src"
    make_fake_models(src, with_manifest=False)
    dst = tmp_path / "dst"
    import shutil
    shutil.copytree(src, dst)

    result = lm.verify_copy(src, dst)
    # 层1 应通过（文件数与字节一致），层2 因为没清单而报错
    assert result["layers"]["totals"]["ok"] is True
    assert result["layers"]["manifest"]["ok"] is False


# ------------------------------------------------------------------ 快照

def test_write_model_snapshot_records_structure(tmp_path, monkeypatch):
    root = tmp_path / "Models"
    make_fake_models(root)
    (root / "catalog_installs.json").write_text(
        json.dumps({"version": 1, "models": ["stt/asr-model"]}), encoding="utf-8")

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    result = lm.write_model_snapshot(root)

    assert Path(result["path"]).is_file()
    snapshot = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert snapshot["root"] == str(root)
    assert snapshot["exists"] is True
    assert snapshot["manifest"] is not None
    assert snapshot["catalog_installs"] is not None
    names = {e["name"] for e in snapshot["entries"]}
    assert {"stt", "translate", "ocr"} <= names


def test_write_model_snapshot_survives_missing_root(tmp_path, monkeypatch):
    """模型库整个丢失时，快照仍要能写出来 —— 那正是它存在的意义。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    result = lm.write_model_snapshot(tmp_path / "gone")

    snapshot = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert snapshot["exists"] is False
    assert snapshot["entries"] == []


# ------------------------------------------------------------------ 迁移状态

def test_should_offer_wizard_respects_dismissed(tmp_path, monkeypatch):
    """用户明确跳过之后不该反复打扰。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    lm.write_state(dismissed=True)

    offered, state = lm.should_offer_wizard()
    assert offered is False
    assert state["dismissed"] is True


def test_should_offer_wizard_respects_completed(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    lm.write_state(completed=True)

    offered, _ = lm.should_offer_wizard()
    assert offered is False


def test_should_offer_wizard_skips_when_risk_is_safe(tmp_path, monkeypatch):
    """数据本来就在安全位置的不该被打扰。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.setattr(lm, "detect_legacy_install",
                        lambda: lm.LegacyInstall(found=True, install_location="D:/app"))
    monkeypatch.setattr(lm, "assess_storage",
                        lambda legacy: [storage("models", Path("D:/M"), risk="safe", inside=False)])

    offered, _ = lm.should_offer_wizard()
    assert offered is False


def test_should_offer_wizard_offers_when_risk_present(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.setattr(lm, "detect_legacy_install",
                        lambda: lm.LegacyInstall(found=True, install_location="D:/app"))
    monkeypatch.setattr(lm, "assess_storage",
                        lambda legacy: [storage("models", Path("D:/app/Models"), risk="conditional")])

    offered, _ = lm.should_offer_wizard()
    assert offered is True


def test_write_state_is_atomic_and_merges(tmp_path, monkeypatch):
    """状态写入要原子（避免半个 JSON），且多次写入是合并而非覆盖。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    lm.write_state(dismissed=True)
    state = lm.write_state(completed=True)

    assert state["dismissed"] is True, "第二次写入不该丢掉第一次的字段"
    assert state["completed"] is True
    # 不该留下 .tmp 残留
    leftovers = list((tmp_path / "AppData" / "VoxSub").glob("*.tmp"))
    assert leftovers == []
