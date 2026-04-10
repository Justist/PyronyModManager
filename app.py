import sys
import traceback

from PySide6.QtWidgets import QApplication
from pmm.ui.main_window import MainWindow
from pmm.ui.error_util import show_error


def _log_exception(exc_type, exc_value, exc_tb) -> None:
    """
    Global exception hook: print a stack trace to stderr and show a dialog.

    This is invoked for uncaught exceptions, not for exceptions caught and handled by the app.
    """
    # Print full traceback to stderr
    traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)

    # Try to show a message box (if a QApplication exists)
    app = QApplication.instance()
    if app is not None:
        msg = "".join(traceback.format_exception_only(exc_type, exc_value)).strip()
        show_error(
            None,
            "Unhandled error",
            f"An unexpected error occurred:\n\n{msg}\n\n"
            "A detailed traceback has been written to stderr.",
        )


def main() -> None:
    # Install global exception hook before creating the application.
    sys.excepthook = _log_exception

    app = QApplication(sys.argv)
    app.setApplicationName("PyIrony")
    app.setOrganizationName("pyirony")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()