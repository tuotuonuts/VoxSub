from __future__ import annotations

from dataclasses import dataclass

from voxsub.ui.navigation_controller import ApplicationNavigationController


class FakeSignal:
    def __init__(self) -> None:
        self._slots = []

    def connect(self, slot) -> None:
        self._slots.append(slot)

    def emit(self, *args) -> None:
        for slot in list(self._slots):
            slot(*args)


class FakeApp:
    def __init__(self) -> None:
        self._voxsub_quitting = False
        self.quit_calls = 0

    def quit(self) -> None:
        self.quit_calls += 1


class FakeTabs:
    def __init__(self) -> None:
        self.current_index = None

    def setCurrentIndex(self, index: int) -> None:
        self.current_index = index


class FakeSettingsWindow:
    def __init__(self, can_close: bool) -> None:
        self._can_close = can_close
        self.tabs = FakeTabs()

    def can_close_application(self) -> bool:
        return self._can_close


class FakeCta:
    def __init__(self) -> None:
        self.clicked = FakeSignal()
        self.running = False

    def is_running(self) -> bool:
        return self.running


class FakeMainWindow:
    def __init__(self) -> None:
        self.cta = FakeCta()
        self.running_state_changed = FakeSignal()
        self.settings_requested = FakeSignal()
        self.diagnostics_requested = FakeSignal()
        self.model_hub_requested = FakeSignal()
        self.visible = False
        self.settings_shown = 0
        self.diagnostics_shown = 0
        self.model_hub_shown = 0
        self.raise_calls = 0
        self.activate_calls = 0

    def isVisible(self) -> bool:
        return self.visible

    def show(self) -> None:
        self.visible = True

    def showNormal(self) -> None:
        self.visible = True

    def show_settings_page(self) -> None:
        self.settings_shown += 1

    def show_model_hub_page(self) -> None:
        self.model_hub_shown += 1

    def raise_(self) -> None:
        self.raise_calls += 1

    def activateWindow(self) -> None:
        self.activate_calls += 1

    def current_mode(self) -> str:
        return "d"

    def set_mode(self, _mode: str) -> None:
        pass


class FakeDiagnosticsWindow:
    def __init__(self) -> None:
        self.shown = 0

    def showNormal(self) -> None:
        self.shown += 1

    def raise_(self) -> None:
        pass

    def activateWindow(self) -> None:
        pass


class FakeInstallerShutdown:
    def __init__(self) -> None:
        self.shutdown_requested = FakeSignal()


@dataclass
class Runtime:
    app: FakeApp
    main: FakeMainWindow
    settings: FakeSettingsWindow
    diagnostics: FakeDiagnosticsWindow
    installer: FakeInstallerShutdown


def make_runtime(*, can_close: bool) -> Runtime:
    return Runtime(
        app=FakeApp(),
        main=FakeMainWindow(),
        settings=FakeSettingsWindow(can_close),
        diagnostics=FakeDiagnosticsWindow(),
        installer=FakeInstallerShutdown(),
    )


def make_controller(runtime: Runtime) -> ApplicationNavigationController:
    return ApplicationNavigationController(
        app=runtime.app,
        main_window=runtime.main,
        settings_window=runtime.settings,
        diagnostics_window=runtime.diagnostics,
        tray=None,
        installer_shutdown=runtime.installer,
    )


def test_quit_is_blocked_and_shows_settings_when_operations_are_active() -> None:
    runtime = make_runtime(can_close=False)
    controller = make_controller(runtime)
    controller.connect()

    assert controller.request_application_quit() is False
    assert runtime.app.quit_calls == 0
    assert runtime.main.settings_shown == 1
    assert runtime.settings.tabs.current_index == 4
    assert runtime.main.visible is True


def test_navigation_signals_and_installer_shutdown_use_controller_protocol() -> None:
    runtime = make_runtime(can_close=True)
    controller = make_controller(runtime)
    controller.connect()

    runtime.main.settings_requested.emit()
    assert runtime.main.settings_shown == 1
    runtime.main.model_hub_requested.emit()
    assert runtime.main.model_hub_shown == 1
    runtime.main.diagnostics_requested.emit()
    assert runtime.diagnostics.shown == 1

    runtime.installer.shutdown_requested.emit()
    assert runtime.app._voxsub_quitting is True
    assert runtime.app.quit_calls == 1
