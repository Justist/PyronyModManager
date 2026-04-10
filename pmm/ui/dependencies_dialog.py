from typing import Dict, List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
   QAbstractItemView,
   QDialog,
   QDialogButtonBox,
   QHBoxLayout,
   QLabel,
   QLineEdit, QListWidget,
   QListWidgetItem,
   QMessageBox,
   QPushButton,
   QVBoxLayout,
   QWidget,
)

import pmm.core.games as games
import pmm.core.parser as parser
from pmm.core.models import Mod, Preferences
from pmm.ui.error_util import show_warning


def _name_matches_query(name: str, query: str) -> bool:
   """
   Case-insensitive multi-word match:
     - Split query into words.
     - All words must appear somewhere in the name, in any order.
     - Works with partial words ("ui dyn" matches "UI Overhaul Dynamic",
       "ami over" also matches).
   """
   q = query.strip().lower()
   if not q:
      return True
   words = [w for w in q.split() if w]
   return all(w in name.lower() for w in words) if words else True

def _filter_list_widget(widget: QListWidget, query: str) -> None:
   """Hide/show items in a QListWidget based on name/query matching."""
   for i in range(widget.count()):
      item = widget.item(i)
      if item is None:
         continue
      name = item.text()
      item.setHidden(not _name_matches_query(name, query))

class DependenciesDialog(QDialog):
   """
   Edit user-defined mod dependencies for a single game.

   For the selected game_id this dialog:

     * Loads all mods from the game's mod directory.
     * Lets the user pick, for each mod, which other mods it depends on.
     * Writes the result into prefs.user_dependencies[game_id][mod_id] = [dep_id, …].

   These dependencies are merged with descriptor.mod dependencies and
   apply to all playsets for that game.
   """

   def __init__(
         self,
         prefs: Preferences,
         game_id: str,
         parent: QWidget | None = None,
         initial_mod_id: str = "",
   ) -> None:
      super().__init__(parent)
      self.setWindowTitle("Edit mod dependencies")
      self.resize(620, 400)

      self._prefs = prefs
      self._game_id = game_id
      self._initial_mod_id = initial_mod_id
      self._mods: List[Mod] = []
      self._mods_by_id: Dict[str, Mod] = {}
      self._deps: Dict[str, List[str]] = {
         mid: list(dep_ids)
         for mid, dep_ids in self._prefs.user_dependencies.get(game_id, {}).items()
      }

      self._build_ui()
      self._load_mods()
      self._populate_mod_list()

   # ── UI construction ───────────────────────────────────────────────────────

   def _build_ui(self) -> None:
      # Left: mods
      self._mod_search = QLineEdit()
      self._mod_search.setPlaceholderText("Search mods…")
      self._mod_search.textChanged.connect(self._filter_mod_list)

      self._mod_list = QListWidget()
      self._mod_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
      self._mod_list.currentRowChanged.connect(self._on_mod_selected)

      # Right: dependencies for selected mod
      self._dep_list = QListWidget()
      self._dep_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

      self._add_dep_btn = QPushButton("Add dependency…")
      self._remove_dep_btn = QPushButton("Remove selected")
      self._add_dep_btn.clicked.connect(self._on_add_dependency)
      self._remove_dep_btn.clicked.connect(self._on_remove_dependency)

      right_buttons = QHBoxLayout()
      right_buttons.addWidget(self._add_dep_btn)
      right_buttons.addWidget(self._remove_dep_btn)
      right_buttons.addStretch()

      right_panel = QVBoxLayout()
      right_panel.addWidget(QLabel("Dependencies for selected mod:"))
      right_panel.addWidget(self._dep_list)
      right_panel.addLayout(right_buttons)

      left_panel = QVBoxLayout()
      left_panel.addWidget(QLabel("Mods in this game:"))
      left_panel.addWidget(self._mod_search)
      left_panel.addWidget(self._mod_list)

      columns = QHBoxLayout()
      columns.addLayout(left_panel, stretch=1)
      columns.addLayout(right_panel, stretch=1)

      # Buttons
      btns = QDialogButtonBox(
         QDialogButtonBox.StandardButton.Ok
         | QDialogButtonBox.StandardButton.Cancel  # type: ignore[arg-type]
      )
      btns.accepted.connect(self._on_accept)
      btns.rejected.connect(self.reject)

      root = QVBoxLayout(self)
      root.addLayout(columns)
      root.addWidget(btns)

   # ── data loading ──────────────────────────────────────────────────────────

   def _load_mods(self) -> None:
      game = games.get_game(self._game_id)
      if not game:
         show_warning(self, "No game", "Selected game no longer exists.")
         return
      mod_dir = games.get_mod_dir(game, self._prefs.game_paths)
      if not mod_dir or not mod_dir.exists():
         show_warning(
            self,
            "Mod directory not found",
            f"Mod directory for {game.display_name} not found:\n{mod_dir}\n\n"
            "Check Settings → Game user-data paths.",
         )
         return

      self._mods = parser.discover_mods(mod_dir)
      self._mods_by_id = {m.id: m for m in self._mods}

      # Make sure we don't keep stale dependencies to unknown IDs.
      valid_ids = set(self._mods_by_id.keys())
      cleaned: Dict[str, List[str]] = {
         mid: [d for d in dep_ids if d in valid_ids]
         for mid, dep_ids in self._deps.items() if mid in valid_ids
      }
      self._deps = cleaned

   def _populate_mod_list(self) -> None:
      self._mod_list.clear()
      sorted_mods = sorted(self._mods, key=lambda m: m.name.lower())
      id_to_row: Dict[str, int] = {}
      for row, mod in enumerate(sorted_mods):
         item = QListWidgetItem(mod.name)
         item.setData(Qt.ItemDataRole.UserRole, mod.id)
         self._mod_list.addItem(item)
         id_to_row[mod.id] = row

      # Pre-select the requested mod if provided; otherwise first row.
      if self._initial_mod_id and self._initial_mod_id in id_to_row:
         self._mod_list.setCurrentRow(id_to_row[self._initial_mod_id])
      elif self._mod_list.count() > 0:
         self._mod_list.setCurrentRow(0)

   def _filter_mod_list(self, text: str) -> None:
      """Filter the left mod list by partial-word query."""
      _filter_list_widget(self._mod_list, text)

   # ── mod selection / dependency view ───────────────────────────────────────

   def _on_mod_selected(self, row: int) -> None:
      """Refresh the dependency list for the currently selected mod."""
      self._dep_list.clear()
      if row < 0:
         return
      item = self._mod_list.item(row)
      if item is None:
         return
      mod_id = item.data(Qt.ItemDataRole.UserRole)
      if not mod_id:
         return

      mod = self._mods_by_id.get(mod_id)
      if mod is None:
         return

      # 1) Dependencies from descriptor.mod (by NAME; map to IDs if possible)
      descriptor_dep_ids: list[str] = []
      if mod.dependencies:
         # Mod.dependencies from parser are names; map to installed mods by name.
         name_to_mod = {m.name: m for m in self._mods}
         for name in mod.dependencies:
            if dep_mod := name_to_mod.get(name):
               descriptor_dep_ids.append(dep_mod.id)

      # 2) User-defined dependencies stored in settings (already by ID).
      user_dep_ids = self._deps.get(mod_id, [])

      # 3) Union for display; keep stable order: descriptor first, then user extras.
      seen: set[str] = set()
      combined_ids: list[str] = []
      for did in descriptor_dep_ids + user_dep_ids:
         if did in self._mods_by_id and did not in seen:
            seen.add(did)
            combined_ids.append(did)

      for dep_id in combined_ids:
         dep_mod = self._mods_by_id.get(dep_id)
         if not dep_mod:
            continue
         dep_item = QListWidgetItem(dep_mod.name)
         dep_item.setData(Qt.ItemDataRole.UserRole, dep_mod.id)
         self._dep_list.addItem(dep_item)

   # ── add / remove dependencies ─────────────────────────────────────────────

   def _on_add_dependency(self) -> None:
      current = self._mod_list.currentItem()
      if current is None:
         return
      mod_id = current.data(Qt.ItemDataRole.UserRole)
      if not mod_id:
         return

      # Build a chooser list excluding self and already-added deps.
      existing = set(self._deps.get(mod_id, []))
      candidates = [
         m for m in self._mods
         if m.id != mod_id and m.id not in existing
      ]
      if not candidates:
         QMessageBox.information(
            self,
            "No candidates",
            "There are no additional mods that can be added as dependencies.",
         )
         return

      dlg = _DependencyPickerDialog(candidates, parent=self)
      if dlg.exec() != QDialog.DialogCode.Accepted:
         return
      chosen_ids = dlg.chosen_ids()
      if not chosen_ids:
         return

      deps = self._deps.setdefault(mod_id, [])
      for cid in chosen_ids:
         if cid not in deps:
            deps.append(cid)

      self._on_mod_selected(self._mod_list.currentRow())

   def _on_remove_dependency(self) -> None:
      current_mod = self._mod_list.currentItem()
      selected_dep = self._dep_list.currentItem()
      if current_mod is None or selected_dep is None:
         return

      mod_id = current_mod.data(Qt.ItemDataRole.UserRole)
      dep_id = selected_dep.data(Qt.ItemDataRole.UserRole)
      if not mod_id or not dep_id:
         return

      deps = self._deps.get(mod_id)
      if not deps:
         return
      self._deps[mod_id] = [d for d in deps if d != dep_id]
      self._on_mod_selected(self._mod_list.currentRow())

   # ── accept / persist ──────────────────────────────────────────────────────

   def _on_accept(self) -> None:
      # Save back into prefs.user_dependencies for this game_id.
      if "user_dependencies" not in self._prefs.__dict__:
         self._prefs.user_dependencies = {}
      self._prefs.user_dependencies[self._game_id] = {
         mid: deps for mid, deps in self._deps.items() if deps
      }
      self.accept()


class _DependencyPickerDialog(QDialog):
   """
   Simple dialog to choose one or more dependency mods from a list.
   """

   def __init__(self, candidates: List[Mod], parent: QWidget | None = None) -> None:
      super().__init__(parent)
      self.setWindowTitle("Add dependency")
      self.resize(400, 300)
      self._candidates = candidates

      self._search = QLineEdit()
      self._search.setPlaceholderText("Search mods…")
      self._search.textChanged.connect(self._filter_list)

      self._list = QListWidget()
      self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
      for mod in sorted(self._candidates, key=lambda m: m.name.lower()):
         item = QListWidgetItem(mod.name)
         item.setData(Qt.ItemDataRole.UserRole, mod.id)
         self._list.addItem(item)

      btns = QDialogButtonBox(
         QDialogButtonBox.StandardButton.Ok
         | QDialogButtonBox.StandardButton.Cancel  # type: ignore[arg-type]
      )
      btns.accepted.connect(self.accept)
      btns.rejected.connect(self.reject)

      layout = QVBoxLayout(self)
      layout.addWidget(QLabel("Select dependency mods:"))
      layout.addWidget(self._search)
      layout.addWidget(self._list)
      layout.addWidget(btns)

   def _filter_list(self, text: str) -> None:
      """Filter the candidate list by partial-word query."""
      _filter_list_widget(self._list, text)

   def chosen_ids(self) -> List[str]:
      ids: List[str] = []
      for item in self._list.selectedItems():
         if mid := item.data(Qt.ItemDataRole.UserRole):
            ids.append(mid)
      return ids
