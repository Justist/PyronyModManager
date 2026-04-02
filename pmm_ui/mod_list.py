from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import (
   QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
   QListWidget, QListWidgetItem, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from pmm_models import Mod


# ── Multi-item drag-drop list ─────────────────────────────────────────────────

class _MultiDragList(QListWidget):
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
      self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
      self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
      self.setDefaultDropAction(Qt.DropAction.MoveAction)
      self.setDragEnabled(True)
      self.setAcceptDrops(True)

   def dropEvent(self, event: QDropEvent) -> None:  # type: ignore[override]
      selected_rows = sorted(self.row(it) for it in self.selectedItems())
      if not selected_rows:
         event.ignore()
         return

      # Snapshot item data before any removal
      snapshots = [_snapshot(self.item(r)) for r in selected_rows]

      # Determine insertion row from the drop position
      target = self._target_row(event.position().toPoint())

      # Remove from bottom to top so indices stay valid
      for row in reversed(selected_rows):
         self.takeItem(row)

      # The target row shifts down by the number of removed rows above it
      rows_above = sum(1 for r in selected_rows if r < target)
      insert_at = max(0, min(target - rows_above, self.count()))

      # Re-insert in original relative order and re-select them
      for offset, snap in enumerate(snapshots):
         new_item = _restore(snap)
         self.insertItem(insert_at + offset, new_item)
         new_item.setSelected(True)

      event.setDropAction(Qt.DropAction.IgnoreAction)
      event.accept()
      self.items_reordered.emit()

   def _target_row(self, pos) -> int:
      """Return the insertion row for a drop at pixel position `pos`."""
      index = self.indexAt(pos)
      if not index.isValid():
         return self.count()
      rect = self.visualRect(index)
      return index.row() + (1 if pos.y() >= rect.center().y() else 0)


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

   def load_mods(self, mods: list[Mod], ordered_ids: list[str]) -> None:
      self._mods = {m.id: m for m in mods}
      active_set = set(ordered_ids)

      self._loading = True

      self._active.clear()
      self._avail.clear()

      # fill active and available in a single pass
      for mod in mods:
         if mod.id in active_set:
            self._active.addItem(_make_active_item(mod, checked=True))
         else:
            self._avail.addTopLevelItem(_make_avail_item(mod, checked=False))

      self._loading = False
      # apply filter once (updates labels as a side effect)
      self._apply_filter(self._search.text())

      # After population, lock the Supported column width and let Mod stretch
      header = self._avail.header()
      supported_width = header.sectionSize(1)
      header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
      header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
      header.resizeSection(1, supported_width)

   def current_order(self) -> list[str]:
      return [
         self._active.item(i).data(Qt.ItemDataRole.UserRole)
         for i in range(self._active.count())
      ]

   def set_active_enabled(self, enabled: bool) -> None:
      self._collection_active = enabled
      for w in (self._avail, self._search, self._active,
                self._btn_up, self._btn_down):
         w.setEnabled(enabled)

   # ── itemChanged handlers ──────────────────────────────────────────────────

   def _on_avail_changed(self, item) -> None:
      """Checking in the available list moves item(s) to the active playset."""
      # item is a QTreeWidgetItem
      if self._loading or not self._collection_active:
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
         mid = it.data(0, Qt.ItemDataRole.UserRole)
         if mid not in self._mods:
            continue
         idx = self._avail.indexOfTopLevelItem(it)
         if idx >= 0:
            self._avail.takeTopLevelItem(idx)
         self._active.addItem(_make_active_item(self._mods[mid], checked=True))

      self._update_labels()
      self._emit_order()

   def _on_active_changed(self, item: QListWidgetItem) -> None:
      """Unchecking in the active playset moves the item back to available."""
      if self._loading:
         return
      if item.checkState() != Qt.CheckState.Unchecked:
         return

      mid = item.data(Qt.ItemDataRole.UserRole)
      if mid not in self._mods:
         return

      row = self._active.row(item)
      if row >= 0:
         self._active.takeItem(row)
      self._avail.addTopLevelItem(_make_avail_item(self._mods[mid], checked=False))

      self._update_labels()
      self._emit_order()

   # ── ▲/▼ reorder — multi-selection aware ──────────────────────────────────

   def _move_up(self) -> None:
      rows = sorted(self._active.row(it) for it in self._active.selectedItems())
      if not rows or rows[0] == 0:
         return
      self._loading = True
      for row in rows:  # top-to-bottom: each moves up by 1
         item = self._active.takeItem(row)
         self._active.insertItem(row - 1, item)
         self._active.item(row - 1).setSelected(True)
      self._loading = False
      self._emit_order()

   def _move_down(self) -> None:
      rows = sorted(
         (self._active.row(it) for it in self._active.selectedItems()),
         reverse=True,
      )
      if not rows or rows[0] == self._active.count() - 1:
         return
      self._loading = True
      for row in rows:  # bottom-to-top: each moves down by 1
         item = self._active.takeItem(row)
         self._active.insertItem(row + 1, item)
         self._active.item(row + 1).setSelected(True)
      self._loading = False
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

def _make_active_item(mod: Mod, checked: bool) -> QListWidgetItem:
   """
   Create a QListWidgetItem for the active-playset list (single column).

   The label still includes the supported version for quick reference.
   """
   label = f"{mod.name}  [{mod.supported_version}]" if mod.supported_version else mod.name
   item = QListWidgetItem(label)
   item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
   item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
   item.setData(Qt.ItemDataRole.UserRole, mod.id)
   item.setToolTip(
      f"ID: {mod.id}\n"
      f"Version: {mod.version or '—'}\n"
      f"Supported: {mod.supported_version or '—'}\n"
      f"Path: {mod.path}"
   )
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
   item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
   item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
   item.setData(0, Qt.ItemDataRole.UserRole, mod.id)
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


def _snapshot(item: QListWidgetItem) -> dict:
   """Capture all displayable state of an item before takeItem() destroys it."""
   return {
      "text": item.text(),
      "data": item.data(Qt.ItemDataRole.UserRole),
      "flags": item.flags(),
      "check": item.checkState(),
      "tooltip": item.toolTip(),
   }


def _restore(snap: dict) -> QListWidgetItem:
   """Reconstruct a QListWidgetItem from a snapshot."""
   item = QListWidgetItem(snap["text"])
   item.setFlags(snap["flags"])
   item.setCheckState(snap["check"])
   item.setData(Qt.ItemDataRole.UserRole, snap["data"])
   item.setToolTip(snap["tooltip"])
   return item
