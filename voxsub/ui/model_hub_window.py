"""Soft Premium model marketplace page."""
from __future__ import annotations

import threading

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from voxsub.logging_setup import get_logger
from voxsub.model_storage import resolve_models_root
from voxsub.model_catalog import (
    CATALOG_UPDATED,
    HardwareProfile,
    ModelMarketplace,
    ModelSpec,
    assess_model,
    detect_hardware,
    format_bytes,
    models_for_task,
)
from voxsub.models import DownloadCancelled
from voxsub.npu_validation import npu_compatibility
from voxsub.config_store import ConfigStore
from voxsub.ui.i18n import (
    language_manager,
    retranslate_widget_tree,
    tr,
    translate_dynamic,
)
from voxsub.ui.selection_controls import PillChoiceButton

logger = get_logger("ui.model_hub")


class ModelDownloadWorker(QThread):
    progress = Signal(int, int, str)
    installed = Signal(str)
    failed = Signal(str, str)

    def __init__(self, marketplace: ModelMarketplace, model: ModelSpec,
                 preference: str, parent=None) -> None:
        super().__init__(parent)
        self.marketplace = marketplace
        self.model = model
        self.preference = preference
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        try:
            self.marketplace.install(
                self.model,
                self.preference,
                progress=lambda done, total, source: self.progress.emit(done, total, source),
                cancelled=self._cancelled.is_set,
            )
        except DownloadCancelled:
            self.failed.emit(self.model.id, "下载已取消，断点已保留，下次可继续")
        except Exception as exc:
            logger.exception("模型安装失败: id=%s", self.model.id)
            self.failed.emit(self.model.id, str(exc))
        else:
            self.installed.emit(self.model.id)


class RecommendationBadge(QLabel):
    def __init__(self, text: str, color: str, parent=None) -> None:
        super().__init__(text, parent)
        self.setObjectName("recommendationBadge")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        value = color.lstrip("#")
        red, green, blue = (int(value[index:index + 2], 16) for index in (0, 2, 4))
        self.setStyleSheet(
            f"QLabel#recommendationBadge {{ color: {color}; "
            f"background-color: rgba({red},{green},{blue},0.13); "
            f"border: 1px solid rgba({red},{green},{blue},0.42); "
            "border-radius: 10px; padding: 4px 10px; font-weight: 600; }}"
        )


class NpuCompatibilityBadge(QLabel):
    def __init__(self, model_id: str, parent=None) -> None:
        super().__init__(parent)
        self.model_id = model_id
        self.setObjectName("npuCompatibilityBadge")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.refresh_language()

    def refresh_language(self) -> None:
        evidence = npu_compatibility(self.model_id)
        self.setText(tr(evidence.label_zh, evidence.label_en))
        details = [tr(evidence.reason_zh, evidence.reason_en)]
        if evidence.device:
            details.append(f"{tr('验证设备', 'Validated device')}: {evidence.device}")
        if evidence.driver:
            details.append(f"{tr('驱动', 'Driver')}: {evidence.driver}")
        if evidence.runtime:
            details.append(f"{tr('运行时', 'Runtime')}: {evidence.runtime}")
        if evidence.validated_at:
            details.append(f"{tr('验证日期', 'Validated')}: {evidence.validated_at}")
        self.setToolTip("\n".join(details))
        value = evidence.color.lstrip("#")
        red, green, blue = (int(value[index:index + 2], 16) for index in (0, 2, 4))
        self.setStyleSheet(
            f"QLabel#npuCompatibilityBadge {{ color: {evidence.color}; "
            f"background-color: rgba({red},{green},{blue},0.12); "
            f"border: 1px solid rgba({red},{green},{blue},0.38); "
            "border-radius: 9px; padding: 3px 8px; font-size: 12px; "
            "font-weight: 600; }}"
        )


class ModelCard(QFrame):
    action_requested = Signal(str)
    uninstall_requested = Signal(str)

    def __init__(self, model: ModelSpec, profile: HardwareProfile,
                 marketplace: ModelMarketplace, parent=None) -> None:
        super().__init__(parent)
        self.model = model
        self.profile = profile
        self.marketplace = marketplace
        self.setObjectName("modelCard")
        self.setProperty("topRank", model.quality_score >= 98)
        self.setMinimumHeight(226)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)

        head = QHBoxLayout()
        copy = QVBoxLayout()
        copy.setSpacing(3)
        eyebrow = QLabel(f"{tr(model.task_label).upper()}  ·  {model.vendor}", self)
        eyebrow.setObjectName("eyebrowLabel")
        name = QLabel(model.name, self)
        name.setObjectName("modelName")
        copy.addWidget(eyebrow)
        copy.addWidget(name)
        head.addLayout(copy, 1)
        assessment = assess_model(model, profile)
        self.badge = RecommendationBadge(tr(assessment.level), assessment.color, self)
        self.badge.setToolTip(translate_dynamic(assessment.reason))
        head.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)

        description = QLabel(tr(model.description), self)
        description.setObjectName("secondaryLabel")
        description.setWordWrap(True)
        root.addWidget(description)

        accelerators = ["CPU"]
        if model.igpu_supported:
            accelerators.insert(0, tr("核显"))
        if model.npu_supported:
            accelerators.insert(0, "Intel NPU" if model.runtime == "llama-hy-mt2" else "NPU")
        if model.gpu_supported:
            accelerators.insert(0, tr("独显"))
        facts = QLabel(
            f"{tr('质量分')} {model.quality_score}  ·  {tr(model.languages)}  ·  "
            f"{model.license}  ·  {model.release}  ·  {tr('运行设备')} {' / '.join(accelerators)}", self)
        facts.setObjectName("modelFacts")
        facts.setWordWrap(True)
        root.addWidget(facts)

        tags = QHBoxLayout()
        tags.setSpacing(7)
        self.npu_badge = NpuCompatibilityBadge(model.id, self)
        tags.addWidget(self.npu_badge)
        for text in model.tags:
            tag = QLabel(tr(text), self)
            tag.setObjectName("modelTag")
            tags.addWidget(tag)
        tags.addStretch(1)
        root.addLayout(tags)

        foot = QHBoxLayout()
        foot.setSpacing(10)
        self.resource = QLabel(self._resource_text(assessment), self)
        self.resource.setObjectName("secondaryLabel")
        self.resource.setWordWrap(True)
        foot.addWidget(self.resource, 1)
        self.uninstall_btn = QPushButton("卸载", self)
        self.uninstall_btn.setObjectName("ghostButton")
        self.uninstall_btn.setMinimumWidth(72)
        self.uninstall_btn.clicked.connect(lambda: self.uninstall_requested.emit(model.id))
        self.action_btn = QPushButton("下载", self)
        self.action_btn.setObjectName("modelActionButton")
        self.action_btn.setMinimumWidth(112)
        self.action_btn.clicked.connect(lambda: self.action_requested.emit(model.id))
        foot.addWidget(self.uninstall_btn)
        foot.addWidget(self.action_btn)
        root.addLayout(foot)

        self.progress = QProgressBar(self)
        self.progress.setObjectName("modelProgress")
        self.progress.setRange(0, 1000)
        # The detailed line below already shows percentage and byte counts.
        # Keep the 7 px bar purely visual so Qt does not clip duplicate text.
        self.progress.setTextVisible(False)
        self.progress.hide()
        root.addWidget(self.progress)
        self.source_text = QLabel("", self)
        self.source_text.setObjectName("downloadStatus")
        self.source_text.hide()
        root.addWidget(self.source_text)
        self.refresh()
        language_manager.language_changed.connect(self._on_language_changed)
        self._on_language_changed(language_manager.language)

    def _resource_text(self, assessment) -> str:
        if self.model.builtin:
            missing = self.marketplace.missing_paths(self.model)
            if missing:
                return f"{tr('缺少文件')}: {', '.join(missing)}"
            return f"{translate_dynamic(assessment.reason)}  ·  {tr('随应用内置')}"
        return f"{translate_dynamic(assessment.reason)}  ·  {format_bytes(self.model.download_bytes)}"

    def refresh(self, active: bool = False, selected: bool = False) -> None:
        installed = self.marketplace.is_installed(self.model)
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)
        self.action_btn.setEnabled(not active)
        self.uninstall_btn.setVisible(installed and not self.model.builtin)
        self.uninstall_btn.setEnabled(not active and not selected)
        self.resource.setText(self._resource_text(assess_model(self.model, self.profile)))
        if active:
            self.action_btn.setText(tr("取消"))
            self.action_btn.setEnabled(True)
        elif installed and selected:
            self.action_btn.setText(tr("使用中"))
            self.action_btn.setEnabled(False)
        elif installed:
            self.action_btn.setText(tr("设为使用"))
        elif self.model.builtin:
            self.action_btn.setText(tr("修复安装"))
        else:
            self.action_btn.setText(tr("下载"))
        if not active:
            self.progress.hide()
            self.source_text.hide()

    def set_progress(self, done: int, total: int, source: str) -> None:
        self.progress.show()
        self.source_text.show()
        if total > 0:
            value = min(1000, int(done / total * 1000))
            self.progress.setRange(0, 1000)
            self.progress.setValue(value)
            percent = value / 10
            self.source_text.setText(
                f"{source}  ·  {percent:.1f}%  ·  {format_bytes(done)} / {format_bytes(total)}")
        else:
            self.progress.setRange(0, 0)
            self.source_text.setText(f"{source}  ·  {tr('已下载')} {format_bytes(done)}")

    def _on_language_changed(self, _language: str) -> None:
        retranslate_widget_tree(self)
        self.npu_badge.refresh_language()
        self.refresh()


class ModelHubWindow(QWidget):
    """Curated, hardware-aware marketplace used as an embedded tertiary page."""

    selection_changed = Signal(str, str)

    def __init__(self, store: ConfigStore | None = None,
                 marketplace: ModelMarketplace | None = None,
                 profile: HardwareProfile | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("modelHubWindow")
        self.setWindowTitle("模型广场 · 语幕 VoxSub")
        self.resize(1040, 780)
        self.setMinimumSize(840, 620)
        self._store = store or ConfigStore()
        self.marketplace = marketplace or ModelMarketplace()
        self.profile = profile or detect_hardware()
        self._filter = "all"
        self._cards: dict[str, ModelCard] = {}
        self._workers: dict[str, ModelDownloadWorker] = {}
        self._embedded = False
        self._build_ui()
        self.refresh()
        language_manager.language_changed.connect(self._on_language_changed)
        self._on_language_changed(language_manager.language)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 26)
        root.setSpacing(16)

        head = QHBoxLayout()
        title_copy = QVBoxLayout()
        eyebrow = QLabel("VOXSUB  /  CURATED LOCAL MODELS", self)
        eyebrow.setObjectName("eyebrowLabel")
        title = QLabel("模型广场", self)
        title.setObjectName("hubTitle")
        self.catalog_summary = QLabel(self._catalog_summary_text(), self)
        self.catalog_summary.setObjectName("secondaryLabel")
        self.catalog_summary.setWordWrap(True)
        title_copy.addWidget(eyebrow)
        title_copy.addWidget(title)
        title_copy.addWidget(self.catalog_summary)
        head.addLayout(title_copy, 1)
        source_box = QVBoxLayout()
        source_label = QLabel("下载源", self)
        source_label.setObjectName("fieldLabel")
        self.source_combo = QComboBox(self)
        self.source_combo.setObjectName("inputBox")
        self.source_combo.setMinimumSize(184, 44)
        self.source_combo.addItem("自动测速切换", "auto")
        self.source_combo.addItem("全球源优先", "global")
        self.source_combo.addItem("中国大陆源优先", "china")
        current = str(self._store.get("download_source", "auto"))
        index = self.source_combo.findData(current)
        self.source_combo.setCurrentIndex(max(0, index))
        self.source_combo.currentIndexChanged.connect(
            lambda _i: self._store.set("download_source", self.source_combo.currentData()))
        source_box.addWidget(source_label)
        source_box.addWidget(self.source_combo)
        head.addLayout(source_box)
        root.addLayout(head)

        hero = QFrame(self)
        hero.setObjectName("hardwareHero")
        hero.setProperty("emphasis", True)
        hero_box = QHBoxLayout(hero)
        hero_box.setContentsMargins(22, 18, 22, 18)
        hero_box.setSpacing(18)
        hardware_copy = QVBoxLayout()
        hardware_title = QLabel("根据这台电脑实时评估", hero)
        hardware_title.setObjectName("sectionTitle")
        self.hardware_detail = QLabel(self._hardware_detail_text(), hero)
        self.hardware_detail.setObjectName("secondaryLabel")
        self.hardware_detail.setWordWrap(True)
        hardware_copy.addWidget(hardware_title)
        hardware_copy.addWidget(self.hardware_detail)
        hero_box.addLayout(hardware_copy, 1)
        legend = QVBoxLayout()
        legend.setSpacing(5)
        from voxsub.model_catalog import RECOMMENDATION_COLORS
        legend_row = QHBoxLayout()
        for level in ("不推荐", "较为推荐", "推荐", "满载"):
            legend_row.addWidget(RecommendationBadge(tr(level), RECOMMENDATION_COLORS[level], hero))
        legend.addLayout(legend_row)
        legend_note = QLabel("徽章同时考虑内存、计算负载与是否存在更合适的高质量模型", hero)
        legend_note.setObjectName("secondaryLabel")
        legend.addWidget(legend_note)
        hero_box.addLayout(legend)
        root.addWidget(hero)

        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.filter_buttons: dict[str, QPushButton] = {}
        for key, text in (("all", "全部"), ("asr", "语音识别"),
                          ("translate", "字幕翻译"), ("tts", "语音朗读"),
                          ("ocr", "OCR 识别")):
            button = PillChoiceButton(text, self)
            button.setObjectName("filterPill")
            button.clicked.connect(lambda _checked, k=key: self.set_filter(k))
            self.filter_buttons[key] = button
            filters.addWidget(button)
        filters.addStretch(1)
        order = QLabel("排序：模型质量 ↓", self)
        order.setObjectName("statusPill")
        filters.addWidget(order)
        root.addLayout(filters)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("modelScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.container = QWidget(self.scroll)
        self.cards_box = QVBoxLayout(self.container)
        self.cards_box.setContentsMargins(2, 2, 10, 2)
        self.cards_box.setSpacing(12)
        self.cards_box.addStretch(1)
        self.scroll.setWidget(self.container)
        root.addWidget(self.scroll, 1)
        self.set_filter("all")

    def _catalog_summary_text(self) -> str:
        """Build the catalog summary from the active product language."""
        return (
            f"{len(models_for_task())} {tr('个可下载或内置模型', 'downloadable or bundled models')} · "
            f"{tr('仅展示已接通运行时的非淘汰模型', 'only supported, current models are shown')} · "
            f"{tr('独显→NPU→核显→CPU · 按质量排序 · 目录更新于', 'discrete GPU -> NPU -> integrated GPU -> CPU · sorted by quality · catalog updated')} {CATALOG_UPDATED}"
        )

    def _hardware_detail_text(self) -> str:
        """Build hardware facts anew because they contain user-specific values."""
        gpu = (
            f"{self.profile.gpu_name} · {self.profile.vram_gb:.1f} GB {tr('显存', 'VRAM')}"
            if self.profile.gpu_name
            else (
                f"{self.profile.gpu_provider} {tr('可用', 'available')}"
                if self.profile.gpu_provider else tr("未检测到独立显卡")
            )
        )
        npu = (
            f"{self.profile.npu_name} · {self.profile.npu_provider or tr('已检测，等待兼容模型后端')}"
            if self.profile.npu_name else tr("未检测到 NPU")
        )
        igpu = (
            f"{self.profile.integrated_gpu_name} · "
            f"{self.profile.integrated_gpu_provider or tr('已检测')}"
            if self.profile.integrated_gpu_name else tr("未检测到核显")
        )
        return (
            f"{self.profile.cpu_name} · {self.profile.physical_cores} {tr('核', 'cores')} / "
            f"{self.profile.logical_cores} {tr('线程', 'threads')} · {self.profile.ram_gb:.1f} GB {tr('内存', 'RAM')}\n"
            f"{tr('独显', 'Discrete GPU')}：{gpu}  ·  NPU：{npu}  · "
            f"{tr('核显', 'Integrated GPU')}：{igpu}"
        )

    def set_filter(self, task: str) -> None:
        self._filter = task if task in {"all", "asr", "translate", "tts", "ocr"} else "all"
        for key, button in self.filter_buttons.items():
            button.setChecked(key == self._filter)
        self._rebuild_cards()

    def _rebuild_cards(self) -> None:
        for card in self._cards.values():
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        for model in models_for_task(self._filter):
            card = ModelCard(model, self.profile, self.marketplace, self.container)
            card.action_requested.connect(self._on_action)
            card.uninstall_requested.connect(self._on_uninstall)
            self.cards_box.insertWidget(self.cards_box.count() - 1, card)
            self._cards[model.id] = card
        self.refresh()

    def _selected_id(self, task: str) -> str:
        key = {
            "asr": "asr_model_id",
            "translate": "translate_model_id",
            "tts": "tts_model_id_zh",
            "ocr": "ocr_model_id",
        }.get(task, "translate_model_id")
        fallback = {
            "asr": "asr-zipformer-bilingual-fast",
            "translate": "mt-opus-fast-builtin",
            "tts": "tts-icefall-zh-aishell3",
            "ocr": "ocr-rapidocr-v6-small-builtin",
        }.get(task, "mt-opus-fast-builtin")
        return str(self._store.get(key, fallback))

    def _is_selected(self, model: ModelSpec) -> bool:
        if model.task != "tts":
            return self._selected_id(model.task) == model.id
        return any(
            str(self._store.get(f"tts_model_id_{lang}", "")) == model.id
            for lang in model.tts_languages
        )

    @staticmethod
    def _selection_updates(model: ModelSpec) -> dict[str, str]:
        if model.task == "asr":
            return {"asr_model_id": model.id}
        if model.task == "translate":
            return {
                "translate_model_id": model.id,
                "translate_tier": (
                    "fast" if model.id == "mt-opus-fast-builtin" else "quality"),
            }
        if model.task == "tts":
            return {
                f"tts_model_id_{lang}": model.id
                for lang in model.tts_languages
            }
        if model.task == "ocr":
            return {"ocr_model_id": model.id}
        return {}

    def refresh(self) -> None:
        for model_id, card in self._cards.items():
            card.refresh(active=model_id in self._workers,
                         selected=self._is_selected(card.model))

    def _on_action(self, model_id: str) -> None:
        card = self._cards.get(model_id)
        if card is None:
            return
        worker = self._workers.get(model_id)
        if worker is not None:
            worker.cancel()
            card.action_btn.setEnabled(False)
            card.action_btn.setText(tr("正在取消…"))
            return
        model = card.model
        if self.marketplace.is_installed(model):
            updates = self._selection_updates(model)
            self._store.update(updates)
            self.selection_changed.emit(model.task, model.id)
            self.refresh()
            return
        self._start_download(model)

    def _start_download(self, model: ModelSpec) -> None:
        preference = str(self.source_combo.currentData() or "auto")
        worker = ModelDownloadWorker(self.marketplace, model, preference, self)
        worker.progress.connect(
            lambda done, total, source, mid=model.id: self._on_progress(
                mid, done, total, source))
        worker.installed.connect(self._on_installed)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(lambda mid=model.id: self._worker_finished(mid))
        self._workers[model.id] = worker
        self.refresh()
        worker.start()

    def _on_progress(self, model_id: str, done: int, total: int, source: str) -> None:
        card = self._cards.get(model_id)
        if card is not None:
            card.set_progress(done, total, source)

    def _on_installed(self, model_id: str) -> None:
        model = next((m for m in models_for_task() if m.id == model_id), None)
        if model is None:
            return
        updates = self._selection_updates(model)
        self._store.update(updates)
        self.selection_changed.emit(model.task, model.id)

    def _on_failed(self, model_id: str, message: str) -> None:
        if "已取消" not in message:
            QMessageBox.warning(self, tr("模型下载未完成"), message)
        else:
            logger.info("%s: %s", model_id, message)

    def _worker_finished(self, model_id: str) -> None:
        worker = self._workers.pop(model_id, None)
        if worker is not None:
            worker.deleteLater()
        self.refresh()

    def _on_uninstall(self, model_id: str) -> None:
        card = self._cards.get(model_id)
        if card is None:
            return
        model = card.model
        selected = self._is_selected(model)
        if selected:
            QMessageBox.information(
                self, tr("模型正在使用"), tr("请先切换到同类的其他模型再卸载。"))
            return
        answer = QMessageBox.question(
            self, tr("卸载模型"),
            f"{tr('确定卸载')} {model.name}?\n{tr('模型文件将从本机删除。')}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.marketplace.uninstall(model, in_use=False)
        except Exception as exc:
            QMessageBox.warning(self, tr("无法卸载"), str(exc))
        self.refresh()

    def closeEvent(self, event) -> None:  # noqa: N802
        # Closing the window does not abort a multi-GB download unexpectedly;
        # the worker remains owned by this hidden top-level window.
        event.accept()

    def set_embedded(self, embedded: bool = True) -> None:
        """Allow the marketplace to live inside MainWindow's page shell."""
        self._embedded = embedded
        if embedded:
            self.setMinimumSize(0, 0)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        else:
            self.setMinimumSize(840, 620)
            self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    def shutdown(self) -> None:
        """Cancel active downloads and wait briefly for clean thread teardown."""
        workers = list(self._workers.values())
        for worker in workers:
            worker.cancel()
        for worker in workers:
            worker.wait(5000)

    def has_active_downloads(self) -> bool:
        """Whether a storage migration must wait for a download to finish."""
        return bool(self._workers)

    def reload_model_storage(self) -> bool:
        """Point cards and future downloads at the newly selected model root."""
        if self.has_active_downloads():
            logger.warning("模型目录已变化，但仍有下载任务，暂不重载模型广场")
            return False
        self.marketplace = ModelMarketplace(resolve_models_root(self._store))
        self._rebuild_cards()
        logger.info("模型广场已重载模型目录: %s", self.marketplace.models_dir)
        return True

    def _on_language_changed(self, _language: str) -> None:
        retranslate_widget_tree(self)
        self.catalog_summary.setText(self._catalog_summary_text())
        self.hardware_detail.setText(self._hardware_detail_text())
        self._rebuild_cards()


__all__ = ["ModelCard", "ModelDownloadWorker", "ModelHubWindow"]
