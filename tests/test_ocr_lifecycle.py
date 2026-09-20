"""OCR 独立生命周期与借用协议（工作单缺陷 #8）。

覆盖三件在源码里成立、但此前没有测试的缺陷：

1. **OCR 自己起的运行时没有独立 ``close()``**：``RapidOcrEngine`` 只管建、不管收，
   调用方（``frontend/backend/ipc_server.py:1089``）每次识别都新建一个引擎、
   从不释放。这里把"独立回收 + 幂等 + 关闭后使用明确报错"钉住。
2. **配置指纹没有覆盖所有影响结果的维度**：``voxsub/ocr.py`` 的 ``_translator_key``
   只列了 5 个新键，而 CloudTranslator 实际也读旧别名 ``api_key``/``base_url``/
   ``model``（``voxsub/translate/cloud.py:73-81``）—— 改了端点旧译文照样命中。
3. **借用的翻译器**：会话会替换并关闭当前翻译器（``voxsub/pipeline.py:707-724``），
   抓着裸引用就会用到已关闭的实例。这里验证借用协议：每帧现取、换代即失效、
   归还时**绝不**关闭借来的实例。

不加载 RapidOCR / ONNX 模型（重资源边界用 ``engine_factory`` 替身替换），
不弹窗、不抢焦点、不播声音；时序用 ``Event``/``Barrier`` 控制，不靠 sleep 碰运气。
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from voxsub.ocr import (
    RESULT_CONFIG_KEYS,
    OcrBox,
    OcrFrame,
    OcrLine,
    OcrRuntimeClosedError,
    OcrTranslationService,
    OcrUnavailableError,
    RapidOcrEngine,
    ocr_result_signature,
    translator_config_signature,
)

_IMAGE = np.zeros((8, 8, 3), dtype=np.uint8)


# --------------------------------------------------------------------- 替身


class _StubEngine:
    """RapidOCR 替身：不加载任何模型，只记录调用与释放。"""

    def __init__(
        self, *, raises: Exception | None = None,
        gate: threading.Event | None = None,
        started: threading.Event | None = None,
        providers: tuple[str, ...] = (),
    ) -> None:
        self.close_calls = 0
        self.inference_calls = 0
        self._raises = raises
        self._gate = gate
        self._started = started
        if providers:
            # 让 RapidOcrEngine._detect_engine_backend 认为这具引擎跑在 GPU 上。
            session = SimpleNamespace(get_providers=lambda: list(providers))
            self.text_det = SimpleNamespace(
                session=SimpleNamespace(session=session))

    def __call__(self, _image):
        self.inference_calls += 1
        if self._started is not None:
            self._started.set()
        if self._gate is not None and not self._gate.wait(5.0):
            raise AssertionError("测试没放行：__call__ 不该卡在这里")
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(
            boxes=[[[0, 0], [8, 0], [8, 8], [0, 8]]],
            txts=["hello"],
            scores=[0.99],
        )

    def close(self) -> None:
        self.close_calls += 1


def _engine_with(*, raises=None, gate=None, started=None, providers=()):
    """返回 (引擎, 记录每次构造的引擎列表)。"""
    built: list[_StubEngine] = []

    def build(params):
        assert "Global.text_score" in params, params
        engine = _StubEngine(
            raises=raises, gate=gate, started=started, providers=providers)
        built.append(engine)
        return engine

    return RapidOcrEngine(engine_factory=build), built


class _StubTranslator:
    """翻译器替身：记录逐行调用与关闭次数。"""

    def __init__(self, label: str = "stub") -> None:
        self.label = label
        self.calls: list[tuple[str, str, str]] = []
        self.close_calls = 0

    def warmup(self) -> None:
        self.warmed = True

    def translate(self, text, source_lang, target_lang, **_kwargs):
        self.calls.append((text, source_lang, target_lang))
        return f"[{self.label}] {text}"

    def close(self) -> None:
        self.close_calls += 1


@pytest.fixture()
def factory(monkeypatch):
    """替换 TranslatorFactory：测试里绝不加载真实翻译模型。"""
    created: list[_StubTranslator] = []

    def create(kind, config=None):
        translator = _StubTranslator(f"{kind}-{len(created)}")
        created.append(translator)
        return translator

    monkeypatch.setattr(
        "voxsub.ocr.TranslatorFactory",
        SimpleNamespace(create=staticmethod(create)),
    )
    return SimpleNamespace(create=create, created=created)


def _frame(text: str = "こんにちは世界", width: int = 120, height: int = 40) -> OcrFrame:
    return OcrFrame(
        width, height,
        (OcrLine(OcrBox(0, 0, width, 20), text, 0.9),),
    )


_CONFIG = {
    "translate_tier": "fast",
    "translate_model_id": "mt-opus-fast-builtin",
    "lang_pair": "ja-zh",
}


# ============================================================== 引擎生命周期


def test_engine_close_is_idempotent_and_releases_exactly_once():
    engine, built = _engine_with()
    assert engine.recognize(_IMAGE).lines, "前置条件：替身引擎能识别"

    engine.close()
    engine.close()
    engine.close()

    assert engine.closed is True
    assert built[0].close_calls == 1, "重复 close 不该重复释放原生会话"
    assert engine._engine is None, "关闭后不该还持有引擎引用"


def test_engine_use_after_close_reports_a_clear_error():
    engine, built = _engine_with()
    engine.recognize(_IMAGE)
    engine.close()

    with pytest.raises(OcrRuntimeClosedError) as error:
        engine.recognize(_IMAGE)

    assert "close" in str(error.value)
    assert len(built) == 1, "关闭后不许静默重建引擎继续用"


def test_engine_close_is_independent_of_any_session():
    """OCR 自己起的运行时自己就能收 —— 不需要会话，也不等会话。"""
    engine, built = _engine_with()
    engine.recognize(_IMAGE)

    # 没有会话、没有翻译器，直接回收。
    engine.close()
    assert engine.closed and built[0].close_calls == 1

    # 从未使用过就回收也是合法的（比如预热失败后的收尾）。
    unused, _ = _engine_with()
    unused.close()
    assert unused.closed


def test_engine_is_a_context_manager():
    built: list[_StubEngine] = []

    def build(_params):
        engine = _StubEngine()
        built.append(engine)
        return engine

    with RapidOcrEngine(engine_factory=build) as engine:
        engine.recognize(_IMAGE)
        assert engine.closed is False

    assert engine.closed is True
    assert built[0].close_calls == 1
    with pytest.raises(OcrRuntimeClosedError):
        engine.recognize(_IMAGE)


def test_engine_with_a_failed_initialization_is_still_closable():
    def explode(_params):
        raise RuntimeError("模型文件缺失")

    engine = RapidOcrEngine(engine_factory=explode)
    with pytest.raises(OcrUnavailableError):
        engine.recognize(_IMAGE)

    engine.close()          # 收尾路径不能因为"从没建成功"而抛错
    engine.close()
    with pytest.raises(OcrRuntimeClosedError):
        engine.recognize(_IMAGE)


def test_gpu_fallback_releases_the_failed_engine():
    """GPU 推理失败后旧引擎必须真的释放，而不是只把引用置空。"""
    built: list[_StubEngine] = []

    def build(_params):
        if not built:
            engine = _StubEngine(
                raises=RuntimeError("DirectML 掉线"),
                providers=("DmlExecutionProvider",),
            )
        else:
            engine = _StubEngine()          # 回退后的 CPU 引擎
        built.append(engine)
        return engine

    engine = RapidOcrEngine(engine_factory=build)

    result = engine.recognize(_IMAGE)

    assert result.lines, "回退到 CPU 后应当有结果"
    assert len(built) == 2
    assert built[0].close_calls == 1, "失败的引擎被漏掉了"
    assert built[1].close_calls == 0
    assert result.backend == "CPU"


def test_close_waits_for_in_flight_inference_instead_of_pulling_the_engine():
    """时序：识别还在用引擎时，close 不许把它抽走。

    用 Event 精确控制：识别进入 ``__call__`` 后先不放行，此时另一线程 close
    必须阻塞（同一把锁），放行后 close 才完成。
    """
    inference_started = threading.Event()
    finish_inference = threading.Event()
    close_returned = threading.Event()
    engine, built = _engine_with(
        gate=finish_inference, started=inference_started)

    failures: list[BaseException] = []

    def run_inference():
        try:
            engine.recognize(_IMAGE)
        except BaseException as exc:  # noqa: BLE001 - 测试要看到任何异常
            failures.append(exc)

    def run_close():
        try:
            engine.close()
        finally:
            close_returned.set()

    inference = threading.Thread(target=run_inference, name="ocr-inference")
    closer = threading.Thread(target=run_close, name="ocr-close")
    inference.start()
    assert inference_started.wait(5.0), "识别没有进入引擎"
    closer.start()

    assert not close_returned.wait(0.3), "close 不该在识别还在跑时就把引擎抽走"
    assert engine.closed is False

    finish_inference.set()
    inference.join(5.0)
    closer.join(5.0)

    assert close_returned.is_set()
    assert failures == []
    assert engine.closed is True
    assert built[0].close_calls == 1
    with pytest.raises(OcrRuntimeClosedError):
        engine.recognize(_IMAGE)


# ==================================================== 配置指纹：缓存键的维度


#: 缓存键必须覆盖的维度。少一个就会出现"设置保存了但译文没变"。
_REQUIRED_RESULT_KEYS = frozenset({
    "lang_pair",
    "translate_tier", "translate_model_id", "translate_api_key",
    "translate_base_url", "translate_model",
    # 0.3.x 旧别名：CloudTranslator 真的会读它们
    "api_key", "base_url", "model",
    "models_root",
    "ocr_model_id", "ocr_minimum_confidence", "ocr_maximum_lines",
    "ocr_maximum_characters", "ocr_group_paragraphs",
    "ocr_live_batch_items", "ocr_live_batch_characters",
})


def test_every_result_affecting_key_is_in_the_cache_dimension_table():
    missing = _REQUIRED_RESULT_KEYS - set(RESULT_CONFIG_KEYS)
    assert not missing, f"缓存键缺失这些影响结果的维度: {sorted(missing)}"


def test_changing_any_result_dimension_changes_the_signature():
    baseline = dict(_CONFIG)
    base_signature = ocr_result_signature(baseline, source_lang="ja", target_lang="zh")
    unchanged: list[str] = []
    for key in sorted(_REQUIRED_RESULT_KEYS):
        changed = dict(baseline, **{key: f"changed-{key}"})
        if ocr_result_signature(
                changed, source_lang="ja", target_lang="zh") == base_signature:
            unchanged.append(key)
    assert not unchanged, f"这些维度改了签名却没变: {unchanged}"


def test_signature_changes_with_the_language_pair():
    baseline = dict(_CONFIG)
    assert ocr_result_signature(
        baseline, source_lang="ja", target_lang="zh") != ocr_result_signature(
        baseline, source_lang="zh", target_lang="en")


def test_signature_never_contains_the_api_key_verbatim():
    secret = "sk-super-secret-value-12345"
    signature = ocr_result_signature(
        dict(_CONFIG, translate_api_key=secret, api_key=secret))
    assert secret not in signature
    assert len(signature) <= 16
    # 指纹必须仍然对密钥敏感（换了密钥就是另一套配置）
    assert signature != ocr_result_signature(
        dict(_CONFIG, translate_api_key="another-key", api_key="another-key"))


def test_translator_identity_signature_is_narrower_than_the_result_signature():
    """翻译器身份不含逐帧变化的 OCR 调优值，避免每帧重建。"""
    live = dict(_CONFIG, ocr_live_mode=True, ocr_minimum_confidence=0.54)
    refine = dict(
        _CONFIG, ocr_live_mode=True, ocr_refinement_mode=True,
        ocr_minimum_confidence=0.48, ocr_maximum_lines=72)

    assert translator_config_signature(live, source_lang="ja", target_lang="zh") == \
        translator_config_signature(refine, source_lang="ja", target_lang="zh")
    assert ocr_result_signature(live, source_lang="ja", target_lang="zh") != \
        ocr_result_signature(refine, source_lang="ja", target_lang="zh")


# ================================================== 译文缓存：改配置即失效


def test_translation_cache_hits_for_the_same_configuration(factory):
    service = OcrTranslationService()
    frame = _frame()

    first = service.translate_frame(frame, "ja", "zh", _CONFIG)
    second = service.translate_frame(frame, "ja", "zh", _CONFIG)

    assert first.translation_requests == 1
    assert second.translation_requests == 0, "同一套配置下应当命中缓存"
    assert second.translation_text == first.translation_text
    assert len(factory.created) == 1, "不该重建翻译器"


def test_changing_configuration_makes_old_translations_miss(factory):
    """**核心回归**：改配置后旧译文不再命中（此前会命中并显示旧配置的译文）。"""
    service = OcrTranslationService()
    frame = _frame()
    service.translate_frame(frame, "ja", "zh", _CONFIG)

    changed = dict(_CONFIG, translate_base_url="https://api.openai.com/v1")
    after = service.translate_frame(frame, "ja", "zh", changed)

    assert after.translation_requests == 1, "改了端点却命中了旧缓存"
    assert len(factory.created) == 2, "改了端点应当换一个翻译器"


def test_changing_only_a_legacy_endpoint_alias_makes_old_translations_miss(factory):
    """旧别名也必须算变化：CloudTranslator 读的就是 api_key/base_url/model。"""
    service = OcrTranslationService()
    frame = _frame()

    service.translate_frame(frame, "ja", "zh", dict(_CONFIG, api_key="key-one"))
    after = service.translate_frame(frame, "ja", "zh", dict(_CONFIG, api_key="key-two"))

    assert after.translation_requests == 1, "换了凭据却命中了旧缓存"
    assert len(factory.created) == 2


def test_changing_the_language_pair_makes_old_translations_miss(factory):
    service = OcrTranslationService()
    frame = _frame("こんにちは")   # 日文原文，源语言 ja 才匹配

    service.translate_frame(frame, "ja", "zh", _CONFIG)
    after = service.translate_frame(frame, "ja", "en", _CONFIG)

    assert after.translation_requests == 1, "换了目标语言却命中了旧译文"
    assert len(factory.created) == 2, "语言对变了应当换翻译器"


def test_configuration_change_also_reuses_the_existing_invalidation(factory):
    """原有失效机制保留：翻译器身份变化时清缓存 + 关旧实例。"""
    service = OcrTranslationService()
    frame = _frame()
    service.translate_frame(frame, "ja", "zh", _CONFIG)

    service.translate_frame(
        frame, "ja", "zh", dict(_CONFIG, translate_tier="quality"))

    assert factory.created[0].close_calls == 1, "旧翻译器应当被关掉"


# ==================================================== 服务独立回收（幂等）


def test_service_close_is_idempotent_and_terminal(factory):
    service = OcrTranslationService()
    service.translate_frame(_frame(), "ja", "zh", _CONFIG)

    service.close()
    service.close()
    service.close()

    assert service.closed is True
    assert factory.created[0].close_calls == 1, "重复 close 不该重复关闭"

    with pytest.raises(OcrRuntimeClosedError):
        service.translate_frame(_frame(), "ja", "zh", _CONFIG)
    with pytest.raises(OcrRuntimeClosedError):
        service.warmup(_CONFIG)
    with pytest.raises(OcrRuntimeClosedError):
        service.attach_translator_provider(lambda: None)


def test_service_closes_without_ever_having_a_session(factory):
    """不依赖会话：从没借用过、也没翻译过，也能干净回收。"""
    service = OcrTranslationService()
    service.close()
    assert service.closed is True


# ============================================================ 借用/归还协议


def test_borrowed_translator_is_used_but_never_closed(factory):
    borrowed = _StubTranslator("session")
    holder = {"translator": borrowed, "generation": 1}
    service = OcrTranslationService()
    service.attach_translator_provider(
        lambda: (holder["translator"], holder["generation"]))

    result = service.translate_frame(_frame(), "ja", "zh", _CONFIG)

    assert result.translation_requests == 1
    assert borrowed.calls, "应当用会话的翻译器"
    assert not factory.created, "借到实例时不该再自建一个"

    service.detach_translator_provider()
    service.close()

    assert borrowed.close_calls == 0, "OCR 不许关闭借来的会话翻译器"


def test_replaced_session_translator_is_picked_up_and_old_results_invalidate(factory):
    """会话换档会关掉旧实例并换成新实例（pipeline.py:707-724）。"""
    old = _StubTranslator("old")
    new = _StubTranslator("new")
    holder = {"translator": old, "generation": 1}
    service = OcrTranslationService()
    service.attach_translator_provider(
        lambda: (holder["translator"], holder["generation"]))
    frame = _frame()

    first = service.translate_frame(frame, "ja", "zh", _CONFIG)
    assert first.translation_requests == 1 and old.calls

    # 会话侧：关掉旧实例、换上新实例（世代号 +1）。
    old.close()
    holder["translator"], holder["generation"] = new, 2

    second = service.translate_frame(frame, "ja", "zh", _CONFIG)

    assert new.calls, "换代之后必须用新实例"
    assert second.translation_requests == 1, "旧世代的译文不该被复用"
    assert old.close_calls == 1, "只有拥有者关它，OCR 不插手"
    assert new.close_calls == 0


def test_borrowed_translator_may_be_a_bare_reference(factory):
    """provider 允许返回裸对象；同一对象照样命中缓存。"""
    borrowed = _StubTranslator("session")
    service = OcrTranslationService()
    service.attach_translator_provider(lambda: borrowed)
    frame = _frame()

    service.translate_frame(frame, "ja", "zh", _CONFIG)
    second = service.translate_frame(frame, "ja", "zh", _CONFIG)

    assert second.translation_requests == 0
    assert not factory.created


def test_provider_returning_nothing_falls_back_to_the_config_translator(factory):
    """会话一停，provider 返回 None：OCR 按配置自建，功能不掉。"""
    session = _StubTranslator("session")
    holder = {"translator": session}
    service = OcrTranslationService()
    service.attach_translator_provider(lambda: holder["translator"])

    service.translate_frame(_frame(), "ja", "zh", _CONFIG)
    holder["translator"] = None            # 会话停了

    after = service.translate_frame(_frame(), "ja", "zh", _CONFIG)

    assert after.translation_requests == 1
    assert factory.created, "借不到时应当按配置自建"
    assert session.close_calls == 0
    service.close()
    assert factory.created[0].close_calls == 1


def test_provider_that_raises_does_not_break_translation(factory):
    """拥有者正在拆时 provider 可能抛错：降级到自有翻译器，不能把 OCR 打断。"""
    service = OcrTranslationService()

    def broken():
        raise RuntimeError("会话正在关闭")

    service.attach_translator_provider(broken)
    result = service.translate_frame(_frame(), "ja", "zh", _CONFIG)

    assert result.translation_requests == 1
    assert factory.created


# ================================================== 并发/时序（Barrier 控制）


def test_concurrent_translation_and_close_leave_no_broken_state(factory):
    """三路同时翻译 + 一路同时回收：只允许两种结果 —— 完整结果或明确报错。"""
    service = OcrTranslationService()
    barrier = threading.Barrier(4)
    outcomes: list[str] = []
    unexpected: list[BaseException] = []
    frame = _frame()

    def translate():
        barrier.wait(5.0)
        try:
            service.translate_frame(frame, "ja", "zh", _CONFIG)
            outcomes.append("translated")
        except OcrRuntimeClosedError:
            outcomes.append("closed-error")
        except BaseException as exc:  # noqa: BLE001 - 意外异常必须暴露
            unexpected.append(exc)

    def recycle():
        barrier.wait(5.0)
        service.close()
        outcomes.append("closed")

    threads = [
        threading.Thread(target=translate, name=f"translate-{index}")
        for index in range(3)
    ] + [threading.Thread(target=recycle, name="recycle")]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10.0)

    assert not any(thread.is_alive() for thread in threads), "有线程卡住了"
    assert unexpected == [], f"出现了不该有的异常: {unexpected!r}"
    assert len(outcomes) == 4
    assert service.closed is True
    for translator in factory.created:
        assert translator.close_calls == 1, "自建翻译器必须恰好被关一次"


def test_concurrent_cache_reads_are_consistent(factory):
    """并发读同一份缓存：结果一致，且不会重复请求（LRU 有锁）。"""
    service = OcrTranslationService()
    barrier = threading.Barrier(4)
    results: list[str] = []
    unexpected: list[BaseException] = []
    frame = _frame()
    service.translate_frame(frame, "ja", "zh", _CONFIG)

    def reader():
        barrier.wait(5.0)
        try:
            results.append(
                service.translate_frame(frame, "ja", "zh", _CONFIG).translation_text)
        except BaseException as exc:  # noqa: BLE001
            unexpected.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10.0)

    assert unexpected == []
    assert len(set(results)) == 1, results
    assert len(factory.created) == 1
    assert factory.created[0].close_calls == 0
