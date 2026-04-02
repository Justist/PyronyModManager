from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
   QLabel, QPushButton, QProgressBar, QSplitter, QTabWidget, QTextEdit,
   QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import pmm_services
from pmm_models import Mod
from pmm_services import DefinitionDiff


@dataclass(frozen=True)
class _FileNodeData:
   kind: str  # "file"
   rel_path: str
   owners: list[Mod]


@dataclass(frozen=True)
class _ModNodeData:
   kind: str  # "mod"
   rel_path: str
   mod: Mod


class ConflictView(QWidget):
   def __init__(self, parent: QWidget | None = None) -> None:
      super().__init__(parent)

      # ── left panel: conflict tree ────────────────────────────────────────
      scan_btn = QPushButton("🔄  Scan for conflicts")
      scan_btn.clicked.connect(self._scan)

      # tiny loading bar shown while scanning for conflicts
      self._progress = QProgressBar()
      self._progress.setMaximumHeight(10)
      self._progress.setRange(0, 0)  # indeterminate
      self._progress.hide()

      self._tree = QTreeWidget()
      self._tree.setHeaderLabels(["File / Mod", "Info"])
      self._tree.setColumnWidth(0, 360)
      self._tree.itemSelectionChanged.connect(self._on_selection)

      left = QWidget()
      ll = QVBoxLayout(left)
      ll.setContentsMargins(0, 0, 4, 0)
      ll.addWidget(scan_btn)
      ll.addWidget(self._progress)
      ll.addWidget(self._tree)

      # ── right panel: tabbed diff views ───────────────────────────────────
      self._right_placeholder = QLabel("Select a conflicting file to see the diff.")
      self._right_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
      self._right_placeholder.setStyleSheet("color: #888;")

      self._diff_tabs = QTabWidget()
      self._diff_tabs.hide()

      right = QWidget()
      rl = QVBoxLayout(right)
      rl.setContentsMargins(4, 0, 0, 0)
      rl.addWidget(self._right_placeholder)
      rl.addWidget(self._diff_tabs)

      # ── splitter ─────────────────────────────────────────────────────────
      splitter = QSplitter(Qt.Orientation.Horizontal)
      splitter.addWidget(left)
      splitter.addWidget(right)
      splitter.setStretchFactor(0, 2)
      splitter.setStretchFactor(1, 3)

      root_layout = QVBoxLayout(self)
      root_layout.setContentsMargins(0, 0, 0, 0)
      root_layout.addWidget(splitter)

      self._mods: list[Mod] = []
      self._conflicts: dict[str, list[Mod]] = {}

   # ── public ───────────────────────────────────────────────────────────────

   def set_mods(self, mods: list[Mod]) -> None:
      self._mods = mods

   # ── scan ─────────────────────────────────────────────────────────────────

   def _scan(self) -> None:
      self._tree.clear()
      self._clear_diff_panel()

      # show tiny loading bar while scanning
      self._progress.show()
      self._progress.repaint()  # ensure immediate visual update

      try:
         self._conflicts = pmm_services.detect_file_conflicts(self._mods)
      finally:
         self._progress.hide()

      if not self._conflicts:
         QTreeWidgetItem(self._tree, ["✓  No conflicts found", ""])
         return

      for rel_path, owners in sorted(self._conflicts.items()):
         top = QTreeWidgetItem([rel_path, f"{len(owners)} mods"])
         top.setForeground(0, Qt.GlobalColor.red)
         top.setData(0, Qt.ItemDataRole.UserRole, _FileNodeData("file", rel_path, owners))
         for mod in owners:
            child = QTreeWidgetItem([f"  {mod.name}", ""])
            child.setData(0, Qt.ItemDataRole.UserRole, _ModNodeData("mod", rel_path, mod))
            top.addChild(child)
         self._tree.addTopLevelItem(top)

      self._tree.expandAll()

   # ── selection ─────────────────────────────────────────────────────────────

   def _on_selection(self) -> None:
      items = self._tree.selectedItems()
      if not items:
         return

      data = items[0].data(0, Qt.ItemDataRole.UserRole)
      if data is None:
         return

      # Typed node payloads improve readability over raw tuples.
      if isinstance(data, _FileNodeData) and data.kind == "file":
         self._show_diff_for_owners(data.rel_path, data.owners)
      elif isinstance(data, _ModNodeData) and data.kind == "mod":
         owners = self._conflicts.get(data.rel_path, [])
         clicked = data.mod
         others = [m for m in owners if m is not clicked]
         if others:
            # For now show the first other mod vs the clicked one.
            self._show_diff_for_owners(data.rel_path, [others[0], clicked])

   # ── diff panel ────────────────────────────────────────────────────────────

   def _show_diff_for_owners(self, rel_path: str, owners: list[Mod]) -> None:
      """Build one diff tab per adjacent pair of owners."""
      self._clear_diff_panel()
      for mod_a, mod_b in zip(owners, owners[1:]):
         tab = _DiffTab(rel_path, mod_a, mod_b)
         a_label = mod_a.name[:14] + ("…" if len(mod_a.name) > 14 else "")
         b_label = mod_b.name[:14] + ("…" if len(mod_b.name) > 14 else "")
         self._diff_tabs.addTab(tab, f"{a_label} → {b_label}")

      self._right_placeholder.hide()
      self._diff_tabs.show()

   def _clear_diff_panel(self) -> None:
      while self._diff_tabs.count():
         w = self._diff_tabs.widget(0)
         self._diff_tabs.removeTab(0)
         if w:
            w.deleteLater()
      self._diff_tabs.hide()
      self._right_placeholder.show()


# ── _DiffTab ──────────────────────────────────────────────────────────────────

class _DiffTab(QWidget):
   """
   Per-mod-pair tab with two sub-panels:
     Top    – structural (definition-level) diff tree from pmm_clausewitz
     Bottom – raw unified diff, or definition diff when a row is selected
   """

   # status → human label and color; reused across instances
   _STATUS_LABEL = {
      "changed": "changed",
      "only_in_a": "only in {a}",
      "only_in_b": "only in {b}",
   }
   _STATUS_COLOR = {
      "changed": QColor("#e0a020"),
      "only_in_a": QColor("#e07070"),
      "only_in_b": QColor("#70b870"),
   }

   def __init__(self, rel_path: str, mod_a: Mod, mod_b: Mod, parent=None) -> None:
      super().__init__(parent)
      self._rel_path = rel_path
      self._mod_a = mod_a
      self._mod_b = mod_b

      mono = QFont("Courier New", 9)

      # structural diff tree
      self._def_tree = QTreeWidget()
      self._def_tree.setHeaderLabels(["Definition", "Status"])
      self._def_tree.setColumnWidth(0, 280)
      self._def_tree.itemSelectionChanged.connect(self._on_def_selected)

      # bottom text area — shows full-file diff by default,
      # switches to definition diff when a row is selected
      self._unified_edit = QTextEdit()
      self._unified_edit.setReadOnly(True)
      self._unified_edit.setFont(mono)

      self._def_diff_edit = QTextEdit()
      self._def_diff_edit.setReadOnly(True)
      self._def_diff_edit.setFont(mono)
      self._def_diff_edit.hide()

      self._bottom_tabs = QTabWidget()
      self._bottom_tabs.addTab(self._unified_edit, "Unified diff (full file)")
      self._bottom_tabs.addTab(self._def_diff_edit, "Definition diff")

      vsplit = QSplitter(Qt.Orientation.Vertical)
      vsplit.addWidget(self._def_tree)
      vsplit.addWidget(self._bottom_tabs)
      vsplit.setStretchFactor(0, 1)
      vsplit.setStretchFactor(1, 2)

      layout = QVBoxLayout(self)
      layout.setContentsMargins(0, 0, 0, 0)
      layout.addWidget(vsplit)

      self._populate()

   def _populate(self) -> None:
      """Fill the definition tree and unified diff for this file/mod pair."""
      diffs = pmm_services.get_definition_diffs(
         self._rel_path, self._mod_a, self._mod_b
      )

      if diffs:
         a_name = self._mod_a.name
         b_name = self._mod_b.name
         for dd in diffs:
            label_template = self._STATUS_LABEL.get(dd.status, dd.status)
            status_label = label_template.format(a=a_name, b=b_name)
            item = QTreeWidgetItem([dd.def_id, status_label])
            item.setForeground(1, self._STATUS_COLOR.get(dd.status, QColor("#aaa")))
            item.setData(0, Qt.ItemDataRole.UserRole, dd)
            self._def_tree.addTopLevelItem(item)
      else:
         QTreeWidgetItem(
            self._def_tree,
            ["(no definition-level differences)", ""],
         )

      diff_text = pmm_services.get_unified_diff(
         self._rel_path, self._mod_a, self._mod_b
      )
      _render_diff(
         self._unified_edit,
         diff_text or "(files are identical or one is missing)",
      )

   def _on_def_selected(self) -> None:
      items = self._def_tree.selectedItems()
      if not items:
         return
      dd: DefinitionDiff | None = items[0].data(0, Qt.ItemDataRole.UserRole)
      if dd is None:
         return
      import difflib
      lines_a = (dd.text_a + "\n").splitlines(keepends=True) if dd.text_a else []
      lines_b = (dd.text_b + "\n").splitlines(keepends=True) if dd.text_b else []
      mini_diff = "".join(difflib.unified_diff(
         lines_a, lines_b,
         fromfile=self._mod_a.name,
         tofile=self._mod_b.name,
      ))
      self._def_diff_edit.show()
      _render_diff(self._def_diff_edit, mini_diff or "(identical)")
      self._bottom_tabs.setCurrentWidget(self._def_diff_edit)


# ── colour-coded diff renderer ────────────────────────────────────────────────

_ADD_FMT = QTextCharFormat()
_ADD_FMT.setBackground(QColor("#1c3a1c"))
_ADD_FMT.setForeground(QColor("#7ec87e"))

_DEL_FMT = QTextCharFormat()
_DEL_FMT.setBackground(QColor("#3a1c1c"))
_DEL_FMT.setForeground(QColor("#e07070"))

_HDR_FMT = QTextCharFormat()
_HDR_FMT.setForeground(QColor("#6699cc"))

_DEF_FMT = QTextCharFormat()


def _render_diff(edit: QTextEdit, text: str) -> None:
   edit.clear()
   cursor = edit.textCursor()
   for line in text.splitlines(keepends=True):
      if line.startswith(("---", "+++")):
         cursor.setCharFormat(_HDR_FMT)
      elif line.startswith("@@"):
         cursor.setCharFormat(_HDR_FMT)
      elif line.startswith("+"):
         cursor.setCharFormat(_ADD_FMT)
      elif line.startswith("-"):
         cursor.setCharFormat(_DEL_FMT)
      else:
         cursor.setCharFormat(_DEF_FMT)
      cursor.insertText(line)
   edit.setTextCursor(cursor)
   edit.moveCursor(QTextCursor.MoveOperation.Start)
