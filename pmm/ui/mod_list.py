from typing import Dict, List

from PySide6.QtCore import QPoint, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDropEvent
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMenu, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from pmm.core.models import Mod


# ── Multi-item drag-drop tree (active list) ───────────────────────────────────

class _MultiDragList(QTreeWidget):
   """
   QListWidget subclass that supports dragging multiple selected items at once
   while preserving their relative order.

   Qt's built-in InternalMove drops items one by one, which scrambles order
   when more than one row is selected.  This subclass intercepts dropEvent,
   collects all selected rows sorted by position, removes them, then
   re-inserts them contiguously at the computed target row.
   """

   items_reordered = Signal()

   def __init__(self, parent=None) -> None:
      super().__init__(parent)
      self.setColumnCount(2)
      # Column 0: tiny index column, fixed minimal width
      # Column 1: main mod label, stretches
      header = self.header()
      header.setStretchLastSection(False)
      header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
      header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
      header.resizeSection(0, 28)  # tiny index column
      header.setVisible(False)  # hide header; we just want the column

      self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
      self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
      self.setDefaultDropAction(Qt.DropAction.MoveAction)
      self.setDragEnabled(True)
      self.setAcceptDrops(True)
      self.setRootIsDecorated(False)

   def dropEvent(self, event: QDropEvent) -> None:
      selected_rows = sorted(
         self.indexOfTopLevelItem(it) for it in self.selectedItems()
      )
      if not selected_rows:
         event.ignore()
         return

      snapshots = []
      for r in selected_rows:
         tlir = self.topLevelItem(r)
         if tlir is not None:
            snapshots.append(_snapshot(tlir))

      target = self._target_row(event.position().toPoint())

      for row in reversed(selected_rows):
         self.takeTopLevelItem(row)

      rows_above = sum(r < target for r in selected_rows)
      insert_at = max(0, min(target - rows_above, self.topLevelItemCount()))

      for offset, snap in enumerate(snapshots):
         new_item = _restore(snap)
         self.insertTopLevelItem(insert_at + offset, new_item)
         new_item.setSelected(True)

      event.setDropAction(Qt.DropAction.IgnoreAction)
      event.accept()
      self.renumber()
      self.items_reordered.emit()

   def _target_row(self, pos) -> int:
      """Return the insertion row for a drop at pixel position `pos`."""
      index = self.indexAt(pos)
      if not index.isValid():
         return self.topLevelItemCount()
      rect = self.visualRect(index)
      return index.row() + (1 if pos.y() >= rect.center().y() else 0)

   def renumber(self) -> None:
      """Update the left index column to show 1-based load order."""
      for row in range(self.topLevelItemCount()):
         it = self.topLevelItem(row)
         if it is not None:
            it.setText(0, str(row + 1))


# ── ModListWidget ─────────────────────────────────────────────────────────────

class ModListWidget(QWidget):
   """
   Dual-list mod selector driven by checkboxes.

   Left  – all mods on disk, unchecked.  Checking one (or all currently
           selected items when one of them is checked) moves them into the
           active playset on the right.
   Right – active playset in load order, all checked.  Unchecking one moves
           it back to the left.  Multiple items can be dragged together
           (preserving relative order) or moved with ▲/▼.

   Both panels are disabled when no collection is active; call
   set_active_enabled(bool) to switch.  The signal order_changed(list[str])
   fires whenever the active list changes, so main_window.py is unaffected.
   """

   order_changed: Signal = Signal(list)

   def __init__(self, parent: QWidget | None = None) -> None:
      super().__init__(parent)
      self._mods: dict[str, Mod] = {}
      self._loading: bool = False
      self._collection_active: bool = False

      # ── left: available ──────────────────────────────────────────────────
      self._avail_label = QLabel("Available mods")
      self._avail_label.setStyleSheet("font-weight: bold;")

      self._search = QLineEdit()
      self._search.setPlaceholderText("Filter available mods…")
      self._search.textChanged.connect(self._apply_filter)

      self._avail = QTreeWidget()
      self._avail.setColumnCount(2)
      self._avail.setHeaderLabels(["Mod", "Supported"])
      self._avail.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
      self._avail.setSortingEnabled(True)
      self._avail.itemChanged.connect(self._on_avail_changed)
      self._avail.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
      self._avail.customContextMenuRequested.connect(
         lambda pos: self._show_context_menu(self._avail, pos)
      )
      # sort by Supported version column by default
      self._avail.sortItems(1, Qt.SortOrder.DescendingOrder)

      # Column sizing: give "Supported" only the space it needs, let "Mod" take the rest
      header = self._avail.header()
      header.setStretchLastSection(False)
      # initial size based on contents; will be refined after items are added
      header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
      header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)

      left_layout = QVBoxLayout()
      left_layout.setContentsMargins(0, 0, 0, 0)
      left_layout.addWidget(self._avail_label)
      left_layout.addWidget(self._search)
      left_layout.addWidget(self._avail)

      # ── right: active playset ────────────────────────────────────────────
      self._active_label = QLabel("Active playset")
      self._active_label.setStyleSheet("font-weight: bold;")

      self._active = _MultiDragList()
      self._active.items_reordered.connect(self._emit_order)
      self._active.itemChanged.connect(self._on_active_changed)
      self._active.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
      self._active.customContextMenuRequested.connect(
         lambda pos: self._show_context_menu(self._active, pos)
      )

      self._btn_up = QPushButton("▲ Up")
      self._btn_down = QPushButton("▼ Down")
      self._btn_up.setFixedWidth(80)
      self._btn_down.setFixedWidth(80)
      self._btn_up.clicked.connect(self._move_up)
      self._btn_down.clicked.connect(self._move_down)

      order_row = QHBoxLayout()
      order_row.setContentsMargins(0, 0, 0, 0)
      order_row.addWidget(self._btn_up)
      order_row.addWidget(self._btn_down)
      order_row.addStretch()

      right_layout = QVBoxLayout()
      right_layout.setContentsMargins(0, 0, 0, 0)
      right_layout.addWidget(self._active_label)
      right_layout.addWidget(self._active)
      right_layout.addLayout(order_row)

      # ── root layout ──────────────────────────────────────────────────────
      root = QHBoxLayout(self)
      root.setContentsMargins(0, 0, 0, 0)
      root.addLayout(left_layout, stretch=1)
      root.addLayout(right_layout, stretch=1)

      self.set_active_enabled(False)

   # ── public API ────────────────────────────────────────────────────────────

   def load_mods(self, mods: List[Mod], ordered_ids: List[str]) -> None:
      self._mods = {m.id: m for m in mods}
      active_set = set(ordered_ids)

      self._loading = True

      self._active.clear()
      self._avail.clear()

      # fill active and available in a single pass
      for mod in mods:
         if mod.id in active_set:
            self._active.addTopLevelItem(_make_active_item(mod, checked=True))
         else:
            self._avail.addTopLevelItem(_make_avail_item(mod, checked=False))

      self._loading = False
      # apply filter once (updates labels as a side effect)
      self._apply_filter(self._search.text())

      # Renumber active items after initial load
      self._active.renumber()

      # After population, lock the Supported column width and let Mod stretch
      header = self._avail.header()
      supported_width = header.sectionSize(1)
      header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
      header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
      header.resizeSection(1, supported_width)

   def current_order(self) -> List[str]:
      return_list: List[str] = []
      for i in range(self._active.topLevelItemCount()):
         if (tlii := self._active.topLevelItem(i)) is not None:
            mod: Mod | None = tlii.data(1, Qt.ItemDataRole.UserRole)
            if isinstance(mod, Mod):
               return_list.append(mod.id)
      return return_list

   def set_active_enabled(self, enabled: bool) -> None:
      self._collection_active = enabled
      for w in (self._avail, self._search, self._active,
                self._btn_up, self._btn_down):
         w.setEnabled(enabled)

   # ── itemChanged handlers ──────────────────────────────────────────────────

   def _on_avail_changed(self, item: QTreeWidgetItem, column: int) -> None:
      """Checking in the available list moves item(s) to the active playset."""
      # item is a QTreeWidgetItem
      if self._loading or not self._collection_active:
         return
      if column != 0:
         return
      if item.checkState(0) != Qt.CheckState.Checked:
         return

      selected = self._avail.selectedItems()
      to_move = list(selected) if item in selected else [item]

      self._loading = True
      for it in to_move:
         it.setCheckState(0, Qt.CheckState.Checked)
      self._loading = False

      for it in to_move:
         mod: Mod | None = it.data(0, Qt.ItemDataRole.UserRole)
         mid = mod.id if isinstance(mod, Mod) else None
         if not mid or mid not in self._mods:
            continue
         idx = self._avail.indexOfTopLevelItem(it)
         if idx >= 0:
            self._avail.takeTopLevelItem(idx)
         self._active.addTopLevelItem(
            _make_active_item(self._mods[mid], checked=True)
         )

      self._sync_after_active_change()

   def _on_active_changed(self, item: QTreeWidgetItem, column: int) -> None:
      """Unchecking in the active playset moves the item back to available."""
      if self._loading:
         return
      if column != 1:
         return
      if item.checkState(1) != Qt.CheckState.Unchecked:
         return

      mod: Mod | None = item.data(1, Qt.ItemDataRole.UserRole)
      mid = mod.id if isinstance(mod, Mod) else None
      if not mid or mid not in self._mods:
         return

      row = self._active.indexOfTopLevelItem(item)
      if row >= 0:
         self._active.takeTopLevelItem(row)
      self._avail.addTopLevelItem(_make_avail_item(self._mods[mid], checked=False))

      self._sync_after_active_change()

   def _sync_after_active_change(self) -> None:
      """Common post-change logic for active/available list updates."""
      self._active.renumber()
      self._update_labels()
      self._emit_order()

   # ── ▲/▼ reorder — multi-selection aware ──────────────────────────────────

   def _move_up(self) -> None:
      rows = sorted(
         self._active.indexOfTopLevelItem(it)
         for it in self._active.selectedItems()
      )
      if not rows or rows[0] == 0:
         return
      self._loading = True
      for row in rows:  # top-to-bottom: each moves up by 1
         item = self._active.takeTopLevelItem(row)
         if item is None:
            continue
         self._active.insertTopLevelItem(row - 1, item)
         item.setSelected(True)
      self._finalize_move()

   def _move_down(self) -> None:
      rows = sorted(
         (self._active.indexOfTopLevelItem(it)
          for it in self._active.selectedItems()),
         reverse=True,
      )
      if not rows or rows[0] == self._active.topLevelItemCount() - 1:
         return
      self._loading = True
      for row in rows:  # bottom-to-top: each moves down by 1
         item = self._active.takeTopLevelItem(row)
         if item is None:
            continue
         self._active.insertTopLevelItem(row + 1, item)
         item.setSelected(True)
      self._finalize_move()

   def _finalize_move(self) -> None:
      """Shared tail for _move_up/_move_down after reordering items."""
      self._loading = False
      self._active.renumber()
      self._emit_order()

   # ── filter ────────────────────────────────────────────────────────────────

   def _apply_filter(self, text: str) -> None:
      """Filter available mods by case-insensitive substring match on the name."""
      needle = text.strip().lower()
      count = self._avail.topLevelItemCount()
      for i in range(count):
         it = self._avail.topLevelItem(i)
         if it is None:
            continue
         if not needle:
            it.setHidden(False)
         else:
            it.setHidden(needle not in it.text(0).lower())
      self._update_labels()

   # ── helpers ───────────────────────────────────────────────────────────────

   def _emit_order(self, *_) -> None:
      self.order_changed.emit(self.current_order())

   def _show_context_menu(self, tree: QTreeWidget, pos: QPoint) -> None:
      """Show context menu for the item at the given position."""
      item = tree.itemAt(pos)
      if item is None:
         return

      # Determine which column carries the Mod object.
      col = 0 if tree is self._avail else 1
      mod: Mod | None = item.data(col, Qt.ItemDataRole.UserRole)
      if not isinstance(mod, Mod):
         return

      menu = QMenu(self)
      open_folder = menu.addAction("Open in File Explorer")
      open_workshop = menu.addAction("Open in Steam Workshop")
      if not mod.remote_id:
         open_workshop.setEnabled(False)

      action = menu.exec(tree.viewport().mapToGlobal(pos))
      if action is open_folder:
         self._open_mod_folder(mod)
      elif action is open_workshop and mod.remote_id:
         self._open_mod_workshop(mod)

   def _open_mod_folder(self, mod: Mod) -> None:
      """Open the mod's folder in the system file manager."""
      path = mod.path
      if not path:
         return
      url = QUrl.fromLocalFile(str(path))
      QDesktopServices.openUrl(url)

   def _open_mod_workshop(self, mod: Mod) -> None:
      """Open the mod's Steam Workshop page in the Steam client, if possible."""
      if not mod.remote_id:
         return
      # Use Steam URI so the Steam client handles it.
      url = QUrl(f"steam://url/CommunityFilePage/{mod.remote_id}")
      QDesktopServices.openUrl(url)

   def _update_labels(self) -> None:
      """Update the available-mods label with the number of visible entries."""
      visible = 0
      for i in range(self._avail.topLevelItemCount()):
         it = self._avail.topLevelItem(i)
         if it is not None and not it.isHidden():
            visible += 1

      # "Available mods (N)" – keep base label text stable for translations later.
      self._avail_label.setText(f"Available mods ({visible})")


# ── item helpers ──────────────────────────────────────────────────────────────

def _make_active_item(mod: Mod, checked: bool) -> QTreeWidgetItem:
   """
   Create a QTreeWidgetItem for the active-playset tree (two columns):
     column 0: order number (1-based; filled/updated by renumber)
     column 1: mod label with checkbox
   """
   label = f"{mod.name}  [{mod.supported_version}]" if mod.supported_version else mod.name
   item = QTreeWidgetItem(["", label])
   item.setFlags(
      item.flags()
      | Qt.ItemFlag.ItemIsUserCheckable
      | Qt.ItemFlag.ItemIsSelectable
      | Qt.ItemFlag.ItemIsEnabled
   )
   item.setCheckState(1, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
   # Store full Mod object for context-menu actions; id is still derivable.
   item.setData(1, Qt.ItemDataRole.UserRole, mod)
   tooltip = (
      f"ID: {mod.id}\n"
      f"Version: {mod.version or '—'}\n"
      f"Supported: {mod.supported_version or '—'}\n"
      f"Path: {mod.path}"
   )
   item.setToolTip(0, tooltip)
   item.setToolTip(1, tooltip)
   return item


def _make_avail_item(mod: Mod, checked: bool) -> QTreeWidgetItem:
   """
   Create a QTreeWidgetItem for the available-mods list (two columns):
     column 0: mod name (with checkbox)
     column 1: supported version (or empty string)
   """
   version = mod.supported_version or ""
   item = QTreeWidgetItem([mod.name, version])
   # Enable user checkability only on the first column
   item.setFlags(
      item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable |
      Qt.ItemFlag.ItemIsEnabled)
   item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
   # Store full Mod object for context-menu actions.
   item.setData(0, Qt.ItemDataRole.UserRole, mod)
   item.setToolTip(
      0,
      f"ID: {mod.id}\n"
      f"Version: {mod.version or '—'}\n"
      f"Supported: {mod.supported_version or '—'}\n"
      f"Path: {mod.path}"
   )
   # Mirror tooltip on second column for convenience
   item.setToolTip(1, item.toolTip(0))
   return item


def _snapshot(item: QTreeWidgetItem) -> Dict:
   """Capture all displayable state of an item before takeTopLevelItem() destroys it."""
   return {
      "texts": [item.text(0), item.text(1)],
      "data": item.data(1, Qt.ItemDataRole.UserRole),
      "flags": item.flags(),
      "check": item.checkState(1),
      "tooltips": [item.toolTip(0), item.toolTip(1)],
   }


def _restore(snap: Dict) -> QTreeWidgetItem:
   """Reconstruct a QTreeWidgetItem from a snapshot."""
   item = QTreeWidgetItem(snap["texts"])
   item.setFlags(snap["flags"])
   item.setCheckState(1, snap["check"])
   item.setData(1, Qt.ItemDataRole.UserRole, snap["data"])
   item.setToolTip(0, snap["tooltips"][0])
   item.setToolTip(1, snap["tooltips"][1])
   return item
