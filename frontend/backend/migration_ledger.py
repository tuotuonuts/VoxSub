"""迁移台账与清理授权 —— 删除操作只认台账记录，不认调用方给的路径。

为什么单独一个模块：

  1. ``ipc_server.py`` 已经 1600+ 行，"能不能删"的判断混在命令分发里会让
     规则无法逐条测试。
  2. 授权逻辑必须是**纯逻辑、可被 pytest 直接调用**，不依赖 Electron、
     不依赖 pipeline、不依赖真实文件系统布局。
  3. 台账是"上一次迁移到底做了什么"的唯一记忆 —— 迁移命令写、清理命令读。

安全铁律（本模块最高优先级约束，逐条对应实现与测试）：

  · 清理只接受**台账记录标识**（``record_id``），不接受调用方给的任意路径。
    路径由台账解析，因此"手写一次 IPC 就能删掉任意目录"在结构上不成立。
  · 只有"迁移已完成且三层校验通过"的记录才有资格进入删除流程。
  · 清理时源路径必须**与台账记录仍然一致** —— 记录被改写成别处就拒绝。
  · 空路径、卷根（``C:\\``）、受保护目录、重解析点（symlink/junction）
    一律拒绝，且这些判断不依赖"目录黑名单"是否列全。
  · 源与目标必须是不同目录，且互不包含。
  · 源不能是、也不能包含应用**正在使用**的数据根（模型/缓存/日志/配置/自身目录）
    —— 擦掉正在用的东西比误删旧副本严重得多。
  · 记录类型必须是真正会被搬的那几类（模型/缓存/工具）。
  · 网络路径与设备路径一律拒绝：解析不出可信的本地位置，"确认安全"无从谈起。
  · 用户必须显式确认（``confirm=True``）。前端确认框不能替代后端校验。

授权模型：**授权来自"这是一次真实、已校验通过的迁移"这件事本身**，
而不是来自一张"好目录"名单。调用方拿不到路径，所以"手写一次 IPC
就能删任意目录"在结构上不成立；其余检查都是护栏，不是猜测。

设计取舍：台账走 ``%LOCALAPPDATA%\\VoxSub\\migration-ledger.json``，与
``migration-state.json`` 同目录、同风格（原子写）。不写进 ``config.json``：
那份配置由 ConfigStore 的白名单管辖，加键会被拒；而且台账属于"新版自己的
记忆"，不该污染共享配置。
"""
from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence

#: 台账最多保留多少条记录。迁移是低频操作，50 条足够覆盖"最近几次"，
#: 同时避免这个文件无界增长。
MAX_RECORDS = 50

#: 这些目录**永远**不允许被清理。这里不是"黑名单兜底"，而是最后一道保险：
#: 真正的授权依据是台账记录 + 数据根归属，黑名单只负责少犯错。
PROTECTED_PATHS = (
    "c:\\",
    "c:\\windows",
    "c:\\users",
    "c:\\program files",
    "c:\\program files (x86)",
    "c:\\programdata",
)


class CleanupRefused(ValueError):
    """清理请求被拒绝。消息面向开发者/日志，不直接当用户文案。"""


class MigrationSourceRefused(ValueError):
    """迁移的**源**被拒绝。

    删除授权链的第一环就在这里：清理侧只认台账记录，而台账是 ``start_migration``
    写的。如果 ``start_migration`` 不校验源，那么"两步 IPC 删掉任意目录"依然成立 ——
    这是独立审查实测出来的（跨卷复制时源仍在，而 C:→D: 正是本项目的常规迁移方向）。
    """


def ledger_path() -> Path:
    """台账文件位置。与 migration-state.json 同目录。"""
    local = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(local) / "VoxSub" / "migration-ledger.json"


def read_ledger() -> list[dict[str, Any]]:
    """读取台账。文件缺失/损坏一律当作空台账，不抛异常。"""
    try:
        raw = json.loads(ledger_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def write_ledger(records: Sequence[dict[str, Any]]) -> None:
    """原子写入台账。

    复用 ``voxsub.file_io.write_text_atomically``：崩溃安全与 Windows 瞬时
    占用重试是**已经解决过一次**的问题，再写一份平行实现只会让两边慢慢跑偏
    （工作单 §3.2：同一条公共规则只允许有一个权威实现）。
    """
    from voxsub.file_io import write_text_atomically  # noqa: PLC0415

    target = ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    trimmed = list(records)[-MAX_RECORDS:]
    write_text_atomically(
        target,
        json.dumps(trimmed, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def new_record_id() -> str:
    """给一条迁移记录生成标识。"""
    return uuid.uuid4().hex


def record_migrations(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """把本次迁移的成功项追加进台账，返回结果台账。"""
    existing = read_ledger()
    existing.extend(entries)
    write_ledger(existing)
    return existing[-MAX_RECORDS:]


def build_record(*, key: str, source: str, target: str, mode: str,
                 verified: bool) -> dict[str, Any]:
    """构造一条迁移记录。

    ``verified`` 是清理授权的**唯一**入场券：只有三层校验通过的成功项才为真。
    """
    return {
        "id": new_record_id(),
        "key": str(key),
        "source": str(source),
        "target": str(target),
        "mode": str(mode),
        "verified": bool(verified),
    }


def find_record(record_id: str) -> dict[str, Any] | None:
    """按标识查记录。空标识直接返回 None（不匹配任何记录）。"""
    wanted = str(record_id or "").strip()
    if not wanted:
        return None
    for record in read_ledger():
        if str(record.get("id", "")) == wanted:
            return record
    return None


# ------------------------------------------------------------------ 路径判定

def _norm(path: Path | str) -> str:
    """归一化用于比较的字符串：解析 + 小写 + 去掉尾部反斜杠。"""
    try:
        text = str(Path(path).resolve())
    except (OSError, ValueError):
        text = str(path)
    return text.lower().rstrip("\\/")


def is_volume_root(path: Path | str) -> bool:
    """是否是卷根（``C:\\``、``D:\\``）。"""
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        return True  # 解析不了就不允许删
    return not resolved.name and not resolved.parent.name


def is_protected(path: Path | str) -> bool:
    """是否落在受保护目录名单里（含其本身，不含其子目录）。"""
    return _norm(path) in {item for item in PROTECTED_PATHS}


def is_reparse_point(path: Path) -> bool:
    """是否是重解析点（symlink / junction）。

    重解析点可以把"删除 A"变成"删除 A 指向的 B"，因此一律拒绝。
    Windows 上用 lstat 的文件属性判断（``is_symlink()`` 认不出 junction）。
    """
    try:
        info = path.lstat()
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        return True
    return path.is_symlink()


def is_inside(child: Path | str, parent: Path | str) -> bool:
    """child 是否位于 parent 之内（纯字符串判定，不要求存在）。"""
    child_n = _norm(child)
    parent_n = _norm(parent)
    if not child_n or not parent_n:
        return False
    return child_n != parent_n and child_n.startswith(parent_n + os.sep)


# ------------------------------------------------------------------ 授权

#: 允许被清理的迁移键。来源是 ``legacy_migration`` 里真正会搬的东西：
#: 模型库、OCR 缓存、可重建的工具目录。
#:
#: 为什么不放 ``config_dir`` / ``logs``：那两个在 ``NEVER_MOVE_KEYS`` 里，
#: 根本不会被搬，也就永远不该出现在清理流程里。**默认拒绝** ——
#: 出现未知 key 说明台账被塞了不该有的东西，拒绝比猜安全。
MIGRATABLE_KEYS = frozenset({"models", "cache", "tools"})


def active_app_roots() -> list[Path]:
    """当前应用**正在使用**的数据根。

    这不是"允许删哪些"的白名单，而是"这些绝对不能删、也不能包含在被删目录里"
    的护栏：把正在用的模型根/缓存/日志/配置或应用自身目录连锅端掉，
    后果比误删旧副本严重得多。

    取值一律来自环境与配置，**不取自调用方参数** —— 否则护栏就变成自证。
    """
    roots: list[Path] = []

    try:
        from voxsub.model_storage import resolve_models_root  # noqa: PLC0415

        roots.append(Path(resolve_models_root()))
    except Exception:  # noqa: BLE001 - 取不到就不加入护栏
        pass

    local = os.environ.get("LOCALAPPDATA") or str(Path.home())
    base = Path(local) / "VoxSub"
    roots.extend([base / "models", base / "cache", base / "logs", base])

    app_root = os.environ.get("VOXSUB_ROOT")
    if app_root:
        roots.append(Path(app_root))

    return roots


def is_unc_or_device_path(path: Path | str) -> bool:
    """是否是 UNC（``\\\\server\\share``）或设备路径（``\\\\?\\`` / ``\\\\.\\``）。

    这类路径解析不出可信的本地位置，"确认安全"无从谈起，因此默认拒绝。
    """
    text = str(path).strip()
    if not text:
        return False
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    return bool(Path(text).drive.startswith("\\\\"))


def contains_active_app_root(source: Path | str,
                             roots: Sequence[Path] | None = None) -> Path | None:
    """源是否包含（或就是）某个正在使用的应用根。返回命中的那个根。"""
    candidates = list(roots) if roots is not None else active_app_roots()
    for root in candidates:
        if not str(root).strip():
            continue
        if _norm(source) == _norm(root) or is_inside(root, source):
            return Path(root)
    return None


def validate_migration_source(source: Path | str, *,
                              allowed_sources: Sequence[Path]) -> Path:
    """校验"能不能把**这个目录**当作迁移的源"，返回归一化后的源路径。

    为什么需要它（独立审查实测出的真问题）：

      清理侧只认台账记录，看起来"手写一次 IPC 就能删任意目录"不成立。但台账是
      ``start_migration`` 写的，而 ``start_migration`` 以前对 ``source`` **只检查
      "是不是目录"**。于是两步就能删掉任意仍存在的用户目录：

          1. start_migration({source: <任意用户目录>, target: <攻击者选的目录>})
             → 校验"通过"、台账写入 verified=True、跨卷复制后源仍在
          2. cleanup_migrated_source({record_id, confirm: true}) → rmtree

      同卷 rename 会让源消失（被"源目录不存在"挡住），但**跨卷复制是常规用法** ——
      C: 到 D:/VoxSub 正是本项目自己的默认迁移方向。所以这条链真的能走通。

    授权依据：**源必须是应用自己认的数据位置**（``allowed_sources`` 由调用方给出，
    来自 ``legacy_migration.assess_storage`` 的探测结果与当前配置的模型/缓存根）。
    这与清理侧同一套思路 —— 不是"猜哪些路径好"，而是"证明它是我们的东西"。
    **未知即拒绝。**
    """
    raw = str(source or "").strip()
    if not raw:
        raise MigrationSourceRefused("迁移源为空，拒绝")

    path = Path(raw)

    if is_unc_or_device_path(raw):
        raise MigrationSourceRefused(f"拒绝迁移网络/设备路径：{raw}")
    if is_volume_root(path):
        raise MigrationSourceRefused(f"拒绝迁移卷根：{path}")
    if is_protected(path):
        raise MigrationSourceRefused(f"拒绝迁移受保护目录：{path}")
    if not path.is_dir():
        raise MigrationSourceRefused(f"迁移源不是目录：{path}")
    if is_reparse_point(path):
        raise MigrationSourceRefused(f"拒绝迁移重解析点（symlink/junction）：{path}")

    authorized = [Path(item) for item in allowed_sources if str(item).strip()]
    if not authorized:
        raise MigrationSourceRefused(
            "没有探测到任何属于本应用的数据位置，拒绝迁移（无法证明这个源是我们的）")

    if not any(_norm(path) == _norm(root) or is_inside(path, root) for root in authorized):
        raise MigrationSourceRefused(
            f"迁移源不在本应用已知的数据位置内，拒绝：{path}")

    return path


def source_is_inside_recorded_target(record: dict[str, Any]) -> bool:
    """源是否被搬到了目标之内（移动套娃），这种现场不该按"源"清掉。"""
    source = str(record.get("source", ""))
    target = str(record.get("target", ""))
    if not source or not target:
        return False
    return is_inside(source, target)


def validate_cleanup_target(record: dict[str, Any] | None, *,
                            confirm: bool,
                            active_roots: Sequence[Path] | None = None) -> Path:
    """校验一条台账记录是否允许清理，返回可删除的源路径。

    任一条件不满足就抛 :class:`CleanupRefused`。调用方（IPC 命令）只负责
    把台账记录喂进来并转成用户可读的应答，判断规则全在这里，便于逐条测试。

    **授权模型**：授权来自"这条记录是一次真实、已校验通过的迁移"这件事本身。
    调用方无法提供路径，所以"手写一次 IPC 就能删任意目录"在结构上不成立。
    其余检查都是护栏（拒绝条件），不是"猜哪些路径是好的"。
    """
    if record is None:
        raise CleanupRefused("没有对应的迁移记录，拒绝清理")
    if not confirm:
        raise CleanupRefused("缺少用户确认，拒绝清理")
    if not record.get("verified"):
        raise CleanupRefused("该记录未通过迁移校验，拒绝清理")

    key = str(record.get("key", "")).strip()
    if key not in MIGRATABLE_KEYS:
        raise CleanupRefused(f"记录类型不可清理（{key or '空'}），拒绝清理")

    source_raw = str(record.get("source", "")).strip()
    target_raw = str(record.get("target", "")).strip()
    if not source_raw or not target_raw:
        raise CleanupRefused("记录的源或目标为空，拒绝清理")

    source = Path(source_raw)
    target = Path(target_raw)

    # 解析不出可信本地位置的路径：一律拒绝（"无法确认安全性"）。
    if is_unc_or_device_path(source_raw) or is_unc_or_device_path(target_raw):
        raise CleanupRefused(f"拒绝清理网络/设备路径：{source_raw}")

    if _norm(source) == _norm(target):
        raise CleanupRefused("源与目标是同一目录，拒绝清理")

    if is_volume_root(source):
        raise CleanupRefused(f"拒绝清理卷根：{source}")
    if is_protected(source):
        raise CleanupRefused(f"拒绝清理受保护目录：{source}")

    if is_inside(target, source):
        raise CleanupRefused("目标是源的子目录，拒绝清理")
    if source_is_inside_recorded_target(record):
        raise CleanupRefused("源位于目标之内，拒绝清理")

    # 最关键的一条：不能把应用正在使用的东西删掉。
    hit = contains_active_app_root(source, active_roots)
    if hit is not None:
        raise CleanupRefused(f"源包含或就是正在使用的数据目录，拒绝清理：{hit}")

    if not source.exists():
        raise CleanupRefused(f"源目录不存在：{source}")
    if not source.is_dir():
        raise CleanupRefused(f"源不是目录：{source}")
    if is_reparse_point(source):
        raise CleanupRefused(f"拒绝清理重解析点（symlink/junction）：{source}")

    return source
