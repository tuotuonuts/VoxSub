from __future__ import annotations

from voxsub.ui.shutdown_coordinator import ApplicationShutdownCoordinator


class FakeSignal:
    def __init__(self) -> None:
        self._slots = []

    def connect(self, slot) -> None:
        self._slots.append(slot)

    def emit(self) -> None:
        for slot in list(self._slots):
            slot()


class FakeApp:
    def __init__(self) -> None:
        self.aboutToQuit = FakeSignal()


class FakeComponent:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events

    def shutdown(self) -> None:
        self.events.append(self.name)

    def prepare_for_page_leave(self) -> None:
        self.events.append(self.name)


class FakePipeline:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        self.events.append("pipeline")
        if self.fail:
            raise RuntimeError("close failed")


def make_coordinator(
    events: list[str], *, pipeline: FakePipeline | None = None
) -> tuple[FakeApp, ApplicationShutdownCoordinator]:
    app = FakeApp()
    coordinator = ApplicationShutdownCoordinator(
        app=app,
        model_hub=FakeComponent("model_hub", events),
        ocr_workspace=FakeComponent("ocr", events),
        settings_window=FakeComponent("settings", events),
        pipeline=pipeline or FakePipeline(events),
        error_reporting_shutdown=lambda: events.append("error_reporting"),
    )
    return app, coordinator


def test_shutdown_callbacks_keep_the_application_teardown_order() -> None:
    events: list[str] = []
    app, coordinator = make_coordinator(events)
    coordinator.connect()
    coordinator.connect()

    app.aboutToQuit.emit()

    assert events == [
        "model_hub",
        "ocr",
        "settings",
        "pipeline",
        "error_reporting",
    ]


def test_pipeline_failure_does_not_prevent_later_shutdown_callbacks() -> None:
    events: list[str] = []
    pipeline = FakePipeline(events, fail=True)
    app, coordinator = make_coordinator(events, pipeline=pipeline)
    coordinator.connect()

    app.aboutToQuit.emit()

    assert events == [
        "model_hub",
        "ocr",
        "settings",
        "pipeline",
        "error_reporting",
    ]
    assert pipeline.close_calls == 1
