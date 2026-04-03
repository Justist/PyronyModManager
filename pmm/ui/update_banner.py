import webbrowser

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton


class UpdateBanner(QFrame):
   """
   A thin dismissible bar shown when a newer release is available.
   Hidden by default; call show_update() to display it.
   """

   dismissed = Signal()

   def __init__(self, parent=None) -> None:
      super().__init__(parent)
      self.setObjectName("UpdateBanner")
      self.setStyleSheet(
         "#UpdateBanner { background: #1e4620; border-radius: 4px;"
         "  border: 1px solid #2e6b32; }"
         "QLabel { color: #b6f0bb; }"
         "QPushButton { color: #b6f0bb; background: transparent;"
         "  border: 1px solid #3a7a3e; border-radius: 3px; padding: 2px 10px; }"
         "QPushButton:hover { background: #2a5c2e; }"
      )

      self._msg = QLabel()
      self._view_btn = QPushButton("View release")
      self._dismiss = QPushButton("✕")
      self._dismiss.setFixedWidth(28)
      self._url = ""

      row = QHBoxLayout(self)
      row.setContentsMargins(10, 4, 6, 4)
      row.addWidget(self._msg)
      row.addStretch()
      row.addWidget(self._view_btn)
      row.addWidget(self._dismiss)

      self._view_btn.clicked.connect(self._on_view)
      self._dismiss.clicked.connect(self._on_dismiss)
      self.hide()

   def show_update(self, version: str, url: str) -> None:
      self._msg.setText(f"⬆  Version {version} is available")
      self._url = url
      self.show()

   def _on_view(self) -> None:
      if self._url:
         webbrowser.open(self._url)

   def _on_dismiss(self) -> None:
      self.hide()
      self.dismissed.emit()
