"""发布包装契约守卫 —— 纯文本解析，不做任何真实打包。

背景（工作单缺陷 #13/#14）：
    版本号原本散落在 package.json / electron-builder 配置 / Inno Setup /
    Python 包 / IPC 握手 / README 与发布说明至少六处，没有任何自动检查。
    历史上有过 `tests/test_packaging.py`（在 commit 9b87ba5 的重构中被删除），
    但它守的是安装器行为断言，**没有**版本一致性断言 —— 所以"删掉了检查"
    这句话要修正成"从来没有过版本一致性检查"。本文件补上真正的契约。

这些测试只读文件、只跑纯函数，不调用 electron-builder、不产出安装包、
不弹窗、不播放音频。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD_RELEASE_PY = ROOT / "frontend" / "tools" / "build-release.py"


def _load_build_release():
    """把带连字符的 build-release.py 当模块载入。

    必须先进 sys.modules —— dataclasses 在装饰期会 `sys.modules[cls.__module__]`，
    只做 module_from_spec 会抛 AttributeError: 'NoneType' object has no attribute
    '__dict__'（实测踩过）。
    """
    spec = importlib.util.spec_from_file_location("build_release", BUILD_RELEASE_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_release"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def build_release():
    return _load_build_release()


@pytest.fixture(scope="module")
def version_sites(build_release):
    hits, errors = build_release.collect_version_sites(ROOT)
    return hits, errors


# ------------------------------------------------------------------ 版本一致性

def test_product_version_is_readable_from_package_json(build_release):
    """权威来源必须可读且是非空字符串 —— 读不到就不能"猜一个"。"""
    version = build_release.read_product_version(ROOT)
    assert isinstance(version, str)
    assert version.strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", version), version


def test_every_version_site_is_present(version_sites):
    """位点被删掉 = 契约被绕过，所以每个位点都必须真的存在。"""
    _hits, errors = version_sites
    assert errors == [], "版本位点缺失/无法解析：\n" + "\n".join(errors)


def test_all_version_sites_match_product_version(build_release, version_sites):
    """核心契约：仓库内所有版本位点必须等于 package.json 的版本。"""
    hits, errors = version_sites
    product_version = build_release.read_product_version(ROOT)
    problems = build_release.compare_versions(hits, errors, product_version)
    assert problems == [], (
        f"版本不一致（权威版本 {product_version}）：\n" + "\n".join(problems)
    )


def test_version_sites_cover_the_six_required_locations(build_release):
    """六类必须被覆盖：package.json / electron-builder / Inno Setup /
    Python 包 / IPC 握手 / README+RELEASE_NOTES 文案。缺一类就是漏检。"""
    hits, _errors = build_release.collect_version_sites(ROOT)
    files = {hit.file for hit in hits}
    covered = {
        "package.json": "frontend/package.json" in files,
        "electron-builder 配置": "frontend/electron-builder.config.cjs" in files,
        "Inno Setup 脚本": "scripts/installer.iss" in files,
        "Python 包版本": "voxsub/__init__.py" in files,
        "IPC 握手": "frontend/backend/ipc_server.py" in files,
        "README": "README.md" in files or "README_EN.md" in files,
        "RELEASE_NOTES": "RELEASE_NOTES.md" in files,
    }
    missing = [name for name, present in covered.items() if not present]
    assert missing == [], f"版本位点覆盖不全，缺少：{missing}"


def test_cross_file_consultation_versions_agree(version_sites):
    """不经过 build_release 模块的独立复算 —— 防止守卫函数本身写错而一起骗过。"""
    hits, _errors = version_sites
    by_file: dict[str, str] = {}
    for hit in hits:
        if hit.derived:
            continue
        by_file.setdefault(hit.file, hit.value)

    pkg = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    init = (ROOT / "voxsub" / "__init__.py").read_text(encoding="utf-8")
    iss = (ROOT / "scripts" / "installer.iss").read_text(encoding="utf-8")
    notes = (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    init_m = re.search(r'^__version__\s*=\s*"([^"]+)"', init, re.MULTILINE)
    iss_m = re.search(r'^#define\s+MyAppVersion\s+"([^"]+)"', iss, re.MULTILINE)
    iss_out = re.search(r'^OutputBaseFilename=VoxSub-Setup-(.+)$', iss, re.MULTILINE)
    notes_m = re.search(r"^##\s*版本:\s*v?([^\s（(]+)", notes, re.MULTILINE)
    readme_m = re.search(r"当前源码候选版本：`([^`]+)`", readme)

    for label, match in (
        ("voxsub/__init__.py", init_m),
        ("scripts/installer.iss MyAppVersion", iss_m),
        ("scripts/installer.iss OutputBaseFilename", iss_out),
        ("RELEASE_NOTES.md 版本标题", notes_m),
        ("README.md 候选版本声明", readme_m),
    ):
        assert match is not None, f"{label} 里找不到版本号"
        assert match.group(1) == pkg["version"], (
            f"{label} = {match.group(1)!r}，package.json = {pkg['version']!r}"
        )

    for file, value in by_file.items():
        assert value == pkg["version"], f"{file} = {value!r}，package.json = {pkg['version']!r}"


# ---------------------------------------------------------- 安装器 / 打包配置

def test_electron_builder_artifact_name_is_derived_from_version():
    """安装包文件名必须用 ${version} 模板派生，不能写死版本号。

    写死就意味着改版本时这里会被漏掉，而产物名错误很难在发布前被发现。
    """
    config = (ROOT / "frontend" / "electron-builder.config.cjs").read_text(encoding="utf-8")
    match = re.search(r'artifactName:\s*"([^"]+)"', config)
    assert match is not None, "electron-builder 配置里找不到 artifactName"
    artifact = match.group(1)
    assert "${version}" in artifact, f"artifactName 没有派生版本：{artifact!r}"
    assert not re.search(r"\d+\.\d+\.\d+", artifact), (
        f"artifactName 里写死了版本号：{artifact!r}"
    )


def test_installer_output_base_filename_matches_app_version():
    """Inno Setup 里两处版本（宏 + 输出文件名）必须同源。"""
    iss = (ROOT / "scripts" / "installer.iss").read_text(encoding="utf-8")
    macro = re.search(r'^#define\s+MyAppVersion\s+"([^"]+)"', iss, re.MULTILINE)
    output = re.search(r"^OutputBaseFilename=VoxSub-Setup-(.+)$", iss, re.MULTILINE)
    assert macro is not None and output is not None
    assert output.group(1) == macro.group(1), (
        f"OutputBaseFilename 版本 {output.group(1)!r} != MyAppVersion {macro.group(1)!r}"
    )


def test_installer_output_base_filename_uses_the_macro():
    """更强的一版：输出文件名应当引用 {#MyAppVersion} 而不是再抄一遍字面量。

    这条是"应该做但当前没做"的改进项 —— 用 xfail 标注，不伪装成已通过。
    """
    iss = (ROOT / "scripts" / "installer.iss").read_text(encoding="utf-8")
    output = re.search(r"^OutputBaseFilename=(.+)$", iss, re.MULTILINE)
    assert output is not None
    if "{#MyAppVersion}" in output.group(1):
        return
    pytest.xfail(
        "installer.iss 的 OutputBaseFilename 仍写死版本字面量（"
        f"{output.group(1)!r}）；当前由版本门禁兜住，建议改为 "
        "OutputBaseFilename=VoxSub-Setup-{#MyAppVersion}"
    )


def test_packaging_test_does_not_invoke_a_real_build():
    """本文件必须保持"不打包"的性质 —— 不许有任何子进程调用入口。

    用 AST 而不是文本搜索：第一版用字面量 needle 做文本匹配，结果这条测试
    匹配到了它自己（forbidden 列表里就写着那个字符串），属于自指误报。
    AST 只看真实语法结构，天然免疫。
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert "subprocess" not in imported, "test_packaging.py 不应导入 subprocess"
    assert "os" not in imported, "test_packaging.py 不应导入 os（os.system 可绕）"

    banned = {"subprocess.run", "subprocess.Popen", "os.system", "os.popen", "os.spawnv"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called = ast.unparse(node.func)
            assert called not in banned, f"test_packaging.py 不应调用 {called}"


# ---------------------------------------------------------------- 门禁本身健全

def test_build_release_has_a_version_gate(build_release):
    """build-release.py 必须真的带版本门禁（不是只有注释）。"""
    source = BUILD_RELEASE_PY.read_text(encoding="utf-8")
    assert "def check_versions(" in source
    assert "check_versions()" in source, "定义了版本门禁但主流程没有调用"


def test_build_release_installer_glob_filters_by_version():
    """产物收集必须按版本过滤，不能再裸 glob 全部 exe。"""
    source = BUILD_RELEASE_PY.read_text(encoding="utf-8")
    assert 'glob("*.exe")' in source, "选择器实现变了，同步更新这条断言"
    assert "def select_installers(" in source
    # 裸调用（不经过 select_installers）应当已经消失
    bare = re.findall(r"RELEASE_DIR\.glob\(\"\*\.exe\"\)", source)
    assert bare == [], "仍有未按版本过滤的 RELEASE_DIR.glob('*.exe') 调用"


def test_build_release_writes_a_manifest():
    source = BUILD_RELEASE_PY.read_text(encoding="utf-8")
    assert "def build_manifest(" in source
    assert "def write_manifest(" in source
    assert "release-manifest-" in source
    assert '"lockfiles_sha256"' in source
    assert '"protocol_version"' in source
    assert '"commit"' in source
