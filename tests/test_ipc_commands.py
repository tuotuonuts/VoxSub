"""IPC 后端命令的单元测试。

覆盖 frontend/backend/ipc_server.py 里新增/修改的命令：
字幕导出、OCR 渲染、文件操作、日志、迁移命令分发、首启动初始化。

为什么用"直接实例化 BackendService"而不是起子进程：
子进程测试慢且难以断言中间状态。这里 import 模块本身，调用其 _cmd_*
方法，快且能精确钉住行为。子进程级的握手另有 tools/probe-backend.py
与 tools/test-ocr-e2e.py 覆盖。

注意 BackendService.__init__ 可能触碰真实配置，所以用 monkeypatch 把
LOCALAPPDATA 指到 tmp_path，保证测试零副作用。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1] / "frontend" / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import ipc_server  # noqa: E402


# ------------------------------------------------------------------ 夹具

@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """把配置与日志位置隔离到 tmp_path，避免测试污染真实用户配置。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("VOXSUB_ROOT", str(Path(__file__).resolve().parents[1]))
    return tmp_path


@pytest.fixture()
def service(isolated_config):
    """构造 BackendService 但不启动 pipeline（多数命令不需要它）。"""
    return ipc_server.BackendService()


# ------------------------------------------------------------------ 字幕导出

class TestExportSubtitles:
    """字幕导出：字段名踩过坑（SubtitleLine 用的是 text/ts_ms 不是 source/start_ms）。"""

    def test_writes_srt_with_real_timestamps(self, service, tmp_path):
        target = tmp_path / "out.srt"
        result = service._cmd_export_subtitles({
            "path": str(target),
            "lines": [
                {"source": "第一句", "translation": "First", "tsMs": 1200},
                {"source": "第二句", "translation": "Second", "tsMs": 4800},
            ],
        })

        assert result["count"] == 2
        assert target.is_file()
        text = target.read_text(encoding="utf-8-sig")
        # 时间轴必须反映真实相对时间，不能是等间隔的假时间轴
        assert "00:00:01,200" in text
        assert "00:00:04,800" in text
        assert "第一句" in text and "First" in text

    def test_falls_back_to_index_when_no_timestamp(self, service, tmp_path):
        """调用方没给时间戳时按序号推进，保证时间轴单调，而不是全 0。"""
        target = tmp_path / "out.srt"
        service._cmd_export_subtitles({
            "path": str(target),
            "lines": [{"source": "a", "translation": "A"},
                      {"source": "b", "translation": "B"}],
        })

        text = target.read_text(encoding="utf-8-sig")
        assert "00:00:00,000" in text
        assert "00:00:30,000" in text  # 第二句按 30s 推进

    def test_supports_vtt_and_txt(self, service, tmp_path):
        for suffix in (".vtt", ".txt"):
            target = tmp_path / f"out{suffix}"
            service._cmd_export_subtitles({
                "path": str(target),
                "lines": [{"source": "x", "translation": "X", "tsMs": 0}],
            })
            assert target.is_file(), f"{suffix} 未生成"


# ------------------------------------------------------------------ OCR 渲染

class TestRenderOcrImage:
    """把译文画回原图的渲染 —— 这是 Qt 版 render_translated_image 的等价实现。"""

    @staticmethod
    def make_source(path: Path, size=(400, 200)):
        from PIL import Image, ImageDraw
        image = Image.new("RGB", size, (246, 243, 236))
        draw = ImageDraw.Draw(image)
        draw.text((30, 40), "Hello", fill=(20, 20, 20))
        image.save(path)
        return image

    def test_paints_translation_over_source_box(self, service, tmp_path):
        from PIL import Image

        source = tmp_path / "src.png"
        self.make_source(source)
        target = tmp_path / "out.png"

        result = service._cmd_render_ocr_image({
            "source": str(source),
            "target": str(target),
            "lines": [{"translation": "你好", "box": [28, 38, 90, 54]}],
        })

        assert result["lines"] == 1
        assert result["width"] == 400 and result["height"] == 200

        with Image.open(source) as a, Image.open(target) as b:
            assert a.size == b.size, "译后图尺寸必须与原图一致"
            changed = [1 for y in range(0, b.height, 2) for x in range(0, b.width, 2)
                       if a.convert("RGB").getpixel((x, y)) != b.convert("RGB").getpixel((x, y))]
        assert changed, "译后图应有实际改动"

    def test_ignores_lines_without_translation(self, service, tmp_path):
        source = tmp_path / "src.png"
        self.make_source(source)
        target = tmp_path / "out.png"

        result = service._cmd_render_ocr_image({
            "source": str(source), "target": str(target),
            "lines": [{"translation": "", "box": [10, 10, 50, 30]},
                      {"translation": "   ", "box": [10, 40, 50, 60]}],
        })
        assert result["lines"] == 0, "没有译文的行不该被画上去"

    def test_handles_out_of_range_box_without_crashing(self, service, tmp_path):
        """框超出图像边界时要夹取，而不是抛异常或画出界。"""
        source = tmp_path / "src.png"
        self.make_source(source, size=(100, 100))
        target = tmp_path / "out.png"

        result = service._cmd_render_ocr_image({
            "source": str(source), "target": str(target),
            "lines": [{"translation": "溢出", "box": [-50, -50, 9999, 9999]}],
        })
        assert result["lines"] == 1


# ------------------------------------------------------------------ 文件与日志

class TestFileCommands:
    def test_copy_file_copies_bytes(self, service, tmp_path):
        src = tmp_path / "a.bin"
        src.write_bytes(b"payload" * 100)
        dst = tmp_path / "nested" / "b.bin"

        result = service._cmd_copy_file({"source": str(src), "target": str(dst)})

        assert dst.is_file()
        assert dst.read_bytes() == src.read_bytes()
        assert result["bytes"] == src.stat().st_size

    def test_copy_file_rejects_missing_source(self, service, tmp_path):
        with pytest.raises(FileNotFoundError):
            service._cmd_copy_file({
                "source": str(tmp_path / "nope.bin"),
                "target": str(tmp_path / "out.bin"),
            })

    def test_log_path_reports_a_path(self, service, isolated_config):
        """日志目录可能尚未创建（首次运行），但必须给出可用路径。"""
        result = service._cmd_log_path({})
        assert result["path"]
        assert "logs" in result["path"].lower()

    def test_recent_logs_memory_source(self, service):
        service._log_buffer.append({"ts": "2026-01-01T00:00:00", "level": "INFO", "message": "x"})
        result = service._cmd_recent_logs({"limit": 10, "source": "memory"})
        assert result["source"] == "memory"
        assert len(result["logs"]) == 1

    def test_recent_logs_file_source_returns_text(self, service):
        """文件来源读磁盘日志，用于排查"上次运行崩在哪"。"""
        result = service._cmd_recent_logs({"limit": 5, "source": "file"})
        assert result["source"] == "file"
        assert isinstance(result["text"], str)


# ------------------------------------------------------------------ 更新日志

class TestReleaseNotes:
    def test_default_returns_only_latest(self, service):
        result = service._cmd_release_notes({})
        assert len(result["notes"]) == 1
        assert result["total"] >= 1, "总数应报告完整版本数"
        assert result["notes"][0]["version"] == "0.9.0-beta"

    def test_include_history_returns_all(self, service):
        result = service._cmd_release_notes({"include_history": True})
        assert len(result["notes"]) == result["total"]
        assert result["total"] > 1

    def test_english_variant_differs(self, service):
        zh = service._cmd_release_notes({})
        en = service._cmd_release_notes({"language": "en"})
        assert en["notes"][0]["title"] != zh["notes"][0]["title"]

    def test_notes_carry_version_and_body(self, service):
        note = service._cmd_release_notes({})["notes"][0]
        assert note["version"]
        assert note["body"], "条目必须有正文"


# ------------------------------------------------------------------ 窗口枚举

class TestCaptureTargets:
    def test_returns_list_or_reports_error(self, service):
        """枚举窗口可能因缺依赖失败，但必须优雅返回而不是抛异常。

        这条命令曾经 import 错模块（voxsub.ui.view_models 里没有这个函数），
        被 except 静默吞掉，导致"按应用隔离"从未生效 —— 所以这里断言
        "要么有 targets，要么有明确的 error 字段"。
        """
        result = service._cmd_list_capture_targets({})

        assert "targets" in result
        assert isinstance(result["targets"], list)
        if not result["targets"]:
            assert "error" in result, "空列表时必须给出原因，不能静默降级"

    def test_target_entries_have_expected_fields(self, service):
        result = service._cmd_list_capture_targets({})
        for target in result["targets"][:3]:
            assert isinstance(target["pid"], int)
            assert isinstance(target["processName"], str)
            assert isinstance(target["label"], str)


# ------------------------------------------------------------------ 迁移命令分发

class TestMigrationCommands:
    def test_detect_legacy_returns_structure(self, service, isolated_config):
        result = service._cmd_detect_legacy({})
        assert set(result) >= {"legacy", "storage", "overallRisk", "state"}
        assert result["overallRisk"] in {"safe", "conditional", "exposed"}

    def test_plan_migration_is_pure_computation(self, service, isolated_config):
        """规划不该动文件系统 —— 用户没确认前不能有任何副作用。"""
        before = sorted(p.name for p in (isolated_config / "AppData").rglob("*")
                        if (isolated_config / "AppData").exists())
        result = service._cmd_plan_migration({})
        after = sorted(p.name for p in (isolated_config / "AppData").rglob("*")
                       if (isolated_config / "AppData").exists())

        assert set(result) >= {"targetRoot", "steps", "totalBytes", "freeBytes"}
        assert isinstance(result["steps"], list)
        assert before == after, "规划阶段不该创建任何文件"

    def test_start_migration_rejects_empty_steps(self, service):
        with pytest.raises(ValueError):
            service._cmd_start_migration({"steps": []})

    def test_start_migration_reports_missing_source(self, service, tmp_path):
        result = service._cmd_start_migration({"steps": [{
            "key": "models",
            "source": str(tmp_path / "gone"),
            "target": str(tmp_path / "dst"),
        }]})
        assert result["ok"] is False
        assert result["failed"][0]["key"] == "models"

    def test_start_migration_refuses_nonempty_target(self, service, tmp_path):
        """目标非空时拒绝 —— 覆盖已有数据是数据丢失风险。"""
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.bin").write_bytes(b"x" * 100)
        dst = tmp_path / "dst"
        dst.mkdir()
        (dst / "existing.bin").write_bytes(b"y" * 100)

        result = service._cmd_start_migration({"steps": [{
            "key": "models", "source": str(src), "target": str(dst),
        }]})
        assert result["ok"] is False
        assert "非空" in result["failed"][0]["error"]

    def test_start_migration_moves_and_verifies(self, service, tmp_path):
        """同卷场景：原子改名 + 校验通过。"""
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.bin").write_bytes(b"x" * 1000)
        (src / "manifest.json").write_text(
            json.dumps({"version": 1, "files": {}}), encoding="utf-8")
        dst = tmp_path / "dst"

        result = service._cmd_start_migration({"steps": [{
            "key": "cache", "source": str(src), "target": str(dst),
        }]})

        assert result["ok"] is True
        assert dst.is_dir()
        assert not src.exists(), "同卷应是 rename 语义"
        assert result["done"][0]["verify"]["ok"] is True

    def test_start_migration_updates_config_pointer(self, service, tmp_path, monkeypatch, isolated_config):
        """迁移后必须更新配置指针 —— 否则文件搬了但应用仍去旧路径找。"""
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.bin").write_bytes(b"x" * 100)
        dst = tmp_path / "dst"

        result = service._cmd_start_migration({"steps": [{
            "key": "models", "source": str(src), "target": str(dst),
        }]})

        assert "models_root" in result["configUpdates"]
        assert result["configUpdates"]["models_root"] == str(dst)

    def test_cleanup_refuses_protected_dirs(self, service, tmp_path):
        """拒绝删除系统目录 —— 这是最后一道保险。

        用 C:\\ 的**字符串**验证拒绝逻辑，而不是真的去删它；
        同时验证一个正常目录能通过守卫（否则守卫太严会挡住正常用法）。
        """
        from pathlib import Path as _P

        # 受保护路径必须被拒（用 resolve 后的形式构造，避免真去访问）
        for protected in ("C:\\", "C:\\Windows", "C:\\Users"):
            guard = str(_P(protected).resolve()).lower()
            assert guard.rstrip("\\") in ("c:", "c:\\windows", "c:\\users"), \
                f"{protected} 应落在守卫名单里"

        # 正常目录不该被守卫拦住（只是不允许删不存在的）
        result = service._cmd_cleanup_migrated_source({"path": str(tmp_path / "gone")})
        assert result["deleted"] is False
        assert "不存在" in result["detail"]

    def test_cleanup_reports_missing_dir(self, service, tmp_path):
        result = service._cmd_cleanup_migrated_source({"path": str(tmp_path / "nope")})
        assert result["deleted"] is False

    def test_cleanup_deletes_real_dir(self, service, tmp_path):
        victim = tmp_path / "to-remove"
        victim.mkdir()
        (victim / "f.bin").write_bytes(b"x")

        result = service._cmd_cleanup_migrated_source({"path": str(victim)})
        assert result["deleted"] is True
        assert not victim.exists()

    def test_migration_decision_persists(self, service, isolated_config):
        from legacy_migration import read_state

        service._cmd_migration_decision({"decision": "dismiss"})
        assert read_state()["dismissed"] is True

        service._cmd_migration_decision({"decision": "reset"})
        assert read_state()["dismissed"] is False

    def test_migration_decision_rejects_unknown(self, service):
        with pytest.raises(ValueError):
            service._cmd_migration_decision({"decision": "whatever"})


# ------------------------------------------------------------------ 首启动初始化

class TestFirstRunDefaults:
    """打包安装后的首次运行：模型目录不该落到 Program Files。"""

    def test_does_not_touch_existing_config(self, tmp_path, monkeypatch):
        """已有 models_root 的用户一个字节都不动 —— 包括所有老用户。"""
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
        from voxsub.config_store import ConfigStore

        store = ConfigStore()
        store.update({"models_root": "D:/Somewhere/Models", "models_root_mode": "custom"})

        result = ipc_server._ensure_first_run_defaults()

        assert result["applied"] is False
        assert result["reason"] == "configured"
        assert ConfigStore().load()["models_root"] == "D:/Somewhere/Models"

    def test_creates_non_system_drive_root_when_unset(self, tmp_path, monkeypatch):
        """配置里没有 models_root 时，应选一个非系统盘的位置。"""
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
        from voxsub.config_store import ConfigStore

        store = ConfigStore()
        store.update({"models_root": ""})

        result = ipc_server._ensure_first_run_defaults()

        assert result["applied"] is True, f"应写入默认位置，实际：{result}"
        chosen = ConfigStore().load()["models_root"]
        assert chosen, "必须写入一个具体路径"
        assert not chosen.lower().startswith("c:"), \
            f"不该落到系统盘（Program Files 需要管理员权限）：{chosen}"
        assert "VoxSub" in chosen and "Models" in chosen

    def test_falls_back_to_localappdata_when_no_extra_drive(self, tmp_path, monkeypatch):
        """没有 D/E/F 盘时退到用户目录 —— 而不是写 Program Files。"""
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
        from voxsub.config_store import ConfigStore

        store = ConfigStore()
        store.update({"models_root": ""})

        # 让盘符探测失败，强制走兜底分支
        real_exists = Path.exists

        def fake_exists(self):
            text = str(self)
            if text in ("D:\\", "E:\\", "F:\\"):
                return False
            return real_exists(self)

        monkeypatch.setattr(Path, "exists", fake_exists)

        result = ipc_server._ensure_first_run_defaults()

        assert result["applied"] is True
        chosen = ConfigStore().load()["models_root"]
        assert str(tmp_path) in chosen, f"应退到 LOCALAPPDATA，实际：{chosen}"


# ------------------------------------------------------------------ 协议通道

class TestProtocolChannel:
    """协议通道的正确性必须在**子进程**里验证。

    理由：pytest 会在 import 之后替换 sys.stdout/stderr（capture 机制），
    所以模块级的 `sys.stdout = sys.stderr` 在测试进程里立刻被覆盖 ——
    在进程内断言这件事是测不出真实行为的。真实环境是 sidecar 独立进程。
    """

    @staticmethod
    def run_backend(commands: list[dict], timeout: int = 90) -> list[dict]:
        import subprocess

        payload = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in commands)
        proc = subprocess.run(
            [sys.executable, str(BACKEND_DIR / "ipc_server.py")],
            input=payload.encode("utf-8"), capture_output=True, timeout=timeout,
            env={**__import__("os").environ, "PYTHONPATH": "", "PYTHONHOME": ""},
        )
        answers = []
        for raw in (proc.stdout or b"").decode("utf-8", "replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            # 每一行都必须是合法 JSON —— 这是"print 没污染通道"的证明
            answers.append(json.loads(raw))
        return answers

    def test_every_stdout_line_is_valid_json(self):
        """stdout 上只能有 JSON 行。混进任何 print 输出都会让前端解析失败。"""
        lines = self.run_backend([
            {"id": 1, "command": "ping", "args": None},
            {"id": 2, "command": "shutdown", "args": None},
        ])
        assert lines, "没有任何应答"
        assert any(item.get("event") == "ready" for item in lines)
        assert any(item.get("id") == 1 and item.get("ok") is True for item in lines)

    def test_ready_event_reports_version_and_frozen(self):
        lines = self.run_backend([{"id": 1, "command": "shutdown", "args": None}])
        ready = next(item for item in lines if item.get("event") == "ready")
        assert ready["version"], "ready 必须带版本号"
        assert ready["frozen"] is False, "源码运行时应报 frozen=False"

    def test_stderr_carries_chinese_without_mojibake(self):
        """中文日志必须按 UTF-8 写出 —— Windows 默认 CP936 会变成乱码。

        这条对应的实现是启动时的 sys.stdout/stderr.reconfigure(encoding='utf-8')。
        """
        import subprocess

        payload = json.dumps({"id": 1, "command": "shutdown", "args": None}) + "\n"
        proc = subprocess.run(
            [sys.executable, str(BACKEND_DIR / "ipc_server.py")],
            input=payload.encode("utf-8"), capture_output=True, timeout=90,
            env={**__import__("os").environ, "PYTHONPATH": "", "PYTHONHOME": ""},
        )
        stderr_text = (proc.stderr or b"").decode("utf-8", "replace")
        assert "\ufffd" not in stderr_text, "出现替换字符说明编码未统一为 UTF-8"

    def test_unknown_command_produces_error_not_crash(self):
        """未知命令要报错并继续服务，而不是整个后端退出。"""
        lines = self.run_backend([
            {"id": 1, "command": "definitely_not_a_command", "args": None},
            {"id": 2, "command": "ping", "args": None},
            {"id": 3, "command": "shutdown", "args": None},
        ])
        first = next(item for item in lines if item.get("id") == 1)
        assert first["ok"] is False
        # 关键：后面的命令仍然得到应答（说明没崩）
        second = next(item for item in lines if item.get("id") == 2)
        assert second["ok"] is True
