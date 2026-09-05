"""Navigation, tray, and application-level quit wiring for the Qt shell."""
from __future__ import annotations

from typing import Any


class ApplicationNavigationController:
    """Connect top-level navigation without making windows know each other."""

    def __init__(
        self,
        *,
        app: Any,
        main_window: Any,
        settings_window: Any,
        diagnostics_window: Any,
        tray: Any,
        installer_shutdown: Any,
    ) -> None:
        self.app = app
        self.main_window = main_window
        self.settings_window = settings_window
        self.diagnostics_window = diagnostics_window
        self.tray = tray
        self.installer_shutdown = installer_shutdown

    def connect(self) -> None:
        """Attach installer, tray, and main-window navigation signals."""
        self.installer_shutdown.shutdown_requested.connect(
            lambda: self.request_application_quit(show_blocker=False)
        )
        self._connect_tray()
        self._connect_main_window()

    def request_application_quit(self, *, show_blocker: bool = True) -> bool:
        """Quit only when settings/model operations allow application shutdown."""
        if not self.settings_window.can_close_application():
            if show_blocker:
                self._show_settings_blocker()
            return False
        self.app._voxsub_quitting = True  # type: ignore[attr-defined]
        # ``aboutToQuit`` owns the one-and-only Pipeline close. Calling stop here
        # as well could spend the bounded worker deadline twice.
        self.app.quit()
        return True

    def _show_settings_blocker(self) -> None:
        if not self.main_window.isVisible():
            self.main_window.show()
        self.main_window.show_settings_page()
        self.settings_window.tabs.setCurrentIndex(4)
        self.main_window.raise_()
        self.main_window.activateWindow()

    def _connect_tray(self) -> None:
        if self.tray is None:
            return
        tray = self.tray
        win = self.main_window
        tray.set_mode_state(win.current_mode())
        tray.set_running_state(win.cta.is_running())

        def on_mode_changed(mode: str) -> None:
            win.set_mode(mode)
            tray.set_mode_state(mode)
            if mode == "d":
                win.showNormal()
                win.raise_()
                win.activateWindow()

        def on_toggle_run() -> None:
            win._toggle_run()  # noqa: SLF001 - same UI shell boundary
            tray.set_running_state(win.cta.is_running())

        tray.mode_changed.connect(on_mode_changed)
        tray.toggle_run_requested.connect(on_toggle_run)
        tray.show_main_requested.connect(self.show_main)
        tray.settings_requested.connect(self.show_settings)
        tray.diagnostics_requested.connect(self.show_diagnostics)
        tray.quit_requested.connect(self.request_application_quit)

    def _connect_main_window(self) -> None:
        self.main_window.cta.clicked.connect(self.sync_tray_state)
        self.main_window.running_state_changed.connect(
            lambda _running: self.sync_tray_state()
        )
        self.main_window.settings_requested.connect(self.show_settings)
        self.main_window.diagnostics_requested.connect(self.show_diagnostics)
        self.main_window.model_hub_requested.connect(self.show_model_hub)

    def sync_tray_state(self) -> None:
        if self.tray is None:
            return
        self.tray.set_mode_state(self.main_window.current_mode())
        self.tray.set_running_state(self.main_window.cta.is_running())

    def show_main(self) -> None:
        self.main_window.showNormal()
        self.main_window.raise_()
        self.main_window.activateWindow()

    def show_settings(self) -> None:
        if not self.main_window.isVisible():
            self.main_window.show()
        self.main_window.show_settings_page()
        self.main_window.raise_()
        self.main_window.activateWindow()

    def show_diagnostics(self) -> None:
        self.diagnostics_window.showNormal()
        self.diagnostics_window.raise_()
        self.diagnostics_window.activateWindow()

    def show_model_hub(self) -> None:
        if not self.main_window.isVisible():
            self.main_window.show()
        self.main_window.show_model_hub_page()
        self.main_window.raise_()
        self.main_window.activateWindow()
