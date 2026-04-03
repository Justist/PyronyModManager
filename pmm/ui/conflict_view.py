"""
conflict_view
=============
The "Conflicts" tab widget.

Layout
------
  ┌──────────────────┬───────────────────────────────────────┐
  │  [🔄 Scan]       │                                       │
  │  [🔍 filter…   ] │   Select a conflicting file to see    │
  │                  │        the diff.                      │
  │  HARD conflicts  │   (or diff tabs once selected)        │
  │   ├ mod A        │                                       │
  │   └ mod B        │                                       │
  │  SOFT conflicts  │                                       │
  │   …              │                                       │
  │  ─────────────── │                                       │
  │  summary label   │                                       │
  └──────────────────┴───────────────────────────────────────┘

Phase 8 additions
-----------------
  • ConflictScanWorker for non-blocking scan
  • Severity icons (🔴 HARD / 🟡 SOFT) in the tree
  • Filter bar (case-insensitive substring match on file path)
  • Summary label after scan
  • Cancel in-progress scan when a new one starts
"""

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QLineEdit, QProgressBar,
                               QPushButton, QSplitter, QTabWidget, QTextEdit, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from pmm.core.models import Mod
from pmm.core.services import (
   ConflictScanWorker, ConflictSeverity, DefinitionDiff, FileConflict,
   get_definition_diffs, get_unified_diff
)


# ── tree-item payload types ───────────────────────────────────────────────────

@dataclass(frozen=True)
class _FileNodeData:
   kind: str  # "file"
   rel_path: str
   owners: list[Mod]
   severity: ConflictSeverity


@dataclass(frozen=True)
class _ModNodeData:
   kind: str  # "mod"
   rel_path: str
   mod: Mod


# ── ConflictView ──────────────────────────────────────────────────────────────

class ConflictView(QWidget):
   def __init__(self, parent: QWidget | None = None) -> None:
      super().__init__(parent)

      # ── toolbar ──────────────────────────────────────────────────────────
      self._scan_btn = QPushButton("🔄 Scan for conflicts")
      self._scan_btn.clicked.connect(self._scan)

      self._filter = QLineEdit()
      self._filter.setPlaceholderText("🔍  Filter files…")
      self._filter.setClearButtonEnabled(True)
      self._filter.textChanged.connect(self._apply_filter)

      toolbar = QHBoxLayout()
      toolbar.addWidget(self._scan_btn)
      toolbar.addWidget(self._filter, stretch=1)

      # ── progress bar (indeterminate, hidden most of the time) ────────────
      self._progress = QProgressBar()
      self._progress.setMaximumHeight(8)
      self._progress.setTextVisible(False)
      self._progress.hide()

      # ── status label (shown below the toolbar while scanning) ────────────
      self._status = QLabel("")
      self._status.setStyleSheet("color: #888; font-size: 11px;")

      # ── conflict tree ─────────────────────────────────────────────────────
      self._tree = QTreeWidget()
      self._tree.setHeaderLabels(["File", "Severity"])
      h = self._tree.header()
      h.setStretchLastSection(False)
      h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
      h.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
      self._tree.itemSelectionChanged.connect(self._on_selection)

      # ── summary ───────────────────────────────────────────────────────────
      self._summary = QLabel("")
      self._summary.setStyleSheet("color: #aaa; font-size: 11px; padding: 2px 0;")

      left = QWidget()
      ll = QVBoxLayout(left)
      ll.setContentsMargins(0, 0, 4, 0)
      ll.addLayout(toolbar)
      ll.addWidget(self._progress)
      ll.addWidget(self._status)
      ll.addWidget(self._tree, stretch=1)
      ll.addWidget(self._summary)

      # ── right panel ───────────────────────────────────────────────────────
      self._placeholder = QLabel("Select a conflicting file to see the diff.")
      self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
      self._placeholder.setStyleSheet("color: #888;")

      self._diff_tabs = QTabWidget()
      self._diff_tabs.hide()

      right = QWidget()
      rl = QVBoxLayout(right)
      rl.setContentsMargins(4, 0, 0, 0)
      rl.addWidget(self._placeholder)
      rl.addWidget(self._diff_tabs)

      # ── splitter ──────────────────────────────────────────────────────────
      splitter = QSplitter(Qt.Orientation.Horizontal)
      splitter.addWidget(left)
      splitter.addWidget(right)
      splitter.setStretchFactor(0, 2)
      splitter.setStretchFactor(1, 3)

      root_layout = QVBoxLayout(self)
      root_layout.setContentsMargins(0, 0, 0, 0)
      root_layout.addWidget(splitter)

      self._mods: list[Mod] = []
      self._conflicts: dict[str, FileConflict] = {}
      self._worker: ConflictScanWorker | None = None

   # ── public ────────────────────────────────────────────────────────────────

   def set_mods(self, mods: list[Mod]) -> None:
      self._mods = mods

   # ── scan ──────────────────────────────────────────────────────────────────

   def _scan(self) -> None:
      # Cancel any running scan before starting a new one.
      if self._worker and self._worker.isRunning():
         self._worker.cancel()
         self._worker.wait()

      self._tree.clear()
      self._clear_diff_panel()
      self._conflicts = {}
      self._summary.setText("")
      self._filter.clear()

      self._progress.setRange(0, 0)  # indeterminate
      self._progress.show()
      self._scan_btn.setEnabled(False)
      self._status.setText("Scanning mods…")

      self._worker = ConflictScanWorker(self._mods, parent=self)
      self._worker.progress.connect(self._on_progress)
      self._worker.finished.connect(self._on_scan_finished)
      self._worker.error.connect(self._on_scan_error)
      self._worker.start()

   def _on_progress(self, done: int, total: int, phase: str) -> None:
      if total > 0:
         self._progress.setRange(0, total)
         self._progress.setValue(done)
      label = "Scanning mods" if phase == "scanning" else "Classifying conflicts"
      if total:
         self._status.setText(f"{label}… ({done}/{total})")
      else:
         self._status.setText(f"{label}…")

   def _on_scan_finished(self, conflicts: object) -> None:
      result: dict[str, FileConflict] = conflicts  # type: ignore[assignment]
      self._progress.hide()
      self._scan_btn.setEnabled(True)
      self._status.setText("")
      self._conflicts = result
      self._populate_tree(result)

   def _on_scan_error(self, msg: str) -> None:
      self._progress.hide()
      self._scan_btn.setEnabled(True)
      self._status.setText(f"⚠ Scan failed: {msg}")

   # ── tree population ───────────────────────────────────────────────────────

   def _populate_tree(self, conflicts: dict[str, FileConflict]) -> None:
      self._tree.clear()

      if not conflicts:
         QTreeWidgetItem(self._tree, ["✓ No conflicts found", ""])
         self._summary.setText("No conflicts.")
         return

      hard_count = sum(1 for fc in conflicts.values() if fc.severity == ConflictSeverity.HARD)
      soft_count = sum(bool(fc.severity == ConflictSeverity.HARD) for fc in conflicts.values())

      for rel_path, fc in sorted(conflicts.items()):
         is_hard = fc.severity == ConflictSeverity.HARD
         icon = "🔴" if is_hard else "🟡"
         label = "hard" if is_hard else "soft"
         color = QColor("#e07070") if is_hard else QColor("#cc9900")

         top = QTreeWidgetItem([rel_path, f"{icon} {len(fc.owners)} mods · {label}"])
         top.setForeground(0, color)
         top.setForeground(1, color)
         top.setData(0, Qt.ItemDataRole.UserRole,
                     _FileNodeData("file", rel_path, fc.owners, fc.severity))

         for mod in fc.owners:
            child = QTreeWidgetItem([f"  {mod.name}", ""])
            child.setData(0, Qt.ItemDataRole.UserRole,
                          _ModNodeData("mod", rel_path, mod))
            top.addChild(child)

         # Show conflicting definition names as tooltip
         if fc.conflicting_defs:
            top.setToolTip(
               0,
               "Conflicting definitions:\n" + "\n".join(
                  f"  • {d}" for d in fc.conflicting_defs[:20])
               + ("\n  …" if len(fc.conflicting_defs) > 20 else ""),
            )

         self._tree.addTopLevelItem(top)

      self._tree.expandAll()
      self._summary.setText(
         f"{len(conflicts)} conflict{'s' if len(conflicts) != 1 else ''} — "
         f"🔴 {hard_count} hard (definition), 🟡 {soft_count} soft (file only)"
      )

      # Re-apply any active filter text.
      self._apply_filter(self._filter.text())

   # ── filter ────────────────────────────────────────────────────────────────

   def _apply_filter(self, text: str) -> None:
      q = text.strip().lower()
      for i in range(self._tree.topLevelItemCount()):
         item = self._tree.topLevelItem(i)
         if item:
            item.setHidden(bool(q) and q not in item.text(0).lower())

   # ── selection ─────────────────────────────────────────────────────────────

   def _on_selection(self) -> None:
      items = self._tree.selectedItems()
      if not items:
         return
      data = items[0].data(0, Qt.ItemDataRole.UserRole)
      if data is None:
         return

      if isinstance(data, _FileNodeData) and data.kind == "file":
         self._show_diff_for_owners(data.rel_path, data.owners)

      elif isinstance(data, _ModNodeData) and data.kind == "mod":
         fc = self._conflicts.get(data.rel_path)
         owners = fc.owners if fc else []
         others = [m for m in owners if m is not data.mod]
         if others:
            self._show_diff_for_owners(data.rel_path, [others[0], data.mod])

   # ── diff panel ────────────────────────────────────────────────────────────

   def _show_diff_for_owners(self, rel_path: str, owners: list[Mod]) -> None:
      self._clear_diff_panel()
      for mod_a, mod_b in zip(owners, owners[1:]):
         tab = _DiffTab(rel_path, mod_a, mod_b)
         self._diff_tabs.addTab(tab, f"{mod_a.name} → {mod_b.name}")
      self._placeholder.hide()
      self._diff_tabs.show()

   def _clear_diff_panel(self) -> None:
      while self._diff_tabs.count():
         w = self._diff_tabs.widget(0)
         self._diff_tabs.removeTab(0)
         if w:
            w.deleteLater()
      self._diff_tabs.hide()
      self._placeholder.show()


# ── _DiffTab ──────────────────────────────────────────────────────────────────

class _DiffTab(QWidget):
   """
   Per-mod-pair tab.

   Top panel   – structural diff tree (definition-level, from pmm_clausewitz)
   Bottom tabs – Unified diff (full file) / Definition diff (per selected row)
   """

   _STATUS_LABEL: dict[str, str] = {
      "changed": "changed",
      "only_in_a": "only in {a}",
      "only_in_b": "only in {b}",
   }
   _STATUS_COLOR: dict[str, QColor] = {
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
      dh = self._def_tree.header()
      dh.setStretchLastSection(False)
      dh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
      dh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
      self._def_tree.itemSelectionChanged.connect(self._on_def_selected)

      # unified diff (full file)
      self._unified_edit = QTextEdit()
      self._unified_edit.setReadOnly(True)
      self._unified_edit.setFont(mono)

      # definition diff (populated on row selection)
      self._def_diff_edit = QTextEdit()
      self._def_diff_edit.setReadOnly(True)
      self._def_diff_edit.setFont(mono)

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
      diffs = get_definition_diffs(
         self._rel_path, self._mod_a, self._mod_b
      )
      a_name = self._mod_a.name
      b_name = self._mod_b.name

      if diffs:
         for dd in diffs:
            template = self._STATUS_LABEL.get(dd.status, dd.status)
            status_label = template.format(a=a_name, b=b_name)
            item = QTreeWidgetItem([dd.def_id, status_label])
            item.setForeground(1, self._STATUS_COLOR.get(dd.status, QColor("#aaa")))
            item.setData(0, Qt.ItemDataRole.UserRole, dd)
            self._def_tree.addTopLevelItem(item)
      else:
         # Either not a CW text file or no definition-level differences.
         note = "(no parseable definitions — see unified diff)"
         QTreeWidgetItem(self._def_tree, [note, ""])

      diff_text = get_unified_diff(
         self._rel_path, self._mod_a, self._mod_b
      )
      _render_diff(
         self._unified_edit,
         diff_text or "(files are identical or one is missing)",
      )

   def _on_def_selected(self) -> None:
      import difflib
      items = self._def_tree.selectedItems()
      if not items:
         return
      dd: DefinitionDiff | None = items[0].data(0, Qt.ItemDataRole.UserRole)
      if dd is None:
         return
      lines_a = (dd.text_a + "\n").splitlines(keepends=True) if dd.text_a else []
      lines_b = (dd.text_b + "\n").splitlines(keepends=True) if dd.text_b else []
      mini_diff = "".join(difflib.unified_diff(
         lines_a, lines_b,
         fromfile=self._mod_a.name,
         tofile=self._mod_b.name,
      ))
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
