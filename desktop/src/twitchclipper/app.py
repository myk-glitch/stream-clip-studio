"""Application bootstrap."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .core.context import create_context
from .ui import theme
from .ui.main_window import MainWindow


def run(argv: list[str] | None = None) -> int:
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("TwitchClipper")
    app.setOrganizationName("TwitchClipper")
    theme.apply(app)
    ctx = create_context()
    win = MainWindow(ctx)
    win.show()
    return app.exec()
