import sys
import traceback

from PySide6.QtWidgets import QMessageBox, QWidget


def show_error(
      parent: QWidget | None,
      title: str,
      error: BaseException | str,
) -> None:
   """Show a critical dialog; log traceback to stderr when an exception is provided."""
   if isinstance(error, BaseException):
      traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)

   dlg = QMessageBox(parent)
   dlg.setIcon(QMessageBox.Icon.Critical)
   dlg.setWindowTitle(title)
   dlg.setText(str(error))
   dlg.exec()


def show_warning(
      parent: QWidget | None,
      title: str,
      warning: BaseException | str,
) -> None:
   """Show a warning dialog; log traceback to stderr when an exception is provided."""
   if isinstance(warning, BaseException):
      traceback.print_exception(type(warning), warning, warning.__traceback__, file=sys.stderr)
   QMessageBox.warning(parent, title, str(warning))


