"""
Patch Mod Dialog — drives the conflict solver UI.

Flow
----
1. User clicks "🔧 Generate Patch Mod" in the Conflicts tab.
2. PatchDialog is opened with the list of FileConflict objects and the
   ordered mod list.
3. The dialog:
   a. Runs build_patch_plan() + apply_auto_resolutions() immediately.
   b. Shows a summary page:
        "Automatically resolved N definitions.
         M definitions need your input."
      with an option to proceed fully automatically (skip manual tasks)
      or to review them.
   c. On the review page, for each ResolutionTask the user sees:
        • Which mods conflict
        • A side-by-side view of every version
        • Radio buttons to pick a version, or an "Open in editor" button
          that writes the skeleton line and opens the external editor.
   d. On the final page, the user picks:
        • Patch mod name (default: "Pyrony Patch - <collection>")
        • Whether to add the patch mod to the current collection automatically
   e. Clicking "Write Patch Mod" calls write_patch_mod() and emits
      patch_created(patch_folder_name).

The dialog is a QDialog with a simple stacked layout — no QWizard
dependency needed.
"""

from pathlib import Path
from typing import Dict, List, Optional
import contextlib

from pmm.core.patch_solver import (
   apply_auto_resolutions,
   build_patch_plan,
   FilePatchPlan,
   open_in_editor,
   ResolutionTask,
   Strategy,
   write_patch_mod,
   safe_folder_name,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
                               QRadioButton, QScrollArea, QSplitter, QStackedWidget, QTextEdit,
                               QVBoxLayout, QWidget)

from pmm.core.models import Mod
from pmm.core.services import FileConflict
from pmm.ui.error_util import show_error, show_warning


class PatchDialog(QDialog):
   """
   Multi-step dialog that guides the user through conflict resolution
   and generates a patch mod.

   Signals
   -------
   patch_created(str)   – emitted with the patch mod folder name after
                          write_patch_mod() succeeds.
   """

   patch_created: Signal = Signal(str, bool)

   def __init__(
         self,
         conflicts: Dict[str, FileConflict],
         mods_in_order: List[Mod],
         game_user_data: Path,
         collection_name: str = "",
         parent: QWidget | None = None,
   ) -> None:
      super().__init__(parent)
      self.setWindowTitle("Generate Patch Mod")
      self.resize(900, 650)

      self._conflicts = conflicts
      self._mods = mods_in_order
      self._game_user_data = game_user_data
      self._plans: List[FilePatchPlan] = []
      self._collection_name = collection_name

      # ── stacked pages ────────────────────────────────────────────────
      self._stack = QStackedWidget()

      self._page_summary = _SummaryPage()
      self._page_review = _ReviewPage()
      self._page_finish = _FinishPage(collection_name)

      self._stack.addWidget(self._page_summary)  # 0
      self._stack.addWidget(self._page_review)  # 1
      self._stack.addWidget(self._page_finish)  # 2

      # ── nav buttons ──────────────────────────────────────────────────
      self._back_btn = QPushButton("← Back")
      self._next_btn = QPushButton("Next →")
      self._write_btn = QPushButton("✔ Write Patch Mod")
      self._write_btn.hide()

      self._back_btn.clicked.connect(self._go_back)
      self._next_btn.clicked.connect(self._go_next)
      self._write_btn.clicked.connect(self._write)

      nav = QHBoxLayout()
      nav.addStretch()
      nav.addWidget(self._back_btn)
      nav.addWidget(self._next_btn)
      nav.addWidget(self._write_btn)

      root = QVBoxLayout(self)
      root.addWidget(self._stack)
      root.addLayout(nav)

      # Run analysis immediately.
      self._analyse()

   # ── analysis ─────────────────────────────────────────────────────────────

   def _analyse(self) -> None:
      self._plans = build_patch_plan(self._conflicts, self._mods)
      auto_n, manual_n = apply_auto_resolutions(self._plans)
      total = auto_n + manual_n
      self._page_summary.populate(total, auto_n, manual_n)
      self._page_review.populate(self._plans)

   # ── navigation ───────────────────────────────────────────────────────────

   def _go_next(self) -> None:
      idx = self._stack.currentIndex()
      if idx == 0:  # summary → review (or finish if no manual tasks)
         if self._page_summary.has_manual:
            self._stack.setCurrentIndex(1)
         else:
            self._go_to_finish_page()
         self._back_btn.show()
      elif idx == 1:  # review → finish
         self._go_to_finish_page()

   def _go_to_finish_page(self):
      self._stack.setCurrentIndex(2)
      self._next_btn.hide()
      self._write_btn.show()

   def _go_back(self) -> None:
      idx = self._stack.currentIndex()
      if idx == 2:
         prev = 1 if self._page_summary.has_manual else 0
         self._stack.setCurrentIndex(prev)
         self._next_btn.show()
         self._write_btn.hide()
      elif idx == 1:
         self._stack.setCurrentIndex(0)
         self._back_btn.hide()

   # ── write ─────────────────────────────────────────────────────────────────

   def _write(self) -> None:
      # Collect any manual resolutions from the review page.
      self._page_review.flush_manual_choices()

      patch_name = self._page_finish.patch_name()
      from shutil import rmtree

      # Compute folder and remove any previous patch completely.
      folder = safe_folder_name(patch_name)
      patch_root = self._game_user_data / "mod" / folder
      if patch_root.exists():
         try:
            rmtree(patch_root)
         except Exception as exc:
            show_error(self, "Remove old patch mod failed", exc)
            return

      try:
         patch_root = write_patch_mod(
            self._plans,
            patch_name=patch_name,
            game_user_data=self._game_user_data,
            overwrite=True,
         )
      except Exception as exc:
         show_error(self, "Write failed", exc)
         return

      folder = patch_root.name
      msg = f"""Patch mod written to:
{patch_root}

The patch mod will be added at the end of the current collection."""
      QMessageBox.information(self, "Patch mod created", msg)
      self.patch_created.emit(folder, True)
      self.accept()


# ── Page 0 — Summary ──────────────────────────────────────────────────────────

class _SummaryPage(QWidget):
   def __init__(self, parent: QWidget | None = None) -> None:
      super().__init__(parent)
      self._label = QLabel()
      self._label.setWordWrap(True)
      self._label.setAlignment(Qt.AlignmentFlag.AlignTop)

      layout = QVBoxLayout(self)
      layout.addWidget(QLabel("<b>Conflict Analysis</b>"))
      layout.addWidget(self._label)
      layout.addStretch()

      self.has_manual = False

   def populate(self, total: int, auto_n: int, manual_n: int) -> None:
      self.has_manual = manual_n > 0
      if total == 0:
         text = (
            "No HARD definition conflicts were found.\\n\\n"
            "A patch mod is not needed — load order is sufficient."
         )
      else:
         text = (
            f"Found <b>{total}</b> conflicting definition(s).\n\n"
            f"✔ <b>{auto_n}</b> can be resolved automatically "
            f"(last-mod-wins or additive merge).\\n"
         )
         if manual_n:
            text += (
               f"⚠ <b>{manual_n}</b> require your input "
               f"(click Next to review them).\\n"
            )
         else:
            text += "\\nClick Next to generate the patch mod."
      self._label.setText(text)


# ── Page 1 — Review manual tasks ──────────────────────────────────────────────

class _ReviewPage(QWidget):
   def __init__(self, parent: QWidget | None = None) -> None:
      super().__init__(parent)
      self._task_list = QListWidget()
      self._detail = _TaskDetailPanel()

      self._task_list.currentRowChanged.connect(self._on_row_changed)
      self._tasks: List[ResolutionTask] = []

      splitter = QSplitter(Qt.Orientation.Horizontal)
      splitter.addWidget(self._task_list)
      splitter.addWidget(self._detail)
      splitter.setStretchFactor(0, 1)
      splitter.setStretchFactor(1, 3)

      layout = QVBoxLayout(self)
      layout.addWidget(QLabel("<b>Review Conflicts</b>"))
      layout.addWidget(splitter)

   def populate(self, plans: List[FilePatchPlan]) -> None:
      self._tasks.clear()
      self._task_list.clear()
      for plan in plans:
         for task in plan.tasks:
            if task.auto_strategy == Strategy.MANUAL or not task.resolved:
               self._tasks.append(task)
               icon = "✔" if task.resolved else "⚠"
               self._task_list.addItem(
                  QListWidgetItem(f"{icon} {task.def_key}  ({task.rel_path})")
               )
      if self._tasks:
         self._task_list.setCurrentRow(0)

   def _on_row_changed(self, row: int) -> None:
      if 0 <= row < len(self._tasks):
         self._detail.load_task(self._tasks[row])

   def flush_manual_choices(self) -> None:
      """Copy any in-progress choice back into the active task."""
      self._detail.flush()


class _TaskDetailPanel(QWidget):
   """Shows one ResolutionTask — side-by-side version picker."""

   def __init__(self, parent: QWidget | None = None) -> None:
      super().__init__(parent)
      self._task: Optional[ResolutionTask] = None
      self._radio_group = QButtonGroup(self)
      self._radios: List[QRadioButton] = []

      self._header = QLabel()
      self._header.setWordWrap(True)

      self._versions_area = QWidget()
      self._versions_layout = QVBoxLayout(self._versions_area)

      scroll = QScrollArea()
      scroll.setWidgetResizable(True)
      scroll.setWidget(self._versions_area)

      mono = QFont("Courier New", 9)
      self._preview = QTextEdit()
      self._preview.setReadOnly(True)
      self._preview.setFont(mono)
      self._preview.setMaximumHeight(200)

      self._editor_btn = QPushButton("📝 Open file in external editor")
      self._editor_btn.clicked.connect(self._open_editor)

      layout = QVBoxLayout(self)
      layout.addWidget(self._header)
      layout.addWidget(scroll, stretch=1)
      layout.addWidget(QLabel("Preview of chosen version:"))
      layout.addWidget(self._preview)
      layout.addWidget(self._editor_btn)

   def load_task(self, task: ResolutionTask) -> None:
      self._task = task
      mods = ", ".join(task.mod_names)
      self._header.setText(
         f"<b>{task.def_key}</b><br>"
         f"<small>{task.rel_path}</small><br>"
         f"<small>Mods: {mods}</small>"
      )

      # Clear old radio buttons
      for r in self._radios:
         self._radio_group.removeButton(r)
         r.deleteLater()
      self._radios.clear()
      while self._versions_layout.count():
         item = self._versions_layout.takeAt(0)
         if item is None:
            continue
         if item.widget():
            item.widget().deleteLater()

      for i, v in enumerate(task.versions):
         radio = QRadioButton(f"{v.mod.name}")
         self._radio_group.addButton(radio, i)
         mono = QFont("Courier New", 8)

         preview_edit = QTextEdit()
         preview_edit.setReadOnly(True)
         preview_edit.setFont(mono)
         preview_edit.setPlainText(v.text)
         preview_edit.setMaximumHeight(120)

         box = QFrame()
         box.setFrameStyle(QFrame.Shape.StyledPanel)
         bl = QVBoxLayout(box)
         bl.addWidget(radio)
         bl.addWidget(preview_edit)

         self._versions_layout.addWidget(box)
         self._radios.append(radio)
         radio.toggled.connect(lambda checked, idx=i: self._on_radio(checked, idx))

      # Pre-select according to current state.
      if task.resolved and task.chosen_text:
         for i, v in enumerate(task.versions):
            if v.text == task.chosen_text and i < len(self._radios):
               self._radios[i].setChecked(True)
               break
      elif self._radios:
         self._radios[-1].setChecked(True)

   def _on_radio(self, checked: bool, idx: int) -> None:
      if checked and self._task and idx < len(self._task.versions):
         v = self._task.versions[idx]
         self._task.resolve_pick(v.mod)
         self._preview.setPlainText(self._task.chosen_text)

   def flush(self) -> None:
      """Ensure the current selection is applied to the task."""
      if not self._task:
         return
      checked_id = self._radio_group.checkedId()
      if 0 <= checked_id < len(self._task.versions):
         self._task.resolve_pick(self._task.versions[checked_id].mod)

   def _open_editor(self) -> None:
      if not self._task:
         return
      # Write the current chosen text (or last-mod version) to a temp file.
      import tempfile
      text = self._task.chosen_text or (
         self._task.versions[-1].text if self._task.versions else ""
      )
      suffix = Path(self._task.rel_path).suffix
      with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, delete=False, encoding="utf-8"
      ) as tf:
         tf.write(
            f"# Edit this definition and SAVE the file.\\n"
            f"# Pyrony will read it back when you close the editor.\\n"
            f"# Definition: {self._task.def_key}\\n"
            f"# File: {self._task.rel_path}\\n\\n"
         )
         tf.write(text)
         tmp_path = Path(tf.name)

      open_in_editor(tmp_path)

      # Show a "Done editing?" dialog so we know when to read it back.
      reply = QMessageBox.question(
         self,
         "Reload from editor",
         "Click OK when you have saved and closed the editor.\\n"
         "Pyrony will reload your edited version.",
         QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
      )
      if reply == QMessageBox.StandardButton.Ok:
         with contextlib.suppress(Exception):
            edited = tmp_path.read_text(encoding="utf-8")
            # Strip the header comment lines we injected.
            lines = [l for l in edited.splitlines() if not l.startswith("#")]
            if cleaned := "\\n".join(lines).strip():
               self._task.resolve_custom(cleaned)
               self._preview.setPlainText(cleaned)
      with contextlib.suppress(Exception):
         tmp_path.unlink()


# ── Page 2 — Finish / name + options ─────────────────────────────────────────

class _FinishPage(QWidget):
   def __init__(
         self, collection_name: str = "", parent: QWidget | None = None
   ) -> None:
      super().__init__(parent)
      from pmm.core.patch_solver import patch_name_for_collection

      self._collection_name = collection_name
      self._patch_name = patch_name_for_collection(collection_name or "playset")

      self._add_check = QCheckBox(
         "Add this patch mod to the collection automatically"
      )
      self._add_check.setChecked(True)

      layout = QVBoxLayout(self)
      layout.addWidget(QLabel("<b>Finalise Patch Mod</b>"))
      layout.addWidget(
         QLabel(
            f"Patch mod name (fixed): <b>{self._patch_name}</b><br>"
            "<small>The patch mod will be written to your game's mod directory and "
            "must be placed <b>last</b> in the collection.</small>"
         )
      )
      layout.addSpacing(12)
      layout.addWidget(self._add_check)
      layout.addStretch()

   def patch_name(self) -> str:
      return self._patch_name

   def add_to_collection(self) -> bool:
      # Behaviour is now always "add", this is kept for signal signature compat.
      return True
