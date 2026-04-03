from pathlib import Path
from typing import Tuple, Union

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
   QComboBox, QFileDialog, QHBoxLayout, QLabel, QMainWindow,
   QMenu, QMessageBox, QProgressDialog,
   QPushButton, QStatusBar, QTabWidget, QToolButton,
   QVBoxLayout, QWidget,
)

import pmm.core.games as games
import pmm.core.launcher as launcher
import pmm.core.parser as parser
import pmm.core.playset_io as playset_io
import pmm.core.services as services
import pmm.core.storage as storage
import pmm.core.updater as updater
from pmm.core.models import Game, ModCollection, Preferences
from pmm.core.watcher import ModWatcher
from pmm.ui.collection_dialogs import CollectionNameDialog
from pmm.ui.conflict_view import ConflictView
from pmm.ui.mod_list import ModListWidget
from pmm.ui.settings_dialog import SettingsDialog
from pmm.ui.update_banner import UpdateBanner


class MainWindow(QMainWindow):
   def __init__(self) -> None:
      super().__init__()
      self.setWindowTitle("Pyrony Mod Manager")
      self.resize(960, 680)

      self._prefs: Preferences = storage.load_or_default(Preferences, "prefs.json")
      self._all_mods: list = []
      self._update_checker: updater.UpdateChecker
      self._watcher: ModWatcher
      self._zip_worker: playset_io.ZipExportWorker | playset_io.ZipImportWorker | None = None
      self._zip_progress_dlg: QProgressDialog | None = None

      # ── top bar ──────────────────────────────────────────────────────────
      self._game_box = QComboBox()
      self._coll_box = QComboBox()
      self._coll_box.setMinimumWidth(180)

      self._btn_new = QPushButton("＋ New")
      self._btn_rename = QPushButton("✎ Rename")
      self._btn_delete = QPushButton("🗑 Delete")
      self._play_btn = QPushButton("▶ Play")
      self._apply_btn = QPushButton("✔ Apply")
      self._settings_btn = QPushButton("⚙")

      self._btn_new.setFixedWidth(80)
      self._btn_rename.setFixedWidth(90)
      self._btn_delete.setFixedWidth(80)
      self._play_btn.setFixedWidth(90)
      self._apply_btn.setFixedWidth(90)
      self._settings_btn.setFixedWidth(32)
      self._settings_btn.setToolTip("Settings")

      # ── import / export drop-down ─────────────────────────────────────────
      self._io_btn = QToolButton()
      self._io_btn.setText("⇅ I/O")
      self._io_btn.setToolTip("Import / Export playset")
      self._io_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
      self._io_btn.setMenu(self._build_io_menu())
      self._io_btn.setFixedWidth(72)

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
      top.addWidget(self._io_btn)
      top.addStretch()
      top.addWidget(self._apply_btn)
      top.addSpacing(4)
      top.addWidget(self._play_btn)
      top.addSpacing(4)
      top.addWidget(self._settings_btn)

      # ── update banner (hidden until an update is found) ───────────────────
      self._update_banner = UpdateBanner()

      # ── tabs ──────────────────────────────────────────────────────────────
      self._tabs = QTabWidget()
      self._mod_list_widget = ModListWidget()
      if getattr(self._prefs, "font_size", 0):
         self._mod_list_widget.set_font_size(self._prefs.font_size)
      self._conflict_view = ConflictView()
      self._tabs.addTab(self._mod_list_widget, "Load Order")
      self._tabs.addTab(self._conflict_view, "Conflicts")

      # ── root layout ───────────────────────────────────────────────────────
      root = QWidget()
      layout = QVBoxLayout(root)
      layout.addLayout(top)
      layout.addWidget(self._update_banner)
      layout.addWidget(self._tabs)
      self.setCentralWidget(root)
      self.setStatusBar(QStatusBar())

      # ── signals ───────────────────────────────────────────────────────────
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
      QTimer.singleShot(2000, self._start_update_check)

   # ── window lifecycle ──────────────────────────────────────────────────────

   def closeEvent(self, event) -> None:
      self._stop_watcher()
      super().closeEvent(event)

   # ── import / export menu ──────────────────────────────────────────────────

   def _build_io_menu(self) -> QMenu:
      menu = QMenu(self)
      menu.addAction("📥  Import playset (JSON…)", self._on_import_json)
      menu.addAction("📤  Export playset (JSON…)", self._on_export_json)
      menu.addSeparator()
      menu.addAction("📦  Import playset (ZIP…)", self._on_import_zip)
      menu.addAction("🗜   Export playset (ZIP…)", self._on_export_zip)
      return menu

   # ── JSON export ───────────────────────────────────────────────────────────

   def _on_export_json(self) -> None:
      coll = self._active_collection()
      if not coll:
         self.statusBar().showMessage("Select a collection to export.", 4000)
         return

      default = str(Path.home() / playset_io.suggested_json_filename(coll))
      path, _ = QFileDialog.getSaveFileName(
         self, "Export Playset JSON", default, "JSON files (*.json)"
      )
      if not path:
         return

      dest = Path(path)
      try:
         playset_io.export_launcher_json(coll, self._all_mods, dest)
      except Exception as exc:
         QMessageBox.critical(self, "Export error", str(exc))
         return

      self.statusBar().showMessage(
         f'Playset "{coll.name}" exported → {dest.name}', 6000
      )

   # ── JSON import ───────────────────────────────────────────────────────────

   def _on_import_json(self) -> None:
      path, _ = QFileDialog.getOpenFileName(
         self, "Import Playset JSON", str(Path.home()),
         "JSON files (*.json);;All files (*)"
      )
      if not path:
         return

      src = Path(path)
      game_id = self._prefs.active_game_id
      if not game_id:
         QMessageBox.warning(self, "No game selected",
                             "Select a game before importing a playset.")
         return

      # Reject duplicate names; the user can rename the collection afterwards.
      existing = self._collection_names_for_game()
      coll_name = src.stem
      if coll_name in existing:
         coll_name = self._unique_name(coll_name, existing)

      try:
         result = playset_io.import_launcher_json(
            src, game_id, self._all_mods, collection_name=coll_name
         )
      except Exception as exc:
         QMessageBox.critical(self, "Import error", str(exc))
         return

      self._prefs.collections.append(result.collection)
      storage.save(self._prefs, "prefs.json")
      self._repopulate_coll_box(select=result.collection.name)

      msg = f'Imported "{result.collection.name}": {result.summary()}'
      if result.unmatched:
         detail = ", ".join(result.unmatched[:5])
         if len(result.unmatched) > 5:
            detail += f" (+{len(result.unmatched) - 5} more)"
         QMessageBox.information(
            self, "Import complete",
            f"{msg}\n\nNot found locally (may need to subscribe first):\n{detail}",
         )
      else:
         self.statusBar().showMessage(msg, 6000)

   # ── ZIP export ────────────────────────────────────────────────────────────

   def _on_export_zip(self) -> None:
      coll = self._active_collection()
      if not coll:
         self.statusBar().showMessage("Select a collection to export.", 4000)
         return

      default = str(Path.home() / playset_io.suggested_zip_filename(coll))
      path, _ = QFileDialog.getSaveFileName(
         self, "Export Playset ZIP", default, "ZIP archives (*.zip)"
      )
      if not path:
         return

      dest = Path(path)
      total = len(coll.mods)

      self._zip_progress_dlg = QProgressDialog(
         f'Packing "{coll.name}"…', "", 0, total, self
      )
      assert self._zip_progress_dlg is not None
      self._zip_progress_dlg.setWindowTitle("Export Playset")
      self._zip_progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
      self._zip_progress_dlg.setMinimumDuration(400)

      self._zip_worker = playset_io.ZipExportWorker(
         coll, self._all_mods, dest, parent=self
      )
      assert self._zip_worker is not None
      self._zip_worker.progress.connect(self._on_zip_progress)
      self._zip_worker.finished.connect(self._on_zip_export_done)
      self._zip_worker.error.connect(self._on_zip_worker_error)
      self._zip_worker.start()

   def _on_zip_export_done(self, dest: Path) -> None:
      self._close_zip_progress()
      self.statusBar().showMessage(
         f"Playset exported → {dest.name}  ({dest.stat().st_size // 1024:,} KB)", 8000
      )

   # ── ZIP import ────────────────────────────────────────────────────────────

   def _on_import_zip(self) -> None:
      path, _ = QFileDialog.getOpenFileName(
         self, "Import Playset ZIP", str(Path.home()),
         "ZIP archives (*.zip);;All files (*)"
      )
      if not path:
         return

      src = Path(path)
      game_id = self._prefs.active_game_id
      game = games.get_game(game_id)
      if not game:
         QMessageBox.warning(self, "No game selected",
                             "Select a game before importing a playset.")
         return

      # Resolve the mod directory; prefer the effective (override-aware) path.
      user_data = games.get_effective_user_data(game, self._prefs.game_paths)
      mod_dir = (user_data / "mod") if user_data else games.get_mod_dir(game)
      if mod_dir is None:
         QMessageBox.warning(
            self, "No mod directory",
            f"Could not determine the mod directory for {game.display_name}.\n"
            "Set it in Settings → Game user-data paths.",
         )
         return

      # Ask once about overwrite policy.
      reply = QMessageBox.question(
         self,
         "Import ZIP — existing mods",
         "What should happen when a mod folder already exists?\n\n"
         "Yes    — overwrite with the version from the ZIP\n"
         "No     — keep the installed version (skip that mod)",
         QMessageBox.StandardButton.Yes
         | QMessageBox.StandardButton.No
         | QMessageBox.StandardButton.Cancel,
         QMessageBox.StandardButton.No,
      )
      if reply == QMessageBox.StandardButton.Cancel:
         return
      overwrite = reply == QMessageBox.StandardButton.Yes

      # Derive collection name; make it unique.
      existing = self._collection_names_for_game()
      coll_name = self._unique_name(src.stem, existing)

      self._zip_progress_dlg = QProgressDialog(
         f'Extracting "{src.name}"…', "", 0, 0, self
      )
      assert self._zip_progress_dlg is not None
      self._zip_progress_dlg.setWindowTitle("Import Playset")
      self._zip_progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
      self._zip_progress_dlg.setMinimumDuration(400)

      self._zip_worker = playset_io.ZipImportWorker(
         src, game_id, mod_dir, overwrite, coll_name, parent=self
      )
      assert self._zip_worker is not None
      self._zip_worker.progress.connect(self._on_zip_progress)
      self._zip_worker.finished.connect(self._on_zip_import_done)
      self._zip_worker.error.connect(self._on_zip_worker_error)
      self._zip_worker.start()

   def _on_zip_import_done(self, result: object) -> None:
      r: playset_io.ImportResult = result  # type: ignore[assignment]
      self._close_zip_progress()

      self._prefs.collections.append(r.collection)
      storage.save(self._prefs, "prefs.json")

      # Reload mods so newly extracted ones are available.
      self._refresh_game()
      self._repopulate_coll_box(select=r.collection.name)

      summary = r.summary()
      if r.skipped_mods or r.unmatched:
         details: list[str] = []
         if r.skipped_mods:
            details.append(
               "Skipped (already installed):\n"
               + "\n".join(f"  • {f}" for f in r.skipped_mods[:10])
               + (f"\n  … and {len(r.skipped_mods) - 10} more" if len(r.skipped_mods) > 10 else "")
            )
         if r.unmatched:
            details.append(
               "Not matched to installed mods:\n"
               + "\n".join(f"  • {n}" for n in r.unmatched[:10])
               + (f"\n  … and {len(r.unmatched) - 10} more" if len(r.unmatched) > 10 else "")
            )
         QMessageBox.information(
            self,
            f'Imported "{r.collection.name}"',
            f"{summary}\n\n" + "\n\n".join(details),
         )
      else:
         self.statusBar().showMessage(
            f'Imported "{r.collection.name}": {summary}', 8000
         )

   # ── shared ZIP helpers ────────────────────────────────────────────────────

   def _on_zip_progress(self, done: int, total: int, name: str) -> None:
      if self._zip_progress_dlg is None:
         return
      if total > 0:
         self._zip_progress_dlg.setMaximum(total)
      self._zip_progress_dlg.setValue(done)
      if name:
         self._zip_progress_dlg.setLabelText(f"Processing: {name}")

   def _on_zip_worker_error(self, msg: str) -> None:
      self._close_zip_progress()
      QMessageBox.critical(self, "Error", msg)

   def _close_zip_progress(self) -> None:
      if self._zip_progress_dlg:
         self._zip_progress_dlg.close()
         self._zip_progress_dlg = None

   # ── update check ──────────────────────────────────────────────────────────

   def _start_update_check(self) -> None:
      if not self._prefs.check_for_updates:
         return
      self._update_checker = updater.UpdateChecker(parent=self)
      self._update_checker.update_available.connect(self._on_update_available)
      self._update_checker.check_failed.connect(
         lambda msg: self.statusBar().showMessage(
            f"Update check failed: {msg}", 5000
         )
      )
      self._update_checker.start()

   def _on_update_available(self, info: updater.ReleaseInfo) -> None:
      self._update_banner.show_update(info.version, info.html_url)

   # ── settings ──────────────────────────────────────────────────────────────

   def _on_settings(self) -> None:
      dlg = SettingsDialog(self._prefs, parent=self)
      if dlg.exec() == SettingsDialog.DialogCode.Accepted:
         storage.save(self._prefs, "prefs.json")
         if getattr(self._prefs, "font_size", 0):
            self._mod_list_widget.set_font_size(self._prefs.font_size)
         self._refresh_game()
         self.statusBar().showMessage("Settings saved.")

   # ── watcher ───────────────────────────────────────────────────────────────

   def _start_watcher(self, mod_dir: Path) -> None:
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
      new_coll = ModCollection(
         name=dlg.name, game_id=self._prefs.active_game_id
      )
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
      """
      coll = self._active_collection()
      game = games.get_game(self._prefs.active_game_id)
      if not coll or not game:
         self.statusBar().showMessage(
            "Select a game and collection first.", 4000
         )
         return None

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
      self._apply_playset()

   def _on_play(self) -> None:
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
            self._start_watcher(mod_dir)
         else:
            self.statusBar().showMessage(
               f"Mod directory not found: {mod_dir}"
            )
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

   @staticmethod
   def _unique_name(base: str, existing: set[str]) -> str:
      """Return *base* or 'base (2)', 'base (3)', … until unique."""
      if base not in existing:
         return base
      n = 2
      while f"{base} ({n})" in existing:
         n += 1
      return f"{base} ({n})"
