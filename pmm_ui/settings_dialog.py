from pathlib import Path
from typing import Dict

from PySide6.QtWidgets import (
   QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
   QFormLayout, QGroupBox, QHBoxLayout,
   QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

import pmm_games
from pmm_models import Preferences


class SettingsDialog(QDialog):
   """
   Settings dialog for game user-data path overrides and app preferences.

   Mutates the Preferences object in-place on accept; the caller is
   responsible for persisting it with pmm_storage.save().
   """

   def __init__(self, prefs: Preferences, parent=None) -> None:
      super().__init__(parent)
      self.setWindowTitle("Settings")
      self.setMinimumWidth(560)
      self._prefs = prefs
      self._path_edits: Dict[str, QLineEdit] = {}

      layout = QVBoxLayout(self)
      layout.addWidget(self._build_paths_group())
      layout.addWidget(self._build_app_group())
      layout.addWidget(self._build_buttons())

   def _build_paths_group(self) -> QGroupBox:
      group = QGroupBox("Game user-data paths")
      group.setToolTip(
         "Leave blank to use the auto-detected path.\n"
         "Override only if your game data is in a non-standard location."
      )
      form = QFormLayout(group)

      for game in pmm_games.KNOWN_GAMES:
         override = self._prefs.game_paths.get(game.id, "")
         detected = str(game.user_data_path) if game.user_data_path else ""

         edit = QLineEdit(override)
         edit.setPlaceholderText(detected or "Not auto-detected")
         self._path_edits[game.id] = edit

         browse_btn = QPushButton("…")
         browse_btn.setFixedWidth(28)
         browse_btn.setToolTip("Browse…")
         browse_btn.clicked.connect(lambda checked, e=edit: self._browse(e))

         clear_btn = QPushButton("↺")
         clear_btn.setFixedWidth(28)
         clear_btn.setToolTip("Reset to auto-detected path")
         clear_btn.clicked.connect(lambda checked, e=edit: e.clear())

         row = QWidget()
         row_layout = QHBoxLayout(row)
         row_layout.setContentsMargins(0, 0, 0, 0)
         row_layout.addWidget(edit)
         row_layout.addWidget(browse_btn)
         row_layout.addWidget(clear_btn)

         form.addRow(f"{game.display_name}:", row)

      return group

   def _build_app_group(self) -> QGroupBox:
      group = QGroupBox("Application")
      layout = QVBoxLayout(group)
      self._update_check = QCheckBox("Check for updates on startup")
      self._update_check.setChecked(self._prefs.check_for_updates)
      layout.addWidget(self._update_check)
      return group

   def _build_buttons(self) -> QDialogButtonBox:
      btns = QDialogButtonBox(
         QDialogButtonBox.StandardButton.Ok
         | QDialogButtonBox.StandardButton.Cancel  # type: ignore[arg-type]
      )
      btns.accepted.connect(self._on_accept)
      btns.rejected.connect(self.reject)
      return btns

   def _browse(self, edit: QLineEdit) -> None:
      start = edit.text().strip() or str(Path.home())
      if path := QFileDialog.getExistingDirectory(
            self, "Select game user-data folder", start
      ):
         edit.setText(path)

   def _on_accept(self) -> None:
      self._prefs.game_paths = {
         gid: edit.text().strip()
         for gid, edit in self._path_edits.items()
         if edit.text().strip()
      }
      self._prefs.check_for_updates = self._update_check.isChecked()
      self.accept()
