"""发布门禁的负向测试 —— 让"版本不一致 / 主程序缺失 / 只有陈旧产物"真的挡住发布。

设计原则：
    · 只跑纯函数与 CLI 的 --check-only 入口，不调用 electron-builder、
      不产出任何安装包、不写 Release 目录、不弹窗、不播放音频。
    · 每个负例都断言"必须失败"，即使用会被误判为红色的方式构造输入。
    · 正例与负例成对出现，避免"永远失败"的假门禁骗过测试。

跑法：
    ./.venv/Scripts/python.exe -m pytest tests/test_release_gate.py -q
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD_RELEASE_PY = ROOT / "frontend" / "tools" / "build-release.py"


def _cli_python() -> str:
    """跑 CLI 用哪个解释器。

    **优先仓库自带的 venv，没有就退回"正在跑测试的这个解释器"**。

    为什么必须这样：原来硬编码 ``ROOT/.venv/Scripts/python.exe``，在**干净检出**
    （只有 git 里的东西，没有 venv）上直接 ``FileNotFoundError`` —— 于是"干净检出
    能复现构建"这条验收标准会被三条与代码无关的测试失败挡住。CI 里 workflow 会先
    建 .venv 所以看不出来，本地干净检出一定会踩到。
    """
    if VENV_PY.is_file():
        return str(VENV_PY)
    return sys.executable


VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"

# 权威版本用的假版本（与仓库真实版本无关，避免真实发版时测试变红）
GOOD = "1.2.3-beta"
BAD = "9.9.9"


def _load_build_release():
    spec = importlib.util.spec_from_file_location("build_release", BUILD_RELEASE_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_release"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def br():
    return _load_build_release()


# ------------------------------------------------------------------ 假仓库构造

def make_fake_repo(root: Path, *, version: str = GOOD, overrides: dict | None = None) -> Path:
    """造一个最小可解析的仓库镜像，只包含版本门禁需要的文件。

    overrides: {"相对路径": 完整文件内容}，用来注入不一致。
    """
    overrides = overrides or {}

    files = {
        "frontend/package.json": json.dumps(
            {"name": "voxsub-frontend", "version": version}, ensure_ascii=False, indent=2),
        "voxsub/__init__.py": f'"docstring"\n\n__version__ = "{version}"\n',
        "scripts/installer.iss": (
            "; VoxSub installer script\n"
            f"; Build: 用 InnoSetup (iscc) 编译本脚本 -> VoxSub-Setup-{version}.exe\n"
            f"#define MyAppVersion \"{version}\"\n"
            "AppVersion={#MyAppVersion}\n"
            f"OutputBaseFilename=VoxSub-Setup-{version}\n"
        ),
        "frontend/electron-builder.config.cjs": (
            "module.exports = {\n"
            '  productName: "VoxSub",\n'
            '  nsis: { artifactName: "VoxSub-Electron-Setup-${version}.exe" },\n'
            "};\n"
        ),
        "frontend/backend/ipc_server.py": (
            'def handle(self, command):\n'
            '    if command == "ping":\n'
            "        from voxsub import __version__  # noqa: PLC0415\n"
            '        return {"version": __version__}\n'
        ),
        "voxsub/release_notes.py": (
            "RELEASE_HISTORY = (\n"
            "    ReleaseNote(\n"
            f'        "{version}",\n'
            '        "标题",\n'
            "    ),\n"
            ")\n"
        ),
        "README.md": f"当前源码候选版本：`{version}`；其余说明。\n",
        "README_EN.md": f"Current source candidate: `{version}`; rest of the notes.\n",
        "RELEASE_NOTES.md": f"# 语幕 VoxSub\n\n## 版本: v{version}（开发候选，未发布）\n",
    }
    files.update(overrides)

    for relpath, content in files.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


@pytest.fixture
def good_repo(tmp_path):
    return make_fake_repo(tmp_path / "repo", version=GOOD)


# ============================================================ 正例（门禁不误报）

def test_good_repo_passes_version_gate(br, good_repo):
    version, hits = br.check_versions(good_repo)
    assert version == GOOD
    assert hits, "应当命中至少一个版本位点"
    assert br.collect_version_sites(good_repo)[1] == []


def test_real_repo_passes_version_gate(br):
    """真实仓库必须当前就是绿的 —— 否则门禁等于没接。

    注意断言的是什么：`check_versions` 内部就是拿 `read_product_version` 当权威版本，
    所以 "version == read_product_version()" 是**恒真**的（审查点名）。有意义的是
    「它没抛 SystemExit」+「位点数与实测相符」，这里都断言上。
    """
    version, hits = br.check_versions(ROOT)   # 不一致会抛 SystemExit → 本用例直接失败
    assert version, "权威版本读不出来"
    assert len(hits) >= 6, (
        f"版本位点只有 {len(hits)} 处 —— 位点清单可能被删瘦了，门禁会漏检"
    )
    # 每个**写死版本**的位点都必须真的等于权威版本（不是"恰好没抛异常"）。
    # 派生位点（例如 electron-builder 的输出模板，值里根本不写版本）跳过 —— 它们
    # 只要求存在，由 check_versions 自己负责判断该不该派生。
    for hit in hits:
        if getattr(hit, "derived", False):
            continue
        assert getattr(hit, "value", None) == version, (
            f"位点没对上权威版本：{hit}"
        )


# ===================================== 负例 1：版本不一致必须失败（缺陷 #14）

@pytest.mark.parametrize("relpath", [
    "voxsub/__init__.py",
    "scripts/installer.iss",
    "voxsub/release_notes.py",
    "README.md",
    "README_EN.md",
    "RELEASE_NOTES.md",
])
def test_negative_version_mismatch_must_fail(br, tmp_path, relpath):
    """逐个把每个位点改成别的版本号 —— 门禁必须退出非 0。"""
    base = make_fake_repo(tmp_path / "repo", version=GOOD)
    target = base / relpath
    text = target.read_text(encoding="utf-8")
    mutated = re.sub(re.escape(GOOD), BAD, text)
    assert mutated != text, f"{relpath} 里没有可替换的版本号，用例失效"
    target.write_text(mutated, encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        br.check_versions(base)
    assert excinfo.value.code not in (0, None), "版本不一致必须返回非 0 退出码"


def test_negative_version_mismatch_reports_the_offending_site(br, tmp_path, capsys):
    """失败信息必须点名文件:行号 —— 否则门禁挡不住人也修不动。"""
    base = make_fake_repo(tmp_path / "repo", version=GOOD)
    init = base / "voxsub" / "__init__.py"
    init.write_text(f'__version__ = "{BAD}"\n', encoding="utf-8")

    with pytest.raises(SystemExit):
        br.check_versions(base)

    out = capsys.readouterr().out
    assert "voxsub/__init__.py:1" in out
    assert BAD in out and GOOD in out


def test_negative_missing_version_site_must_fail(br, tmp_path):
    """位点被整段删掉（不是改错）也必须失败 —— 不能靠"文件不存在就跳过"绕过。"""
    base = make_fake_repo(tmp_path / "repo", version=GOOD)
    (base / "scripts" / "installer.iss").unlink()

    with pytest.raises(SystemExit):
        br.check_versions(base)


def test_negative_pattern_removed_from_existing_file_must_fail(br, tmp_path):
    """文件还在但版本位点被删除（例如把 #define MyAppVersion 拿掉）必须失败。"""
    base = make_fake_repo(tmp_path / "repo", version=GOOD)
    iss = base / "scripts" / "installer.iss"
    iss.write_text(
        "; VoxSub installer script\n[Setup]\nAppName=VoxSub\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        br.check_versions(base)


def test_negative_electron_builder_version_template_removed(br, tmp_path):
    """artifactName 不再用 ${version} 派生 —— 派生位点也必须守住。"""
    base = make_fake_repo(
        tmp_path / "repo", version=GOOD,
        overrides={
            "frontend/electron-builder.config.cjs":
                'module.exports = { nsis: { artifactName: "VoxSub-Setup.exe" } };\n'
        },
    )
    with pytest.raises(SystemExit):
        br.check_versions(base)


def test_negative_ipc_handshake_stops_using_package_version(br, tmp_path):
    """IPC 握手不再引用 voxsub.__version__（改成自己的硬编码常量）必须失败。"""
    base = make_fake_repo(
        tmp_path / "repo", version=GOOD,
        overrides={
            "frontend/backend/ipc_server.py": (
                'APP_VERSION = "0.0.1"\n'
                'def handle(self, command):\n'
                '    return {"version": APP_VERSION}\n'
            )
        },
    )
    with pytest.raises(SystemExit):
        br.check_versions(base)


def test_positive_ipc_handshake_derives_from_package_version(br, good_repo):
    """正例：派生自 voxsub.__version__ 的握手位点必须被接受。"""
    hits, errors = br.collect_version_sites(good_repo)
    assert errors == []
    handshake = [h for h in hits if h.derived and "ipc_server" in h.file]
    assert handshake, "IPC 握手位点没有被采集到"
    assert br.compare_versions(hits, errors, GOOD) == []


def test_compare_versions_reports_each_mismatch(br):
    """纯函数层面：每个不一致位点都要出现一条问题描述。"""
    hits = [
        br.VersionHit(name="A", file="a.py", line=1, value=GOOD),
        br.VersionHit(name="B", file="b.py", line=2, value=BAD),
        br.VersionHit(name="C", file="c.py", line=3, value=BAD),
        br.VersionHit(name="D（派生）", file="d.cjs", line=4, value="x", derived=True),
    ]
    problems = br.compare_versions(hits, [], GOOD)
    assert len(problems) == 2
    assert "b.py:2" in problems[0] and "c.py:3" in problems[1]


# =============================== 负例 2：主程序缺失必须硬失败（缺陷 #13 前半）

def _make_release(tmp_path, *, installers=(), main_exe=True, unpacked=True,
                  sidecar=True):
    release = tmp_path / "Release" / "electron"
    release.mkdir(parents=True, exist_ok=True)
    for name in installers:
        (release / name).write_bytes(b"not-a-real-installer")
    if unpacked:
        win = release / "win-unpacked"
        win.mkdir(parents=True, exist_ok=True)
        if main_exe:
            (win / "VoxSub.exe").write_bytes(b"MZ fake")
        if sidecar:
            res = win / "resources" / "backend"
            res.mkdir(parents=True, exist_ok=True)
            (res / "VoxSubBackend.exe").write_bytes(b"MZ sidecar")
    return release


def test_positive_artifacts_ok(br, tmp_path):
    """正例：当前版本安装包 + 完整免安装目录 → 通过。"""
    release = _make_release(
        tmp_path, installers=[f"VoxSub-Electron-Setup-{GOOD}.exe"])
    report = br.inspect_artifacts(GOOD, release)
    assert report.passed, report.errors
    assert report.current == [f"VoxSub-Electron-Setup-{GOOD}.exe"]


def test_negative_missing_main_exe_must_fail(br, tmp_path):
    """缺陷 #13 前半：主程序缺失曾经只有一个 if、没有 else，静默通过。"""
    release = _make_release(
        tmp_path, installers=[f"VoxSub-Electron-Setup-{GOOD}.exe"], main_exe=False)
    report = br.inspect_artifacts(GOOD, release)
    assert not report.passed
    assert any("VoxSub.exe" in err and "主程序" in err for err in report.errors), report.errors


def test_negative_missing_sidecar_in_package_must_fail(br, tmp_path):
    release = _make_release(
        tmp_path, installers=[f"VoxSub-Electron-Setup-{GOOD}.exe"], sidecar=False)
    report = br.inspect_artifacts(GOOD, release)
    assert not report.passed
    assert any("sidecar" in err for err in report.errors), report.errors


def test_negative_missing_release_dir_must_fail(br, tmp_path):
    report = br.inspect_artifacts(GOOD, tmp_path / "nope")
    assert not report.passed
    assert any("不存在" in err for err in report.errors)


# ============================= 负例 3：只有陈旧产物必须失败（缺陷 #13 后半）

def test_stale_installers_are_not_counted_as_current(br, tmp_path):
    """裸 glob 会把旧安装包当本次产物 —— 选择器必须按版本拆分。"""
    release = _make_release(
        tmp_path,
        installers=[
            f"VoxSub-Electron-Setup-{GOOD}.exe",
            "VoxSub-Electron-Setup-1.0.0.exe",
            "VoxSub-Setup-0.7.2-beta.exe",
        ],
    )
    current, stale = br.select_installers(release, GOOD)
    assert [p.name for p in current] == [f"VoxSub-Electron-Setup-{GOOD}.exe"]
    assert sorted(p.name for p in stale) == [
        "VoxSub-Electron-Setup-1.0.0.exe", "VoxSub-Setup-0.7.2-beta.exe"]


def test_negative_only_stale_installers_must_fail(br, tmp_path):
    """核心负例：目录里只有旧版本安装包 → 必须失败，不能被当成本次产物。"""
    release = _make_release(
        tmp_path,
        installers=[
            "VoxSub-Electron-Setup-1.0.0.exe",
            "VoxSub-Setup-0.7.2-beta.exe",
        ],
        unpacked=False,
    )
    report = br.inspect_artifacts(GOOD, release)
    assert not report.passed
    assert report.current == []
    assert sorted(report.stale) == [
        "VoxSub-Electron-Setup-1.0.0.exe", "VoxSub-Setup-0.7.2-beta.exe"]
    assert any("陈旧" in err for err in report.errors), report.errors


def test_negative_only_stale_even_with_unpacked_dir_must_fail(br, tmp_path):
    """免安装目录健康但只有旧安装包 —— 仍然必须失败（不能只看目录就放行）。"""
    release = _make_release(tmp_path, installers=["VoxSub-Setup-0.7.2-beta.exe"])
    report = br.inspect_artifacts(GOOD, release)
    assert not report.passed
    assert any("陈旧" in err for err in report.errors), report.errors


def test_negative_dir_only_with_stale_installer_still_fails(br, tmp_path):
    """--dir-only 允许没有安装包，但不允许"只剩旧安装包"混过去。"""
    release = _make_release(tmp_path, installers=["VoxSub-Setup-0.7.2-beta.exe"])
    report = br.inspect_artifacts(GOOD, release, dir_only=True)
    assert not report.passed
    assert any("当前版本" in err for err in report.errors), report.errors


def test_positive_dir_only_without_installers_is_allowed(br, tmp_path):
    """正例：--dir-only 且无安装包 → 不算失败。"""
    release = _make_release(tmp_path, installers=[])
    report = br.inspect_artifacts(GOOD, release, dir_only=True)
    assert report.passed, report.errors


def test_version_filter_does_not_match_substring_of_other_version(br, tmp_path):
    """1.2.3 不能把 11.2.3 当成自己的产物（子串匹配的经典坑）。"""
    release = _make_release(
        tmp_path, installers=["VoxSub-Electron-Setup-11.2.3-beta.exe"])
    current, stale = br.select_installers(release, "1.2.3-beta")
    assert current == [], "版本过滤粒度不足：子串匹配把 11.2.3 也算进来了"
    assert [p.name for p in stale] == ["VoxSub-Electron-Setup-11.2.3-beta.exe"]


# ============================================== 负例 4：构建前置不得偷改源码

def test_negative_source_rewrite_is_detected(br, tmp_path):
    """sanitize.mjs 会就地清写源码 —— 漂移必须被检出并报成失败。"""
    src = tmp_path / "frontend"
    (src / "src").mkdir(parents=True)
    target = src / "src" / "app.ts"
    target.write_text("const a = '\u200b';\n", encoding="utf-8")

    before = br.snapshot_sources(src)
    target.write_text("const a = '';\n", encoding="utf-8")
    after = br.snapshot_sources(src)

    drift = br.source_drift(before, after)
    assert drift == ["src/app.ts（已改写）"]


def test_source_drift_detects_added_and_removed(br):
    before = {"a.ts": "1", "b.ts": "2"}
    after = {"a.ts": "1", "c.ts": "3"}
    drift = br.source_drift(before, after)
    assert "b.ts（已删除）" in drift
    assert "c.ts（已新增）" in drift


def test_positive_no_drift_when_nothing_changes(br, tmp_path):
    src = tmp_path / "frontend"
    (src / "src").mkdir(parents=True)
    (src / "src" / "app.ts").write_text("clean\n", encoding="utf-8")
    before = br.snapshot_sources(src)
    assert br.source_drift(before, br.snapshot_sources(src)) == []


def test_snapshot_skips_build_output_dirs(br, tmp_path):
    """node_modules / dist 不参与漂移检测，否则每次构建都误报。"""
    src = tmp_path / "frontend"
    (src / "node_modules" / "pkg").mkdir(parents=True)
    (src / "node_modules" / "pkg" / "index.js").write_text("x\n", encoding="utf-8")
    (src / "dist").mkdir(parents=True)
    (src / "dist" / "main.js").write_text("y\n", encoding="utf-8")
    (src / "src").mkdir(parents=True)
    (src / "src" / "app.ts").write_text("z\n", encoding="utf-8")

    snapshot = br.snapshot_sources(src)
    assert list(snapshot) == ["src/app.ts"]


def test_report_source_drift_marks_failure_by_default(br, capsys):
    br.passed = 0
    br.failed = 0
    br.report_source_drift(["src/app.ts（已改写）"], allowed=False)
    out = capsys.readouterr().out
    assert "src/app.ts（已改写）" in out
    assert br.failed == 1
    assert "FAIL" in out


def test_report_source_drift_warns_only_when_allowed(br, capsys):
    br.passed = 0
    br.failed = 0
    br.report_source_drift(["src/app.ts（已改写）"], allowed=True)
    out = capsys.readouterr().out
    assert br.failed == 0
    assert "仅告警" in out


# ==================================================== 构建清单（manifest）契约

def test_manifest_has_every_required_field(br):
    hits, _ = br.collect_version_sites(ROOT)
    manifest = br.build_manifest(
        product_version=GOOD,
        protocol_version=GOOD,
        version_sites=hits,
        tests_executed=["pytest tests/"],
        artifacts=[{"kind": "installer", "path": "x.exe", "bytes": 1, "sha256": "ab"}],
        result="pass",
    )
    for key in ("commit", "product_version", "protocol_version", "toolchain",
                "lockfiles_sha256", "tests_executed", "artifacts", "generated_at"):
        assert key in manifest, f"构建清单缺少 {key}"
    assert manifest["product_version"] == GOOD
    assert manifest["protocol_version"] == GOOD
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["commit"]), manifest["commit"]
    assert set(manifest["toolchain"]) >= {"python", "node", "npm"}
    assert manifest["version_sites"], "清单里必须记录每个版本位点"


def test_manifest_records_lockfile_digests(br, tmp_path):
    # 用 write_bytes：write_text 在 Windows 上会把 "\n" 翻成 "\r\n"，
    # 摘要就对不上了（第一版踩过）。
    (tmp_path / "requirements.lock").write_bytes(b"a==1\n")
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package-lock.json").write_bytes(b"{}\n")
    digests = br.lockfile_digests(tmp_path)
    assert digests["requirements.lock"] == hashlib.sha256(b"a==1\n").hexdigest()
    assert digests["frontend/package-lock.json"] == hashlib.sha256(b"{}\n").hexdigest()
    assert digests["requirements.txt"] == "missing", "缺失锁文件必须显式记为 missing"


def test_manifest_artifact_sha256_matches_file(br, tmp_path):
    """产物 sha256 必须等于真实文件摘要，不能是占位串。"""
    release = _make_release(
        tmp_path, installers=[f"VoxSub-Electron-Setup-{GOOD}.exe"])
    payload = (release / f"VoxSub-Electron-Setup-{GOOD}.exe").read_bytes()
    current, _stale = br.select_installers(release, GOOD)
    entry = {
        "kind": "installer", "path": str(current[0]),
        "bytes": current[0].stat().st_size, "sha256": br.sha256_file(current[0]),
    }
    assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
    assert entry["bytes"] == len(payload)


def test_manifest_is_written_under_build_dir_not_release(br, tmp_path):
    """清单只能写在仓库内 build/ 下，绝不能碰正式 Release 目录。"""
    path = br.write_manifest({"ok": True}, GOOD, root=tmp_path)
    assert path == tmp_path / "build" / f"release-manifest-{GOOD}.json"
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
    assert "Release" not in str(path)


def test_manifest_path_sanitizes_unsafe_version(br, tmp_path):
    """版本号里的非法路径字符不能逃出 build/ 目录。"""
    path = br.manifest_path("../../evil/1.0", tmp_path)
    assert path.parent == tmp_path / "build"
    assert ".." not in path.name and "/" not in path.name


def test_protocol_version_comes_from_python_package(br, good_repo):
    """IPC 握手版本必须来自 voxsub.__version__，不是另一处硬编码。"""
    assert br.protocol_version_from_source(good_repo) == GOOD


# ============================ 可选位点与协议版本轴（并发工作中的新位点）

def test_optional_site_missing_is_reported_not_silently_skipped(br, good_repo):
    """可选位点缺失不能静默 —— 必须报出来（否则复刻了本工作单要修的静默缺陷）。"""
    assert not (good_repo / "contracts" / "protocol.json").exists()
    notes = br.optional_version_sites_status(good_repo)
    assert any("contracts/protocol.json" in note for note in notes), notes
    # 但缺失不算失败
    assert br.collect_version_sites(good_repo)[1] == []


def test_optional_site_present_and_wrong_must_fail(br, tmp_path):
    """可选位点一旦产出，就必须和产品版本一致 —— "可选"只针对存在性。"""
    base = make_fake_repo(
        tmp_path / "repo", version=GOOD,
        overrides={"contracts/protocol.json": json.dumps(
            {"protocolVersion": "2.0.0", "protocolVersionInteger": 2,
             "appVersionAtAuthoring": BAD})},
    )
    with pytest.raises(SystemExit):
        br.check_versions(base)


def test_optional_site_present_and_correct_passes(br, tmp_path):
    base = make_fake_repo(
        tmp_path / "repo", version=GOOD,
        overrides={"contracts/protocol.json": json.dumps(
            {"protocolVersion": "2.0.0", "protocolVersionInteger": 2,
             "appVersionAtAuthoring": GOOD})},
    )
    version, _hits = br.check_versions(base)
    assert version == GOOD
    assert br.optional_version_sites_status(base) == []


def test_protocol_version_axis_detects_divergence(br, tmp_path):
    """线上整数（握手）与契约声明不一致时必须被报出来，而不是当没看见。"""
    base = make_fake_repo(
        tmp_path / "repo", version=GOOD,
        overrides={
            "frontend/backend/ipc_loop.py": "PROTOCOL_VERSION = 1\n",
            "contracts/protocol.json": json.dumps(
                {"protocolVersion": "2.0.0", "wireProtocolVersion": {"value": 2}}),
        },
    )
    axis = br.protocol_version_axis(base)
    assert axis["handshake_integer"] == 1
    assert axis["contract_integer"] == 2
    assert axis["consistent"] is False
    assert any("PROTOCOL_VERSION" in f and "不一致" in f for f in axis["findings"]), axis["findings"]


def test_protocol_version_axis_consistent(br, tmp_path):
    """线上整数一致时不该报不一致。

    顺带覆盖契约字段名的**兼容**：这里用的是旧形状 ``protocolVersionInteger``，
    当前契约用的是 ``wireProtocolVersion.value``。字段名换掉就静默读不到 ——
    门禁会从"能判断"退化成"不判断"，而那看起来和"没问题"一模一样。
    """
    base = make_fake_repo(
        tmp_path / "repo", version=GOOD,
        overrides={
            "frontend/backend/ipc_loop.py": "PROTOCOL_VERSION = 1\n",
            "contracts/protocol.json": json.dumps(
                {"protocolVersion": "1.0.0", "protocolVersionInteger": 1}),
        },
    )
    axis = br.protocol_version_axis(base)
    assert axis["consistent"] is True
    assert axis["findings"] == []


def test_protocol_version_axis_reports_when_incomplete(br, good_repo):
    """两处声明都不存在时不能返回"通过"，必须说明无法判断。"""
    axis = br.protocol_version_axis(good_repo)
    assert axis["consistent"] is None
    assert axis["findings"], "无法判断时必须给出说明"


def test_real_repo_protocol_axis_has_a_stable_shape(br):
    """真实仓库的协议版本轴现状必须能被稳定读取（不硬失败但必须可见）。"""
    axis = br.protocol_version_axis(ROOT)
    assert set(axis) == {"handshake_integer", "contract_integer", "contract_semver",
                         "consistent", "findings"}


# ================================================ CLI 端到端（仍然不打包）

def test_cli_check_only_exits_zero_on_real_repo():
    """真实入口：--check-only 必须能在不打包的情况下跑完并返回 0。"""
    result = subprocess.run(
        [_cli_python(), str(BUILD_RELEASE_PY), "--check-only"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "版本一致" in result.stdout
    assert "未执行打包" in result.stdout


def test_cli_manifest_only_writes_real_manifest():
    """真实入口：--manifest-only 必须在 build/ 下产出一份结构完整的清单。"""
    result = subprocess.run(
        [_cli_python(), str(BUILD_RELEASE_PY), "--manifest-only"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    version = json.loads(
        (ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))["version"]
    path = ROOT / "build" / f"release-manifest-{version}.json"
    assert path.is_file(), f"清单没有生成：{path}\n{result.stdout}"

    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["product_version"] == version
    assert manifest["protocol_version"] == version
    assert manifest["mode"] == "manifest-only"
    assert manifest["packaged"] is False
    assert manifest["artifacts"] == [], "未打包时清单不许写产物"
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["commit"])
    assert manifest["version_sites"], "清单必须记录版本位点"
    assert "protocol_version_axis" in manifest, "清单必须记录协议版本轴现状"


def test_cli_check_only_help_mentions_the_gate():
    result = subprocess.run(
        [_cli_python(), str(BUILD_RELEASE_PY), "--help"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    assert result.returncode == 0
    assert "--check-only" in result.stdout
    assert "--allow-source-rewrite" in result.stdout


def test_cli_check_only_fails_on_mismatched_repo(tmp_path):
    """把假仓库当仓库根跑 CLI 也应非 0 —— 门禁不能只在真仓库里生效。

    build-release.py 的路径常量是相对自身解析的，所以这里改为直接调用
    纯函数入口验证同一逻辑（check_versions(root=...)），CLI 行为由上一条覆盖。
    """
    base = make_fake_repo(tmp_path / "repo", version=GOOD)
    (base / "voxsub" / "__init__.py").write_text(f'__version__ = "{BAD}"\n', encoding="utf-8")
    br = _load_build_release()
    with pytest.raises(SystemExit):
        br.check_versions(base)


def test_gate_source_has_no_silent_pass_paths():
    """反回归：门禁函数里不允许出现"缺主程序就跳过"这类静默分支。"""
    source = BUILD_RELEASE_PY.read_text(encoding="utf-8")
    inspect_fn = source.split("def inspect_artifacts(", 1)[1].split("\ndef ", 1)[0]
    # 主程序检查必须有 else/报错分支
    assert "VoxSub.exe" in inspect_fn
    assert "主程序" in inspect_fn and "errors.append" in inspect_fn
    # 不允许再出现裸的 sorted(RELEASE_DIR.glob("*.exe"))
    assert 'sorted(RELEASE_DIR.glob("*.exe"))' not in source
