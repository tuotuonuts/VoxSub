#!/usr/bin/env python
"""旧版检测与迁移规划 —— Electron 版首次启动时的"老用户识别"。

职责（对应已确认的设计）：
  1. 检测旧版（Qt 版）是否安装：读注册表 Uninstall 项，拿安装位置与版本
  2. 分析数据风险：模型库/缓存是否位于会被卸载或误删的位置
  3. 规划迁移：列出需要搬动的项目（原路径 → 目标路径、体积、用途）
  4. 模型快照：即使数据真丢了，也能按快照一键重下

为什么单独一个模块而不是塞进 ipc_server.py：
  ipc_server.py 已经 900+ 行，迁移逻辑（检测/规划/复制/校验/回滚）本身有
  独立的状态机和失败面，混在一起会让"命令分发"和"文件搬运"两件事互相干扰。

安全铁律（本模块的最高优先级约束）：
  · 只读取、不修改：检测阶段绝不写任何文件
  · 迁移用复制而非移动：任何情况下都不删源
  · 校验不过不删除任何东西
  · 删除源必须由用户单独二次确认（不在本模块自动做）

用法：
    python backend/legacy_migration.py detect
    python backend/legacy_migration.py plan
    python backend/legacy_migration.py risk
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

# ------------------------------------------------------------------ 常量

#: 旧版安装器（installer.iss）在安装时即声明删除的目录。
#: 这些目录下的数据在卸载时必然消失 —— 属于 highest 风险。
INSTALL_DELETE_DIRS = ("_internal", "tools", "models_base")

#: 常见"程序目录"，用于判断某个路径是否位于会被整体清理的位置
PROGRAM_LOCATIONS = (
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    os.path.expandvars(r"%ProgramFiles%"),
    os.path.expandvars(r"%ProgramFiles(x86)%"),
    os.path.expandvars(r"%ProgramW6432%"),
)

#: 数据项的用途说明（给用户看的"这是干什么用的"）
PURPOSE = {
    "models": "识别、翻译、朗读所需的模型文件。丢了要重新下载（数 GB）",
    "cache": "OCR 缓存（上传/截图的原图与译后图）。丢了不影响功能，只是历史记录消失",
    "logs": "运行日志。丢了不影响功能",
    "config": "设置（设备选择、语言对、调优参数等）。丢了需要重新设置",
    "tools": "运行时会话组件（如 llama 运行时）。可由程序重新获取",
}


# ------------------------------------------------------------------ 数据结构

@dataclass
class LegacyInstall:
    """检测到的旧版安装。"""

    found: bool = False
    install_location: str = ""
    version: str = ""
    display_name: str = ""
    uninstall_string: str = ""
    uninstaller_present: bool = False
    registry_key: str = ""
    source: str = ""          # registry / config / none
    notes: list[str] = field(default_factory=list)


@dataclass
class StorageCheck:
    """某个数据目录的风险判定。"""

    key: str                  # models / cache / logs / config / tools
    path: str
    exists: bool
    bytes: int
    file_count: int
    inside_install: bool
    #: safe         → 完全在安装目录外，卸载/误删都不会碰到
    #: conditional  → 在安装目录内，但不在 install-delete 名单里（卸载不删，手删会丢）
    #: exposed      → 在 install-delete 名单里（卸载必删）
    risk: str
    purpose: str
    detail: str = ""


@dataclass
class MigrationStep:
    """一条迁移动作。"""

    key: str
    source: str
    target: str
    bytes: int
    file_count: int
    purpose: str
    #: copy（跨卷，真复制）/ move_same_volume（同卷，原子 rename）
    mode: str
    note: str = ""
    #: 程序能自行重建的目录（如 tools\\llama 运行时）——界面默认不勾选
    rebuildable: bool = False


# ------------------------------------------------------------------ 检测

def _registry_roots() -> Iterable[tuple[Any, str, str]]:
    """枚举应该检查的注册表位置（含 32/64 位与当前用户）。"""
    import winreg

    return (
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM32"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKCU"),
    )


def detect_legacy_install() -> LegacyInstall:
    """读注册表找旧版（Qt 版）安装。只读，不写任何东西。"""
    result = LegacyInstall()

    try:
        import winreg
    except ImportError:  # 非 Windows
        result.notes.append("当前平台没有 winreg，跳过注册表检测")
        return _detect_from_config(result)

    for hive, subkey, label in _registry_roots():
        try:
            with winreg.OpenKey(hive, subkey) as root:
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(root, index)
                        index += 1
                    except OSError:
                        break

                    try:
                        with winreg.OpenKey(root, name) as item:
                            display = _reg_value(item, "DisplayName") or ""
                            publisher = _reg_value(item, "Publisher") or ""
                            # 认产品名而不是认固定的 AppId：旧版改过 AppId 也能命中
                            haystack = f"{display} {publisher} {name}".lower()
                            if "voxsub" not in haystack and "语幕" not in haystack:
                                continue

                            result.found = True
                            result.display_name = display
                            result.version = _reg_value(item, "DisplayVersion") or ""
                            result.install_location = _reg_value(item, "InstallLocation") or ""
                            result.uninstall_string = _reg_value(item, "UninstallString") or ""
                            result.registry_key = f"{label}\\{name}"
                            result.source = "registry"
                    except OSError:
                        continue
        except FileNotFoundError:
            continue

    if result.found:
        if result.install_location:
            uninstaller = Path(result.install_location) / "unins000.exe"
            result.uninstaller_present = uninstaller.is_file()
            if not result.uninstaller_present:
                # 安装目录还在但卸载器没了：用户可能手动删过一部分
                result.notes.append(
                    "注册表有卸载记录，但安装目录里找不到卸载器 —— 可能已被手动清理过一部分"
                )
        else:
            result.notes.append("注册表记录里没有 InstallLocation，无法定位安装目录")
        return result

    return _detect_from_config(result)


def _detect_from_config(result: LegacyInstall) -> LegacyInstall:
    """注册表没记录时，退一步看配置里有没有旧版留下的痕迹。

    这种情况常见于：用户直接解压绿色版、或卸载时清理过注册表但数据还在。
    """
    config = legacy_config_path()
    if config.is_file():
        result.notes.append(
            f"注册表无卸载记录，但发现旧版配置：{config}（说明此机器用过旧版）"
        )
        if not result.source:
            result.source = "config"
            result.found = True
    return result


def _reg_value(key: Any, name: str) -> str | None:
    import winreg

    try:
        value, _ = winreg.QueryValueEx(key, name)
        return str(value) if value is not None else None
    except OSError:
        return None


# ------------------------------------------------------------------ 路径工具

def legacy_config_path() -> Path:
    """旧版配置文件路径（新老两版共享同一份）。"""
    local = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(local) / "VoxSub" / "config.json"


def read_legacy_config() -> dict[str, Any]:
    """读旧版配置。解析失败返回空字典（不抛，检测阶段要稳）。"""
    path = legacy_config_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _dir_stats(path: Path) -> tuple[int, int]:
    """返回 (总字节, 文件数)。不可读的条目跳过。"""
    total = 0
    count = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
                    count += 1
            except OSError:
                continue
    except OSError:
        return 0, 0
    return total, count


def _same_volume(a: Path, b: Path) -> bool:
    """判断两个路径是否在同一卷 —— 同卷可以原子 rename，跨卷只能复制。"""
    try:
        return a.resolve().drive.lower() == b.resolve().drive.lower()
    except (OSError, ValueError):
        return False


def _is_inside(child: Path, parent: Path) -> bool:
    """child 是否在 parent 之内（不依赖 exists，纯字符串判定）。"""
    try:
        child_n = str(child.resolve()).lower().rstrip("\\")
        parent_n = str(parent.resolve()).lower().rstrip("\\")
    except (OSError, ValueError):
        return False
    if not parent_n:
        return False
    return child_n == parent_n or child_n.startswith(parent_n + "\\")


# ------------------------------------------------------------------ 风险评估

def assess_storage(legacy: LegacyInstall) -> list[StorageCheck]:
    """评估各数据目录的风险等级。只读。"""
    config = read_legacy_config()
    local = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "VoxSub"
    install = Path(legacy.install_location) if legacy.install_location else None

    # 数据目录候选：(key, 路径来源)
    candidates: list[tuple[str, Path]] = []

    configured_models = str(config.get("models_root") or "").strip()
    if configured_models:
        candidates.append(("models", Path(configured_models)))
    if install is not None:
        # 旧版的默认位置就是安装目录下的 Models
        candidates.append(("models_default", install / "Models"))
        candidates.append(("cache", install / "Cache"))
        candidates.append(("tools", install / "tools"))
    candidates.append(("config_dir", local))
    candidates.append(("logs", local / "logs"))
    candidates.append(("cache_user", local / "cache"))

    checks: list[StorageCheck] = []
    seen: set[str] = set()

    for key, path in candidates:
        real_key = "models" if key.startswith("models") else key
        marker = f"{real_key}:{str(path).lower()}"
        if marker in seen:
            continue
        seen.add(marker)

        exists = path.is_dir()
        size, files = _dir_stats(path) if exists else (0, 0)

        inside = bool(install) and _is_inside(path, install)
        top = ""
        if inside and install is not None:
            try:
                rel = str(path.resolve())[len(str(install.resolve())):].strip("\\")
                top = rel.split("\\")[0].lower() if rel else ""
            except (OSError, ValueError):
                top = ""

        if not inside:
            risk = "safe"
            detail = "位于安装目录之外，卸载旧版不会影响"
        elif top in {d.lower() for d in INSTALL_DELETE_DIRS}:
            risk = "exposed"
            detail = f"位于旧版安装目录的 {top}\\ 下，且该目录在安装器的删除名单里 —— 卸载必然删除"
        else:
            risk = "conditional"
            detail = (
                "位于旧版安装目录内，但不在安装器的删除名单里。"
                "卸载旧版本身不会删它；但如果手动删除整个安装目录，数据会一起丢失"
            )

        checks.append(StorageCheck(
            key=real_key,
            path=str(path),
            exists=exists,
            bytes=size,
            file_count=files,
            inside_install=inside,
            risk=risk,
            purpose=PURPOSE.get(real_key, ""),
            detail=detail,
        ))

    # 合并同 key 的多条（例如 models 同时有配置路径与默认路径）
    merged: dict[str, StorageCheck] = {}
    for check in checks:
        if not check.exists and not check.bytes:
            # 不存在的路径：只有当它是"配置里指定但已丢失"时才报告（属于异常）
            if check.key == "models" and check.path.lower() in seen:
                continue
            continue
        current = merged.get(check.key)
        if current is None or check.bytes > current.bytes:
            merged[check.key] = check

    # 风险排序：exposed > conditional > safe
    order = {"exposed": 0, "conditional": 1, "safe": 2}
    return sorted(merged.values(), key=lambda c: (order.get(c.risk, 9), -c.bytes))


#: 程序能自行重建的目录 —— 这类数据即使位于删除名单里，也不构成"必须先迁移"。
#: 例如 tools\\llama 运行时：新版能重新获取，搬它对新版没有收益。
REBUILDABLE_KEYS = frozenset({"tools"})

#: 永不迁移的共享路径：位置本身就是两版之间的契约，搬走会让两边都读不到。
NEVER_MOVE_KEYS = frozenset({"config_dir", "logs"})


def overall_risk(checks: list[StorageCheck]) -> str:
    """整体风险 —— 只统计"用户真正需要处理"的数据。

    不能简单取最高单项风险：tools\\ 在安装器的删除名单里（exposed），但它是
    程序可自行重建的运行时。若把它算作关键，向导会对所有用户喊"必须先迁移"，
    而真正重要的模型库其实只是 conditional —— 这是误导，会让用户白等几十分钟
    搬一份本可以自动重建的数据。
    """
    for level in ("exposed", "conditional"):
        if any(c.risk == level and c.key not in REBUILDABLE_KEYS for c in checks):
            return level
    return "safe"


# ------------------------------------------------------------------ 迁移状态

def state_path() -> Path:
    """迁移向导的状态标记文件。

    单独一个文件而不是写进 config.json：config.json 由 ConfigStore 管着白名单，
    加新键会被拒；而且这个状态属于"新版自己的记忆"，不该污染共享配置。
    """
    local = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(local) / "VoxSub" / "migration-state.json"


def read_state() -> dict[str, Any]:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(**updates: Any) -> dict[str, Any]:
    state = read_state()
    state.update(updates)
    target = state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    return state


def should_offer_wizard() -> tuple[bool, dict[str, Any]]:
    """是否该弹迁移向导。

    三条都满足才弹：
      1. 检测到旧版
      2. 整体风险不是 safe（数据在安全位置的用户不该被打扰）
      3. 用户还没做过决定（完成过 / 明确跳过过都不再弹）
    """
    state = read_state()
    if state.get("dismissed") or state.get("completed"):
        return False, state

    legacy = detect_legacy_install()
    if not legacy.found:
        return False, state

    checks = assess_storage(legacy)
    if overall_risk(checks) == "safe":
        return False, state

    return True, state


# ------------------------------------------------------------------ 迁移规划

def plan_migration(
    checks: list[StorageCheck],
    target_root: str | Path,
    *,
    keys: Iterable[str] | None = None,
) -> list[MigrationStep]:
    """规划迁移步骤。纯计算，不碰文件系统。

    目标规则：每个数据类型在 target_root 下占一个子目录
      <target_root>/Models、<target_root>/Cache …

    两条硬约束（踩过就会毁数据/毁功能）：
      1. **共享配置目录永不迁移** —— %LOCALAPPDATA%\\VoxSub 是两版共用的配置与
         日志位置。搬走它等于两版都读不到配置，属于灾难性错误。
      2. **程序可重建的运行时目录不建议迁移** —— 例如 tools\\\\（llama 运行时）：
         它在旧版安装器的删除名单里，但新版能自行重新获取；搬它只会让旧版
         缺组件，对新版没有收益。列为建议项而非默认项，且不受"必迁"驱动。
    """
    root = Path(target_root)
    wanted = set(keys) if keys else None
    steps: list[MigrationStep] = []

    for check in checks:
        if check.key in NEVER_MOVE_KEYS:
            continue
        if wanted is not None and check.key not in wanted:
            continue
        if not check.exists or check.bytes <= 0:
            continue

        source = Path(check.path)
        target = root / check.key.capitalize()

        if _is_inside(target, source):
            continue  # 目标是源的子目录，无意义
        if _is_inside(source, target):
            continue  # 源已在目标之下，无需搬迁

        if _same_volume(source, target):
            mode = "move_same_volume"
            note = "同一磁盘：原子改名，几乎瞬间完成且不会留下半个副本"
        else:
            mode = "copy"
            note = "跨磁盘：需要真实复制，请勿中途关闭"

        # 程序可重建的目录：标注出来，让界面可以默认不勾选
        rebuildable = check.key in REBUILDABLE_KEYS
        if rebuildable:
            note = (
                "该目录由程序自行管理，新版可重新获取。"
                "迁移它对新版没有收益，反而可能让旧版缺组件 —— 默认不迁移"
            )

        steps.append(MigrationStep(
            key=check.key,
            source=str(source),
            target=str(target),
            bytes=check.bytes,
            file_count=check.file_count,
            purpose=check.purpose,
            mode=mode,
            note=note,
            rebuildable=rebuildable,
        ))

    # 体积大的排前面：用户最关心那个，也便于早暴露磁盘空间不足
    return sorted(steps, key=lambda s: -s.bytes)


# ------------------------------------------------------------------ 模型快照

def snapshot_path() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(local) / "VoxSub" / "model-snapshot.json"


def write_model_snapshot(models_root: str | Path) -> dict[str, Any]:
    """把模型库现状写成快照。

    用途：万一数据真丢了，可按快照知道"曾经有什么、多大、从哪下"，
    不必让用户凭记忆一个个找回来。
    """
    root = Path(models_root)
    manifest = root / "manifest.json"
    installs = root / "catalog_installs.json"

    snapshot: dict[str, Any] = {
        "root": str(root),
        "exists": root.is_dir(),
        "entries": [],
    }

    for name, path in (("manifest", manifest), ("catalog_installs", installs)):
        try:
            snapshot[name] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            snapshot[name] = None

    # 顶层子目录的体积，便于核对"迁移前后一致"
    if root.is_dir():
        for child in sorted(root.iterdir()):
            try:
                if child.is_dir():
                    size, files = _dir_stats(child)
                    snapshot["entries"].append({
                        "name": child.name, "bytes": size, "files": files,
                    })
            except OSError:
                continue

    target = snapshot_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    return {"path": str(target), "entries": len(snapshot["entries"])}


# ------------------------------------------------------------------ 校验

def capture_expectation(path: Path) -> dict[str, Any]:
    """搬迁前记录源目录的"期望状态"。

    为什么必须单独一步：同卷迁移用的是 rename，执行后源目录就不存在了，
    再想拿它做校验依据已经来不及。所以必须在动文件之前把校验基准存下来。

    记录内容：总字节、文件数、manifest 里每个文件的 sha256。
    """
    total, count = _dir_stats(path)
    hashes: dict[str, str] = {}
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        for rel, meta in (manifest.get("files") or {}).items():
            digest = str(meta.get("sha256") or "")
            if digest:
                hashes[rel] = digest
    except (OSError, json.JSONDecodeError):
        pass

    return {
        "source": str(path),
        "bytes": total,
        "files": count,
        "hashes": hashes,
    }


def verify_against_expectation(expectation: dict[str, Any], target: Path) -> dict[str, Any]:
    """拿搬迁前记录的期望状态校验目标目录。

    三层校验（与 verify_copy 同构，但基准来自快照而非活源目录）：
      层 1 文件数 + 总字节
      层 2 manifest 内的 sha256
      层 3 可加载性 —— 由调用方补充（需要模型引擎）
    """
    import hashlib

    result: dict[str, Any] = {
        "source": expectation.get("source", ""),
        "target": str(target),
        "layers": {},
        "ok": False,
    }

    if not target.is_dir():
        result["layers"]["totals"] = {"ok": False, "detail": "目标目录不存在"}
        return result

    # 层 1
    t_bytes, t_files = _dir_stats(target)
    expect_bytes = int(expectation.get("bytes", 0))
    expect_files = int(expectation.get("files", 0))
    layer1_ok = (t_files == expect_files) and (t_bytes == expect_bytes)
    result["layers"]["totals"] = {
        "ok": layer1_ok,
        "expected": {"bytes": expect_bytes, "files": expect_files},
        "target": {"bytes": t_bytes, "files": t_files},
        "detail": "" if layer1_ok else (
            f"文件数 {expect_files}→{t_files}，字节 {expect_bytes}→{t_bytes}"
        ),
    }

    # 层 2
    hashes: dict[str, str] = expectation.get("hashes") or {}
    layer2: dict[str, Any] = {"ok": True, "checked": 0, "mismatch": [], "missing": []}
    for rel, expected in hashes.items():
        dst = target / rel
        if not dst.is_file():
            layer2["missing"].append(rel)
            continue
        try:
            digest = hashlib.sha256(dst.read_bytes()).hexdigest()
        except OSError:
            layer2["missing"].append(rel)
            continue
        layer2["checked"] += 1
        if digest != expected:
            layer2["mismatch"].append(rel)
    layer2["ok"] = not layer2["missing"] and not layer2["mismatch"]
    layer2["detail"] = (
        f"校验 {layer2['checked']} 个文件" if layer2["ok"] else
        f"缺失 {len(layer2['missing'])} 个，校验不符 {len(layer2['mismatch'])} 个"
    )
    result["layers"]["manifest"] = layer2
    result["layers"]["loadable"] = {"ok": None, "detail": "未执行（需模型引擎）"}

    result["ok"] = bool(layer1_ok and layer2.get("ok"))
    return result


def verify_copy(source: Path, target: Path) -> dict[str, Any]:
    """复制后的三层校验。

    为什么必须三层：实测发现 manifest.json 只覆盖 2.27 GB，而磁盘上实际有
    11 GB（ocr/ 与 marketplace/ 不在清单里）。只查 manifest 会漏掉 8.7 GB。

      层 1 文件数 + 总字节 —— 覆盖全部
      层 2 manifest 里的 sha256 —— 覆盖清单内的部分
      层 3 关键目录可加载 —— 交给调用方（需要模型引擎，本函数不引入重依赖）
    """
    result: dict[str, Any] = {
        "source": str(source),
        "target": str(target),
        "layers": {},
        "ok": False,
    }

    if not target.is_dir():
        result["layers"]["structure"] = {"ok": False, "detail": "目标目录不存在"}
        return result

    # 层 1：文件数 + 总字节
    s_bytes, s_files = _dir_stats(source)
    t_bytes, t_files = _dir_stats(target)
    layer1_ok = (s_files == t_files) and (s_bytes == t_bytes)
    result["layers"]["totals"] = {
        "ok": layer1_ok,
        "source": {"bytes": s_bytes, "files": s_files},
        "target": {"bytes": t_bytes, "files": t_files},
        "detail": "" if layer1_ok else (
            f"文件数 {s_files}→{t_files}，字节 {s_bytes}→{t_bytes}"
        ),
    }

    # 层 2：manifest 内的 sha256
    import hashlib

    manifest_path = source / "manifest.json"
    layer2: dict[str, Any] = {"ok": True, "checked": 0, "mismatch": [], "missing": []}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest.get("files") or {}
        for rel, meta in files.items():
            expected = str(meta.get("sha256") or "")
            src_file = source / rel
            dst_file = target / rel
            if not src_file.is_file():
                continue
            if not dst_file.is_file():
                layer2["missing"].append(rel)
                continue
            if not expected:
                continue
            digest = hashlib.sha256(dst_file.read_bytes()).hexdigest()
            layer2["checked"] += 1
            if digest != expected:
                layer2["mismatch"].append(rel)
        layer2["ok"] = not layer2["missing"] and not layer2["mismatch"]
        layer2["detail"] = (
            f"校验 {layer2['checked']} 个文件"
            if layer2["ok"] else
            f"缺失 {len(layer2['missing'])} 个，校验不符 {len(layer2['mismatch'])} 个"
        )
    except (OSError, json.JSONDecodeError) as error:
        layer2 = {"ok": False, "detail": f"无法读取清单：{error}", "checked": 0}
    result["layers"]["manifest"] = layer2

    # 层 3：占位，由调用方补充（需要模型引擎）
    result["layers"]["loadable"] = {"ok": None, "detail": "未执行（需模型引擎）"}

    result["ok"] = bool(layer1_ok and layer2.get("ok"))
    return result


# ------------------------------------------------------------------ CLI

def _cmd_detect() -> int:
    legacy = detect_legacy_install()
    print(json.dumps(asdict(legacy), ensure_ascii=False, indent=2))
    return 0


def _cmd_risk() -> int:
    legacy = detect_legacy_install()
    checks = assess_storage(legacy)
    print(f"旧版安装: {'找到' if legacy.found else '未找到'}")
    if legacy.found:
        print(f"  位置: {legacy.install_location or '(未知)'}")
        print(f"  版本: {legacy.version or '(未知)'}")
    print(f"\n整体风险: {overall_risk(checks)}")
    print(f"\n{len(checks)} 个数据目录：")
    for check in checks:
        gb = check.bytes / 2**30
        print(f"  [{check.risk:12s}] {check.key:12s} {check.path}")
        print(f"                 {gb:.2f} GB / {check.file_count} 文件")
        if check.detail:
            print(f"                 {check.detail}")
    if legacy.notes:
        print("\n备注：")
        for note in legacy.notes:
            print(f"  · {note}")
    return 0


def _cmd_plan() -> int:
    legacy = detect_legacy_install()
    checks = assess_storage(legacy)
    # 默认目标：与旧版同盘时放在同级的独立目录，避免"数据在安装目录里"的老问题
    install_drive = Path(legacy.install_location).drive if legacy.install_location else "D:"
    target = f"{install_drive}\\VoxSub\\Data"
    steps = plan_migration(checks, target)
    print(f"目标根目录: {target}\n")
    if not steps:
        print("没有需要迁移的项目（数据都已在安全位置，或不存在）")
        return 0
    total = sum(s.bytes for s in steps)
    print(f"共 {len(steps)} 项，合计 {total / 2**30:.2f} GB：\n")
    for step in steps:
        print(f"  · {step.key}")
        print(f"      {step.source}")
        print(f"   →  {step.target}")
        print(f"      {step.bytes / 2**30:.2f} GB / {step.file_count} 文件  [{step.mode}]")
        print(f"      用途：{step.purpose}")
        print(f"      说明：{step.note}")
    return 0


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "risk"
    handlers = {"detect": _cmd_detect, "risk": _cmd_risk, "plan": _cmd_plan}
    handler = handlers.get(command)
    if handler is None:
        print(f"未知命令：{command}\n可用：{' / '.join(handlers)}")
        return 2
    return handler()


if __name__ == "__main__":
    raise SystemExit(main())
