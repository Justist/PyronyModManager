from pathlib import Path
from typing import Tuple, Union

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
   QComboBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
   QPushButton, QStatusBar, QTabWidget, QVBoxLayout, QWidget,
)

import pmm.core.games as games
import pmm.core.launcher as launcher
import pmm.core.parser as parser
import pmm.core.services as services
import pmm.core.storage as storage
import pmm.core.updater as updater
from pmm.core.models import Game, ModCollection, Preferences
from pmm.ui.collection_dialogs import CollectionNameDialog
from pmm.ui.conflict_view import ConflictView
from pmm.ui.mod_list import ModListWidget
from pmm.ui.settings_dialog import SettingsDialog
from pmm.ui.update_banner import UpdateBanner
from pmm.core.watcher import ModWatcher


class MainWindow(QMainWindow):
   def __init__(self) -> None:
      super().__init__()
      self.setWindowTitle("Pyrony Mod Manager")
      self.resize(960, 680)

      self._prefs: Preferences = storage.load_or_default(Preferences, "prefs.json")
      self._all_mods = []
      self._update_checker: updater.UpdateChecker
      self._watcher: ModWatcher

      # ── top bar ─────────────────────────────────────────────────────────
      self._game_box = QComboBox()
      self._coll_box = QComboBox()
      self._coll_box.setMinimumWidth(180)

      self._btn_new = QPushButton("＋ New")
      self._btn_rename = QPushButton("✎ Rename")
      self._btn_delete = QPushButton("🗑 Delete")
      self._play_btn = QPushButton("▶  Play")
      self._apply_btn = QPushButton("✔ Apply")
      self._settings_btn = QPushButton("⚙")

      self._btn_new.setFixedWidth(80)
      self._btn_rename.setFixedWidth(90)
      self._btn_delete.setFixedWidth(80)
      self._play_btn.setFixedWidth(90)
      self._settings_btn.setFixedWidth(32)
      self._apply_btn.setFixedWidth(90)
      self._settings_btn.setFixedWidth(32)
      self._settings_btn.setToolTip("Settings")

      for g in games.KNOWN_GAMES:
         self._game_box.addItem(g.display_name, userData=g.id)
      if self._prefs.active_game_id:
         idx = self._game_box.findData(self._prefs.active_game_id)
         if idx >= 0:
            self._game_box.setCurrentIndex(idx)

      top = QHBoxLayout()
      top.addWidget(QLabel("Game:"))
      top.addWidget(self._game_box)
      top.addSpacing(12)
      top.addWidget(QLabel("Collection:"))
      top.addWidget(self._coll_box)
      top.addWidget(self._btn_new)
      top.addWidget(self._btn_rename)
      top.addWidget(self._btn_delete)
      top.addStretch()
      top.addWidget(self._apply_btn)
      top.addSpacing(4)
      top.addWidget(self._play_btn)
      top.addSpacing(4)
      top.addWidget(self._settings_btn)

      # ── update banner (hidden until an update is found) ──────────────────
      self._update_banner = UpdateBanner()

      # ── tabs ─────────────────────────────────────────────────────────────
      self._tabs = QTabWidget()
      self._mod_list_widget = ModListWidget()
      # Apply stored font size to mod list entries.
      if getattr(self._prefs, "font_size", 0):
         self._mod_list_widget.set_font_size(self._prefs.font_size)
      self._conflict_view = ConflictView()
      self._tabs.addTab(self._mod_list_widget, "Load Order")
      self._tabs.addTab(self._conflict_view, "Conflicts")

      # ── root layout ──────────────────────────────────────────────────────
      root = QWidget()
      layout = QVBoxLayout(root)
      layout.addLayout(top)
      layout.addWidget(self._update_banner)
      layout.addWidget(self._tabs)
      self.setCentralWidget(root)
      self.setStatusBar(QStatusBar())

      # ── signals ──────────────────────────────────────────────────────────
      self._game_box.currentIndexChanged.connect(self._on_game_changed)
      self._coll_box.currentIndexChanged.connect(self._on_collection_changed)
      self._mod_list_widget.order_changed.connect(self._on_order_changed)
      self._btn_new.clicked.connect(self._on_new_collection)
      self._btn_rename.clicked.connect(self._on_rename_collection)
      self._btn_delete.clicked.connect(self._on_delete_collection)
      self._apply_btn.clicked.connect(self._on_apply)
      self._play_btn.clicked.connect(self._on_play)
      self._settings_btn.clicked.connect(self._on_settings)

      self._refresh_game()

      # Start update check 2 s after the window appears (non-blocking).
      QTimer.singleShot(2000, self._start_update_check)

   # ── window lifecycle ──────────────────────────────────────────────────────

   def closeEvent(self, event) -> None:
      self._stop_watcher()
      super().closeEvent(event)

   # ── update check ─────────────────────────────────────────────────────────

   def _start_update_check(self) -> None:
      if not self._prefs.check_for_updates:
         return
      self._update_checker = updater.UpdateChecker(parent=self)
      self._update_checker.update_available.connect(self._on_update_available)
      self._update_checker.check_failed.connect(
         lambda msg: self.statusBar().showMessage(f"Update check failed: {msg}", 5000)
      )
      self._update_checker.start()

   def _on_update_available(self, info: updater.ReleaseInfo) -> None:
      release: updater.ReleaseInfo = info
      self._update_banner.show_update(release.version, release.html_url)

   # ── settings ─────────────────────────────────────────────────────────────

   def _on_settings(self) -> None:
      dlg = SettingsDialog(self._prefs, parent=self)
      if dlg.exec() == SettingsDialog.DialogCode.Accepted:
         storage.save(self._prefs, "prefs.json")
         # Re-apply font size and reload game state.
         if getattr(self._prefs, "font_size", 0):
            self._mod_list_widget.set_font_size(self._prefs.font_size)
         self._refresh_game()
         self.statusBar().showMessage("Settings saved.")

   # ── watcher ───────────────────────────────────────────────────────────────

   def _start_watcher(self, mod_dir) -> None:
      self._stop_watcher()
      self._watcher = ModWatcher(mod_dir, parent=self)
      self._watcher.mods_changed.connect(self._on_mods_changed)
      self._watcher.start()

   def _stop_watcher(self) -> None:
      if hasattr(self, "_watcher") and self._watcher is not None:
         self._watcher.stop()
         self._watcher = None

   def _on_mods_changed(self) -> None:
      self.statusBar().showMessage("Mod directory changed — reloading…")
      self._refresh_game()

   # ── game / collection slots ───────────────────────────────────────────────

   def _on_game_changed(self, _) -> None:
      self._prefs.active_game_id = self._game_box.currentData()
      storage.save(self._prefs, "prefs.json")
      self._refresh_game()

   def _on_collection_changed(self, _) -> None:
      self._prefs.active_collection = self._coll_box.currentData() or ""
      storage.save(self._prefs, "prefs.json")
      self._refresh_list()
      self._refresh_coll_buttons()

   def _on_order_changed(self, new_order: list[str]) -> None:
      coll = self._active_collection()
      if coll:
         coll.mods = new_order
         storage.save(self._prefs, "prefs.json")

   # ── collection CRUD ───────────────────────────────────────────────────────

   def _on_new_collection(self) -> None:
      existing = self._collection_names_for_game()
      dlg = CollectionNameDialog("New Collection", existing, parent=self)
      if dlg.exec() != CollectionNameDialog.DialogCode.Accepted:
         return
      new_coll = ModCollection(name=dlg.name, game_id=self._prefs.active_game_id)
      self._prefs.collections.append(new_coll)
      storage.save(self._prefs, "prefs.json")
      self._repopulate_coll_box(select=dlg.name)
      self.statusBar().showMessage(f'Collection "{dlg.name}" created.')

   def _on_rename_collection(self) -> None:
      coll = self._active_collection()
      if not coll:
         return
      existing = self._collection_names_for_game()
      dlg = CollectionNameDialog(
         "Rename Collection", existing, initial=coll.name, parent=self
      )
      if dlg.exec() != CollectionNameDialog.DialogCode.Accepted:
         return
      old_name = coll.name
      coll.name = dlg.name
      if self._prefs.active_collection == old_name:
         self._prefs.active_collection = dlg.name
      storage.save(self._prefs, "prefs.json")
      self._repopulate_coll_box(select=dlg.name)
      self.statusBar().showMessage(f'Renamed "{old_name}" → "{dlg.name}".')

   def _on_delete_collection(self) -> None:
      coll = self._active_collection()
      if not coll:
         return
      answer = QMessageBox.question(
         self,
         "Delete Collection",
         f'Delete collection "{coll.name}"?\nThis cannot be undone.',
         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
         QMessageBox.StandardButton.No,
      )
      if answer != QMessageBox.StandardButton.Yes:
         return
      name = coll.name
      self._prefs.collections = [
         c for c in self._prefs.collections if c is not coll
      ]
      if self._prefs.active_collection == name:
         self._prefs.active_collection = ""
      storage.save(self._prefs, "prefs.json")
      self._repopulate_coll_box()
      self.statusBar().showMessage(f'Collection "{name}" deleted.')

   # ── apply / play ──────────────────────────────────────────────────────────

   def _apply_playset(self) -> Tuple[Game, Path] | None:
      """
      Write the active playset to the game's load-order files.

      Returns (game, written_to) on success, or None when preconditions fail.
      Shows errors inline so callers never need to catch.

      Also warns when the resolved user-data directory did not exist before
      this call — that usually means the auto-detected path is wrong and the
      user should set an override in Settings.
      """
      coll = self._active_collection()
      game = games.get_game(self._prefs.active_game_id)
      if not coll or not game:
         self.statusBar().showMessage(
            "Select a game and collection first.", 4000
         )
         return None

      # Resolve path early so we can warn *before* creating the directory.
      user_data = games.get_effective_user_data(game, self._prefs.game_paths)
      if user_data is None:
         QMessageBox.warning(
            self,
            "No user-data path",
            f"No user-data path is configured for {game.display_name}.\n"
            "Set it in Settings → Game user-data paths.",
         )
         return None

      path_existed = user_data.exists()

      try:
         written_to = launcher.write_launcher_files(
            game, coll, self._all_mods,
            game_paths=self._prefs.game_paths,
         )
      except Exception as exc:
         QMessageBox.critical(self, "Apply error", str(exc))
         return None

      n = len(coll.mods)
      self.statusBar().showMessage(
         f"Playset applied — {n} mod(s) written to {written_to}", 6000
      )

      if not path_existed:
         QMessageBox.warning(
            self,
            "New directory created",
            f"The directory\n  {written_to}\n"
            "did not exist and was created now.\n\n"
            "If your game stores data elsewhere, open Settings and set "
            "the correct user-data path for this game.",
         )
      storage.save(self._prefs, "prefs.json")
      return game, written_to

   def _on_apply(self) -> None:
      """Apply the active playset to the game files without launching."""
      self._apply_playset()

   def _on_play(self) -> None:
      """Apply the active playset and then launch the game via Steam."""
      result = self._apply_playset()
      if result is None:
         return
      game, _ = result
      try:
         launcher.launch_game(game)
         self.statusBar().showMessage(f"Launching {game.display_name}…", 5000)
      except Exception as exc:
         QMessageBox.critical(self, "Launch error", str(exc))

   # ── refresh helpers ───────────────────────────────────────────────────────

   def _refresh_game(self) -> None:
      game = games.get_game(self._prefs.active_game_id)
      self._all_mods = []
      if game:
         mod_dir = games.get_mod_dir(game)
         if mod_dir and mod_dir.exists():
            self._all_mods = parser.discover_mods(mod_dir)
            self.statusBar().showMessage(
               f"Loaded {len(self._all_mods)} mods from {mod_dir}"
            )
         else:
            self.statusBar().showMessage(f"Mod directory not found: {mod_dir}")
      self._repopulate_coll_box(select=self._prefs.active_collection or None)

   def _repopulate_coll_box(self, select: str | None = None) -> None:
      """Rebuild the collection combo for the current game without cascading signals."""
      self._coll_box.blockSignals(True)
      self._coll_box.clear()
      for c in self._prefs.collections:
         if c.game_id == self._prefs.active_game_id:
            self._coll_box.addItem(c.name, userData=c.name)
      if select:
         idx = self._coll_box.findData(select)
         if idx >= 0:
            self._coll_box.setCurrentIndex(idx)
      self._coll_box.blockSignals(False)

      self._prefs.active_collection = self._coll_box.currentData() or ""
      self._refresh_list()
      self._refresh_coll_buttons()

   def _refresh_list(self) -> None:
      coll = self._active_collection()
      ordered = coll.mods if coll else []
      self._mod_list_widget.load_mods(self._all_mods, ordered)
      ordered_mods = (
         services.resolve_load_order(self._all_mods, coll)
         if coll else self._all_mods
      )
      self._conflict_view.set_mods(ordered_mods)

   def _refresh_coll_buttons(self) -> None:
      has_coll = self._active_collection() is not None
      self._btn_rename.setEnabled(has_coll)
      self._btn_delete.setEnabled(has_coll)
      self._apply_btn.setEnabled(has_coll)
      self._play_btn.setEnabled(has_coll)
      self._mod_list_widget.set_active_enabled(has_coll)

   # ── helpers ───────────────────────────────────────────────────────────────

   def _active_collection(self) -> Union[ModCollection, None]:
      name = self._coll_box.currentData()
      return next(
         (
            c for c in self._prefs.collections
            if c.name == name and c.game_id == self._prefs.active_game_id
         ),
         None,
      )

   def _collection_names_for_game(self) -> set[str]:
      return {
         c.name for c in self._prefs.collections
         if c.game_id == self._prefs.active_game_id
      }
