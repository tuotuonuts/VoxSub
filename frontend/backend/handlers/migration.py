"""旧版迁移：探测、规划、执行、校验、清理授权（IPC 适配层的一个业务域）。

方法体是从 ipc_server.py **原样搬移**过来的，只改了所在文件；
共享的协议与工具依赖收在 ipc_protocol / ipc_support 里。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ipc_protocol import _cancel_requested, _event
from ipc_support import _free_bytes, _resolve_models_root


class MigrationHandlers:
    """旧版迁移：探测、规划、执行、校验、清理授权。"""

    def _cmd_detect_legacy(self, args: dict[str, Any]) -> dict[str, Any]:
        """检测旧版（Qt 版）安装与数据风险。只读，不写任何文件。"""
        from legacy_migration import (  # noqa: PLC0415
            assess_storage, asdict, detect_legacy_install, overall_risk, read_state,
        )

        legacy = detect_legacy_install()
        checks = assess_storage(legacy)
        state = read_state()
        return {
            "legacy": asdict(legacy),
            "storage": [asdict(c) for c in checks],
            "overallRisk": overall_risk(checks),
            "state": state,
        }

    def _cmd_migration_decision(self, args: dict[str, Any]) -> dict[str, Any]:
        """记录用户对迁移向导的决定（跳过 / 完成），避免反复打扰。"""
        from legacy_migration import write_state  # noqa: PLC0415

        decision = str(args.get("decision", ""))
        if decision == "dismiss":
            return write_state(dismissed=True)
        if decision == "complete":
            return write_state(completed=True, dismissed=True)
        if decision == "reset":
            return write_state(dismissed=False, completed=False)
        raise ValueError(f"未知决定：{decision}")

    def _cmd_plan_migration(self, args: dict[str, Any]) -> dict[str, Any]:
        """规划迁移步骤（纯计算，不碰文件系统）。"""
        from legacy_migration import (  # noqa: PLC0415
            assess_storage, asdict, detect_legacy_install, plan_migration,
        )

        legacy = detect_legacy_install()
        checks = assess_storage(legacy)
        target = str(args.get("target_root") or "").strip()
        if not target:
            drive = Path(legacy.install_location).drive if legacy.install_location else "D:"
            target = f"{drive}\\VoxSub\\Data"
        keys = args.get("keys")
        steps = plan_migration(checks, target, keys=keys or None)
        return {
            "targetRoot": target,
            "steps": [asdict(s) for s in steps],
            "totalBytes": sum(s.bytes for s in steps),
            "freeBytes": _free_bytes(target),
        }

    def _cmd_write_model_snapshot(self, args: dict[str, Any]) -> dict[str, Any]:
        """写入模型快照（逃生舱：数据丢了也知道曾经有什么）。"""
        from legacy_migration import write_model_snapshot  # noqa: PLC0415

        root = str(args.get("models_root") or "") or str(_resolve_models_root())
        return write_model_snapshot(root)

    def _cmd_verify_copy(self, args: dict[str, Any]) -> dict[str, Any]:
        """校验一份复制是否完整（三层校验）。"""
        from legacy_migration import verify_copy  # noqa: PLC0415

        return verify_copy(Path(str(args.get("source", ""))), Path(str(args.get("target", ""))))

    def _cmd_start_migration(self, args: dict[str, Any]) -> dict[str, Any]:
        """执行迁移。**复制优先，绝不删源。**

        安全纪律（本命令的最高优先级）：
          · 同卷用原子 rename；跨卷用复制
          · 搬迁前先记录源目录的"期望状态"（字节数/文件数/manifest 哈希）
          · 搬迁后拿目标与期望比对 —— 不能拿 source 比，同卷 rename 后 source 已经没了
          · 校验不过就保留现场并报错，不删任何东西
          · 源目录的清理必须由用户单独确认（另有命令），不在本命令里做
        """
        import shutil  # noqa: PLC0415
        import time  # noqa: PLC0415

        from legacy_migration import (  # noqa: PLC0415
            _same_volume, capture_expectation, verify_against_expectation,
        )

        steps = args.get("steps") or []
        if not steps:
            raise ValueError("没有要迁移的项目")

        started = time.monotonic()
        done: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []

        for index, step in enumerate(steps):
            # 协作式取消：迁移是"每一步之间"可以安全停下的长任务。
            # 不在这里检查的话，用户点了取消仍要等全部步骤跑完 ——
            # 那就违背了"停止和取消不排在普通耗时任务后面"。
            if _cancel_requested():
                print("[migration] 收到取消请求，已在步骤边界停下", file=sys.stderr)
                _event("migration", phase="cancelled", completed=len(done),
                       total=len(steps))
                break

            source = Path(str(step.get("source", "")))
            target = Path(str(step.get("target", "")))
            key = str(step.get("key", f"item{index}"))

            if not source.is_dir():
                failures.append({"key": key, "error": f"源目录不存在：{source}"})
                continue
            if target.exists() and any(target.iterdir()):
                failures.append({"key": key, "error": f"目标已存在且非空：{target}"})
                continue

            _event("migration", phase="start", key=key, index=index,
                   total=len(steps), source=str(source), target=str(target))

            try:
                # 关键顺序：先记期望，再动文件。同卷 rename 之后源就没了。
                expectation = capture_expectation(source)

                target.parent.mkdir(parents=True, exist_ok=True)

                if _same_volume(source, target):
                    source.rename(target)
                    mode = "move_same_volume"
                else:
                    shutil.copytree(
                        source, target,
                        ignore_dangling_symlinks=True,
                        dirs_exist_ok=True,
                    )
                    mode = "copy"

                _event("migration", phase="progress", key=key, completed=100, total=100)

                check = verify_against_expectation(expectation, target)
                if not check.get("ok"):
                    failures.append({
                        "key": key,
                        "error": "校验未通过，已保留现场（未删除任何数据）",
                        "verify": check,
                    })
                    _event("migration", phase="failed", key=key, error="校验未通过")
                    continue

                done.append({"key": key, "mode": mode, "source": str(source),
                             "target": str(target), "verify": check})
                _event("migration", phase="done", key=key, target=str(target))

            except Exception as error:  # noqa: BLE001 - 逐步报告，不中断整体
                failures.append({"key": key, "error": f"{type(error).__name__}: {error}"})
                _event("migration", phase="failed", key=key, error=str(error))

        elapsed = int((time.monotonic() - started) * 1000)

        # 迁移成功 → 记入台账。
        #
        # 台账是"清理授权"的唯一依据：cleanup_migrated_source 只认这里的记录，
        # 不认调用方给的路径。所以这一步必须在报告成功**之前**做 ——
        # 少了它，用户点"清理旧目录"会因为"没有对应记录"被拒（安全但难用）；
        # 有了它，能删的范围就被钉死在"本次真实校验通过的迁移项"上。
        try:
            import migration_ledger  # noqa: PLC0415

            records = [
                migration_ledger.build_record(
                    key=item["key"],
                    source=item["source"],
                    target=item["target"],
                    mode=item["mode"],
                    verified=True,
                )
                for item in done
            ]
            if records:
                migration_ledger.record_migrations(records)
                for item, record in zip(done, records):
                    item["recordId"] = record["id"]
        except Exception as error:  # noqa: BLE001 - 台账失败不该否定已完成的迁移
            print(f"[migration] 台账写入失败: {type(error).__name__}: {error}",
                  file=sys.stderr)

        # 关键收尾：把配置指针指到新位置。
        # 不更新配置的话，文件搬走了但应用仍去旧路径找 —— "迁移成功"却不可用，
        # 这比迁移失败更难排查（用户看到成功提示，功能却是空的）。
        config_updates: dict[str, Any] = {}
        for item in done:
            key = item["key"]
            target = item["target"]
            if key == "models":
                config_updates["models_root"] = target
                config_updates["models_root_mode"] = "custom"
            elif key == "cache":
                config_updates["ocr_cache_root"] = target

        config_error = ""
        if config_updates:
            try:
                from voxsub.config_store import ConfigStore  # noqa: PLC0415

                ConfigStore().update(config_updates)
                _event("migration", phase="config", keys=list(config_updates))
            except Exception as error:  # noqa: BLE001 - 文件已迁好，配置失败要单独报
                config_error = f"{type(error).__name__}: {error}"
                failures.append({
                    "key": "config",
                    "error": f"文件已迁移，但配置未能更新（{config_error}）。"
                             f"请在设置里手动把路径改到：{config_updates}",
                })
                print(f"[migration] 配置更新失败: {config_error}", file=sys.stderr)

        return {
            "done": done,
            "failed": failures,
            "elapsedMs": elapsed,
            "configUpdates": config_updates,
            "ok": not failures,
        }

    def _cmd_cleanup_migrated_source(self, args: dict[str, Any]) -> dict[str, Any]:
        """删除迁移后的源目录 —— **必须由用户单独二次确认**。

        单独一个命令而不是放在 start_migration 里自动做：删除是不可逆的，
        校验通过也不代表用户此刻就想删原件。

        安全模型（工作单 §3.8，**授权优先，不靠目录黑名单**）：

          · 入参只接受台账记录标识 ``record_id``（或 ``record_ids`` 数组），
            **不接受路径**。路径由后端从台账解析 —— 于是"手写一次 IPC 就能
            删掉任意目录"在结构上不成立：攻击者必须先让一次真实迁移成功，
            而迁移的源和目标都要通过校验。
          · 必须显式 ``confirm=True``。前端的确认框只是体验，不是防线。
          · 每条记录逐条过 :func:`migration_ledger.validate_cleanup_target`
            的白名单/空路径/卷根/受保护目录/重解析点/数据根归属检查。
          · 拒绝时**不抛异常**，而是回 ``deleted: false`` + 可读原因 ——
            这样界面能把"为什么不让删"直接告诉用户。

        历史包袱说明：旧版本接受 ``path`` 参数并在校验后 ``rmtree``，
        等于把"删哪个目录"的决定权交给了调用方。现在传 ``path`` 一律拒绝，
        见下面 ``args.get("path")`` 那个分支。
        """
        import shutil  # noqa: PLC0415

        import migration_ledger  # noqa: PLC0415

        if args.get("path"):
            return {
                "deleted": False,
                "code": "path_not_accepted",
                "detail": "出于安全考虑，清理只接受迁移记录标识（record_id），"
                          "不接受直接指定路径。请改用迁移完成后返回的 recordId。",
            }

        raw_ids = args.get("record_ids")
        if raw_ids is None:
            raw_ids = [args.get("record_id")]
        elif isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        record_ids = [str(item).strip() for item in raw_ids if str(item or "").strip()]
        if not record_ids:
            return {
                "deleted": False,
                "code": "missing_record_id",
                "detail": "缺少迁移记录标识（record_id），拒绝清理。",
            }

        confirm = bool(args.get("confirm", False))

        deleted: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []
        for record_id in record_ids:
            record = migration_ledger.find_record(record_id)
            try:
                path = migration_ledger.validate_cleanup_target(record, confirm=confirm)
            except migration_ledger.CleanupRefused as refusal:
                refused.append({"recordId": record_id, "detail": str(refusal)})
                continue
            try:
                shutil.rmtree(path)
            except OSError as error:
                refused.append({
                    "recordId": record_id,
                    "detail": f"删除失败：{type(error).__name__}: {error}",
                })
                continue
            deleted.append({"recordId": record_id, "path": str(path)})
            _event("migration", phase="cleaned", key=str(record.get("key", "")),
                   recordId=record_id, path=str(path))

        return {
            "deleted": bool(deleted),
            "paths": [item["path"] for item in deleted],
            "cleaned": deleted,
            "refused": refused,
            "ok": not refused,
            "detail": ("" if deleted else
                       (refused[0]["detail"] if refused else "没有可清理的项目")),
        }
