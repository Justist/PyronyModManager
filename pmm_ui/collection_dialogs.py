from typing import Any, Set

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QLineEdit, QVBoxLayout


class CollectionNameDialog(QDialog):
    """
    Reusable single-input dialog for creating or renaming a collection.

    existing_names  – names already in use; duplicates are rejected.
    initial         – pre-filled text for rename; excluded from dup check so
                      the user can confirm without changing the name.
    """

    def __init__(
        self,
        title: str,
        existing_names: Set[str],
        initial: str = "",
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(340)

        # On rename the current name is not a duplicate of itself.
        self._forbidden: Set[str] = existing_names - {initial}

        self._edit = QLineEdit(initial)
        self._edit.selectAll()

        self._err = QLabel()
        self._err.setStyleSheet("color: #cc2244; font-size: 11px;")
        self._err.hide()

        buttons = (
              QDialogButtonBox.StandardButton.Ok
              | QDialogButtonBox.StandardButton.Cancel # type: ignore[arg-type]
        )
        btns = QDialogButtonBox(buttons, parent=self)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        self._ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Collection name:"))
        layout.addWidget(self._edit)
        layout.addWidget(self._err)
        layout.addWidget(btns)

        self._edit.textChanged.connect(self._on_text_changed)
        self._on_text_changed(initial)  # set initial Ok-button state

    @property
    def name(self) -> str:
        return self._edit.text().strip()

    def _on_text_changed(self, text: str) -> None:
        stripped = text.strip()
        if not stripped:
            self._ok_btn.setEnabled(False)
            self._err.hide()
        elif stripped in self._forbidden:
            self._ok_btn.setEnabled(False)
            self._err.setText(f'"{stripped}" already exists.')
            self._err.show()
        else:
            self._ok_btn.setEnabled(True)
            self._err.hide()

    def _on_accept(self) -> None:
        if self.name and self.name not in self._forbidden:
            self.accept()