#!/usr/bin/env python
"""迁移模块的端到端实测 —— 用真实小文件树验证，不碰真模型库。

为什么不用真模型库做测试：11 GB 的复制既慢又可能被中途打断，而且
"测试"本身不该有破坏真实数据的可能性。这里造一个结构与真实库一致的
小样本（含 manifest.json + sha256），验证四件事：

  1. 风险判定：能否正确区分 safe / conditional / exposed
  2. 规划：是否排除共享配置目录、是否标注可重建项
  3. 复制：跨卷复制 + 同卷 rename 两条路径
  4. 校验：三层校验能否抓出缺文件与内容不符

用法：
    python tools/test-migration.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "backend"))

import legacy_migration as lm  # noqa: E402

WORK = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Temp" / "voxsub_migration_test"

passed = 0
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"PASS  {name}" + (f"  — {detail}" if detail else ""))
    else:
        failed += 1
        print(f"FAIL  {name}" + (f"  — {detail}" if detail else ""))


def build_fake_library(root: Path) -> dict[str, str]:
    """造一个结构与真实模型库一致的小样本，返回 {相对路径: sha256}。"""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    digests: dict[str, str] = {}
    layout = {
        "stt/asr-model.bin": b"A" * 4096,
        "translate/mt-model.bin": b"B" * 8192,
        "tts/voice.bin": b"C" * 2048,
        # 故意放在清单之外：真实库里 ocr/ 与 marketplace/ 就属于这种情况，
        # 用来验证"只查 manifest 会漏掉"这个发现。
        "ocr/rapidocr/weights.bin": b"D" * 1024,
    }
    for rel, payload in layout.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    files = {}
    for rel, payload in layout.items():
        if rel.startswith("ocr/"):
            continue  # 不在清单里
        digest = hashlib.sha256(payload).hexdigest()
        digests[rel] = digest
        files[rel] = {"size": len(payload), "sha256": digest, "status": "ready"}

    (root / "manifest.json").write_text(
        json.dumps({"version": 1, "files": files}, indent=2), encoding="utf-8"
    )
    (root / "catalog_installs.json").write_text(
        json.dumps({"version": 1, "models": ["stt/asr-model"]}, indent=2), encoding="utf-8"
    )
    return digests


def main() -> int:
    print("=" * 74)
    print("迁移模块实测（用小样本，不碰真实模型库）")
    print("=" * 74)
    print()

    # ---------------------------------------------------------------- 检测
    print("--- 1. 风险判定 ---")
    legacy = lm.detect_legacy_install()
    check("检测到旧版安装", legacy.found, f"{legacy.display_name} @ {legacy.install_location}")

    checks = lm.assess_storage(legacy)
    by_key = {c.key: c for c in checks}
    risk = lm.overall_risk(checks)
    check("有数据目录被识别", len(checks) > 0, f"{len(checks)} 项，整体 {risk}")

    if legacy.install_location:
        # 这两个目录在 installer.iss 的 [InstallDelete] 里 —— 卸载必然删除。
        # 断言用"包含"而不是"位置"，顺序在源码里可能调整。
        check("tools 属删除名单", "tools" in lm.INSTALL_DELETE_DIRS,
              str(lm.INSTALL_DELETE_DIRS))
        check("models 不在删除名单（卸载不会删模型库）",
              "models" not in {d.lower() for d in lm.INSTALL_DELETE_DIRS},
              str(lm.INSTALL_DELETE_DIRS))

    # ---------------------------------------------------------------- 规划
    print()
    print("--- 2. 迁移规划 ---")
    target = WORK / "target"
    steps = lm.plan_migration(checks, target)
    keys = {s.key for s in steps}
    check("排除共享配置目录 config_dir", "config_dir" not in keys, f"计划项：{sorted(keys)}")
    check("排除日志目录 logs", "logs" not in keys, "")

    rebuildable = [s for s in steps if s.rebuildable]
    check("tools 被标为可重建（默认不迁）", any(s.key == "tools" for s in rebuildable),
          f"{[s.key for s in rebuildable]}")

    if steps:
        biggest = steps[0]
        check("最大项排在最前", biggest.bytes == max(s.bytes for s in steps),
              f"{biggest.key} {biggest.bytes / 2**20:.2f} MB")

    # ---------------------------------------------------------------- 复制与校验
    print()
    print("--- 3. 复制与三层校验 ---")

    source = WORK / "fake-models"
    digests = build_fake_library(source)
    dest = WORK / "copied-models"

    if dest.exists():
        shutil.rmtree(dest)

    # 模拟复制（同卷与跨卷在数据层面等价；这里验证的是"复制后校验"）
    shutil.copytree(source, dest)

    result = lm.verify_copy(source, dest)
    check("校验通过（完整副本）", result["ok"], json.dumps(result["layers"]["totals"], ensure_ascii=False)[:80])
    check("层 1 文件数与字节一致", bool(result["layers"]["totals"]["ok"]),
          f"{result['layers']['totals']['source']}")
    check("层 2 manifest sha256 通过", bool(result["layers"]["manifest"]["ok"]),
          f"校验 {result['layers']['manifest'].get('checked')} 个文件")

    # 制造"缺文件"故障
    broken = WORK / "broken-models"
    if broken.exists():
        shutil.rmtree(broken)
    shutil.copytree(source, broken)
    (broken / "translate" / "mt-model.bin").unlink()

    result = lm.verify_copy(source, broken)
    check("缺文件被层 1 抓到", not result["layers"]["totals"]["ok"], "")
    check("缺文件被层 2 抓到", not result["layers"]["manifest"]["ok"],
          f"缺失 {len(result['layers']['manifest'].get('missing', []))} 个")
    check("整体校验判定为失败", result["ok"] is False, "")

    # 制造"内容被改"故障（字节数相同，只有哈希能发现）
    tampered = WORK / "tampered-models"
    if tampered.exists():
        shutil.rmtree(tampered)
    shutil.copytree(source, tampered)
    path = tampered / "stt" / "asr-model.bin"
    path.write_bytes(b"Z" * 4096)  # 同样 4096 字节，内容不同

    result = lm.verify_copy(source, tampered)
    check("内容被改：层 1 看不出来（字节数相同）", bool(result["layers"]["totals"]["ok"]), "")
    check("内容被改：层 2 抓到哈希不符", not result["layers"]["manifest"]["ok"],
          f"不符 {len(result['layers']['manifest'].get('mismatch', []))} 个")
    check("内容被改：整体判定失败", result["ok"] is False, "")

    # ---------------------------------------------------------------- 快照
    print()
    print("--- 4. 模型快照（逃生舱）---")
    snap = lm.write_model_snapshot(source)
    check("快照已写入", Path(snap["path"]).is_file(), snap["path"])
    data = json.loads(Path(snap["path"]).read_text(encoding="utf-8"))
    check("快照记录了 manifest", bool(data.get("manifest")), "")
    check("快照记录了顶层目录体积", len(data.get("entries", [])) > 0,
          f"{[e['name'] for e in data.get('entries', [])]}")

    # ---------------------------------------------------------------- 真实迁移
    print()
    print("--- 5. 真实迁移执行（小样本）---")
    import shutil as _shutil

    mig_source = WORK / "mig-source"
    mig_target = WORK / "mig-target"
    _shutil.rmtree(mig_source, ignore_errors=True)
    _shutil.rmtree(mig_target, ignore_errors=True)
    build_fake_library(mig_source)

    # 用 capture_expectation + rename + verify_against_expectation 走一遍
    # 这正是 ipc_server._cmd_start_migration 对同卷场景的实际路径
    expectation = lm.capture_expectation(mig_source)
    check("搬迁前记录了期望值", expectation["files"] > 0,
          f"{expectation['bytes']} 字节 / {expectation['files']} 文件 / {len(expectation['hashes'])} 条哈希")

    mig_target.parent.mkdir(parents=True, exist_ok=True)
    mig_source.rename(mig_target)
    check("同卷 rename 后源已消失", not mig_source.exists(), "")
    check("同卷 rename 后目标存在", mig_target.is_dir(), "")

    result = lm.verify_against_expectation(expectation, mig_target)
    check("rename 后校验通过", result["ok"],
          f"层1 {result['layers']['totals']['ok']} / 层2 {result['layers']['manifest']['ok']}")

    # 目标被破坏时应能抓到（模拟迁移中出错）
    (mig_target / "tts" / "voice.bin").unlink()
    result = lm.verify_against_expectation(expectation, mig_target)
    check("目标缺文件被抓到", result["ok"] is False,
          f"缺失 {len(result['layers']['manifest'].get('missing', []))} 个")

    # 快照：即使模型库整个消失，快照仍可读
    snap_path = Path(lm.snapshot_path())
    check("快照独立于模型库存在", snap_path.is_file(), str(snap_path))

    # ------------------------------------------------- IPC 命令路径
    print()
    print("--- 6. IPC start_migration 命令（假数据）---")
    import subprocess

    ipc_source = WORK / "ipc-source"
    ipc_target = WORK / "ipc-target"
    _shutil.rmtree(ipc_source, ignore_errors=True)
    _shutil.rmtree(ipc_target, ignore_errors=True)
    build_fake_library(ipc_source)

    voxsub = Path(os.environ.get("VOXSUB_ROOT", r"D:/OneDrive/app_dve/VoxSub"))
    python = voxsub / ".venv" / "Scripts" / "python.exe"
    server = ROOT / "backend" / "ipc_server.py"

    # start_migration 成功后会更新 models_root —— 那是共享配置，测试不能留下痕迹。
    # 先备份，无论测试成败都还原。
    config_file = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VoxSub" / "config.json"
    config_backup = None
    if config_file.is_file():
        config_backup = config_file.read_bytes()

    payload = json.dumps({
        "id": 1,
        "command": "start_migration",
        "args": {"steps": [{
            "key": "models",
            "source": str(ipc_source),
            "target": str(ipc_target),
        }]},
    }, ensure_ascii=False) + "\n"
    payload += json.dumps({"id": 9, "command": "shutdown", "args": None}) + "\n"

    try:
        proc = subprocess.run(
            [str(python), str(server)],
            input=payload.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
            env={**os.environ, "PYTHONPATH": "", "PYTHONHOME": "", "PYTHONUNBUFFERED": "1"},
        )

        answer = None
        for raw in proc.stdout.decode("utf-8", "replace").splitlines():
            raw = raw.strip()
            if raw.startswith("{") and '"id": 1' in raw:
                answer = json.loads(raw)

        if answer is None:
            check("start_migration 有应答", False, "没有收到 id=1 的应答")
        else:
            check("start_migration 执行成功", answer.get("ok") is True,
                  str(answer.get("error"))[:80] if not answer.get("ok") else "")
            data = answer.get("data") or {}
            check("返回 done 列表", len(data.get("done") or []) == 1,
                  f"{[d['key'] for d in data.get('done') or []]}")
            check("迁移后目标存在", ipc_target.is_dir(), str(ipc_target))
            check("同卷走原子改名",
                  (data.get("done") or [{}])[0].get("mode") == "move_same_volume",
                  str((data.get("done") or [{}])[0].get("mode")))
            check("配置指针已更新", "models_root" in (data.get("configUpdates") or {}),
                  str(data.get("configUpdates")))
    finally:
        # 还原共享配置，确保测试零副作用
        if config_backup is not None:
            config_file.write_bytes(config_backup)
            restored = json.loads(config_file.read_text(encoding="utf-8"))
            check("测试后配置已还原", restored.get("models_root") != str(ipc_target),
                  f"models_root = {restored.get('models_root')}")

    print()
    print("=" * 74)
    print(f"{passed} 通过 / {failed} 失败 / 共 {passed + failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
