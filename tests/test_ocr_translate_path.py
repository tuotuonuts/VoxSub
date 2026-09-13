"""OCR 独立工作区的翻译路径测试。

## 覆盖的缺陷

``_cmd_ocr_translate`` 里写的是 ``pipeline.translator[1]`` —— 这个属性
**不存在**（Pipeline 内部叫 ``_translator``）。只要 pipeline 建好过，
OCR 翻译就必然抛：

    AttributeError: 'Pipeline' object has no attribute 'translator'

也就是说 OCR 页的"翻译"整个功能不可用。旁边明明已经写好了一个
``BackendService._translator()`` 辅助方法（依次尝试"运行中会话的翻译器 →
OCR 自己的缓存 → 按配置现建"），但它从来没被调用。

顺带守住的还有两件事：
  · Pipeline 提供公开只读属性 ``translator``，调用方不必读私有名；
  · OCR 翻译按**配置里的档位**建翻译器（此前读的配置键
    ``translator_kind`` 根本不存在，永远落到快档）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "frontend" / "backend"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND_DIR))

import ipc_server  # noqa: E402


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("VOXSUB_ROOT", str(ROOT))
    return tmp_path


class _StubTranslator:
    """最小替身：记录收到的语言对，返回固定译文。"""

    langs = ("zh", "en", "ja", "ko")

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def translate(self, text, src_lang, dst_lang, **_kwargs):
        self.calls.append((text, src_lang, dst_lang))
        return f"[{src_lang}->{dst_lang}] {text}"

    def supports(self, src, dst):
        return True

    def close(self):
        pass


class TestOcrTranslateWithPipeline:
    """有 pipeline 时（这是真实情况：任何需要 pipeline 的命令都会先建它）。"""

    def test_does_not_raise_when_pipeline_exists(self, isolated_config):
        """回归：此前这里抛 AttributeError，功能完全不可用。"""
        service = ipc_server.BackendService()
        pipeline = service.ensure_pipeline()
        stub = _StubTranslator()
        pipeline._translator = stub  # noqa: SLF001

        result = service._cmd_ocr_translate(  # noqa: SLF001
            pipeline, {"text": "会議は三時から始まります。", "source": "ja", "target": "zh"})

        assert result["translation"], result
        assert stub.calls[-1][1:] == ("ja", "zh"), stub.calls

    def test_uses_running_session_translator(self, isolated_config):
        """会话在跑时应当复用它的翻译器，而不是另建一个。"""
        service = ipc_server.BackendService()
        pipeline = service.ensure_pipeline()
        stub = _StubTranslator()
        pipeline._translator = stub  # noqa: SLF001

        service._cmd_ocr_translate(  # noqa: SLF001
            pipeline, {"text": "hello", "source": "en", "target": "zh"})

        assert len(stub.calls) == 1, "应当只调用同一个翻译器一次"

    def test_empty_text_returns_empty_without_translating(self, isolated_config):
        service = ipc_server.BackendService()
        pipeline = service.ensure_pipeline()
        stub = _StubTranslator()
        pipeline._translator = stub  # noqa: SLF001

        result = service._cmd_ocr_translate(pipeline, {"text": "", "source": "ja", "target": "zh"})  # noqa: SLF001

        assert result == {"translation": ""}
        assert stub.calls == [], "空文本不该调用翻译器"


class TestOcrTranslatorFallback:
    """pipeline 还没建翻译器时（用户没点开始就框选屏幕）。"""

    def test_falls_back_to_config_and_caches(self, isolated_config, monkeypatch):
        """按配置现建一个，并缓存起来复用（不每次重建）。"""
        import voxsub.pipeline as pipeline_module

        created: list[str] = []

        class _Stub2(_StubTranslator):
            def __init__(self):
                super().__init__()
                created.append("x")

        monkeypatch.setattr(pipeline_module, "_load_translator",
                            lambda kind, config=None: (_Stub2(), kind))

        service = ipc_server.BackendService()
        pipeline = service.ensure_pipeline()
        assert pipeline.translator is None, "前置条件：此时还没有翻译器"

        first = service._translator()  # noqa: SLF001
        second = service._translator()  # noqa: SLF001

        assert first is not None
        assert first is second, "第二次应复用缓存，而不是重建"
        assert len(created) == 1, f"只应创建一次，实际 {len(created)} 次"

    def test_uses_configured_tier_not_hardcoded_fast(self, isolated_config, monkeypatch):
        """必须按配置的档位建 —— 此前读的配置键不存在，永远落到快档。"""
        import voxsub.pipeline as pipeline_module
        from voxsub.config_store import ConfigStore

        ConfigStore().update({"translate_tier": "quality",
                              "translate_model_id": "mt-hy-mt2-1.8b-q8"})
        kinds: list[str] = []

        class _Stub3(_StubTranslator):
            pass

        monkeypatch.setattr(pipeline_module, "_load_translator",
                            lambda kind, config=None: (kinds.append(kind), (_Stub3(), kind))[1])

        service = ipc_server.BackendService()
        service.ensure_pipeline()
        service._translator()  # noqa: SLF001

        assert kinds, "没有调用翻译器加载"
        assert kinds[0] == "qwen-quality", (
            f"配置是 quality，实际加载 {kinds[0]!r}（应经 kind_for_tier 映射）")

    def test_failure_degrades_instead_of_raising(self, isolated_config, monkeypatch):
        """翻译器建不出来时返回 None（只返回识别结果），不能把 OCR 整个打断。"""
        import voxsub.pipeline as pipeline_module

        def boom(kind, config=None):
            raise RuntimeError("模型缺失")

        monkeypatch.setattr(pipeline_module, "_load_translator", boom)

        service = ipc_server.BackendService()
        service.ensure_pipeline()

        assert service._translator() is None  # noqa: SLF001


class TestPipelineTranslatorAccessor:
    """Pipeline 的公开只读属性（调用方不该读私有名）。"""

    def test_property_exists_and_is_read_only(self):
        from voxsub.pipeline import Pipeline

        pipeline = Pipeline()
        assert pipeline.translator is None

        stub = _StubTranslator()
        pipeline._translator = stub  # noqa: SLF001
        assert pipeline.translator is stub

        with pytest.raises(AttributeError):
            pipeline.translator = stub  # type: ignore[misc]

    def test_reset_clears_accessor(self):
        """换档位/切模型目录会重置内部字段，访问器要如实反映。"""
        from voxsub.pipeline import Pipeline

        pipeline = Pipeline()
        pipeline._translator = _StubTranslator()  # noqa: SLF001
        pipeline.set_translator("cloud", {})
        assert pipeline.translator is None
