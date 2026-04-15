"""QThread wrappers for build_patch_mod and build_merged_mod."""

from concurrent.futures import as_completed, ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List

from PySide6.QtCore import QThread, Signal

from pmm.core.models import Mod
from pmm.core.semantic import build_merged_mod
from pmm.core.services import FileConflict


class _BaseSemanticWorker(QThread):
   progress = Signal(int, int, str)  # done, total, current_file
   finished = Signal(object)  # PatchResult
   error = Signal(str)

   def __init__(self, parent=None):
      super().__init__(parent)
      self._cancelled = False

   def cancel(self) -> None:
      self._cancelled = True


class PatchModWorker(_BaseSemanticWorker):
   """Runs build_patch_mod off the main thread."""

   def __init__(
         self,
         conflicts: Dict[str, FileConflict],
         mods: List[Mod],
         out_dir: Path,
         mod_name: str,
         parent=None,
   ):
      super().__init__(parent)
      self._conflicts = conflicts
      self._mods = mods
      self._out_dir = out_dir
      self._mod_name = mod_name

   def run(self):
      try:
         # build_patch_mod processes one file at a time — we wrap it to
         # emit progress by splitting the work ourselves.
         from pmm.core.semantic import (
            _merge_cw_file, _write_descriptor, MergeNote, PatchResult
         )
         from pmm.core.services import ConflictSeverity, _CW_TEXT_EXTS
         import shutil

         hard = {k: v for k, v in self._conflicts.items()
                 if v.severity == ConflictSeverity.HARD}
         total = len(hard)
         mod_root = self._out_dir / self._mod_name
         mod_root.mkdir(parents=True, exist_ok=True)
         notes: list[MergeNote] = []
         files_written = 0

         # Use a thread pool for the parse+merge phase of each file
         with ThreadPoolExecutor(max_workers=4) as pool:
            def process(item):
               rel_path_l, fc = item
               if self._cancelled:
                  return None, None, rel_path_l
               suffix = Path(rel_path_l).suffix.lower()
               local_notes_l: list[MergeNote] = []
               if suffix not in _CW_TEXT_EXTS:
                  src_l = fc.owners[-1].path / rel_path_l
                  return src_l, None, rel_path_l  # binary: just copy
               text_l = _merge_cw_file(rel_path_l, fc.owners, local_notes_l, hard_only=True)
               return None, (text_l, local_notes_l), rel_path_l

            futures = {pool.submit(process, item): item[0]
                       for item in hard.items()}
            done = 0
            for fut in as_completed(futures):
               done += 1
               rel_path = futures[fut]
               self.progress.emit(done, total, rel_path)
               if self._cancelled:
                  break
               src, text_result, rp = fut.result()
               dst = mod_root / rp
               dst.parent.mkdir(parents=True, exist_ok=True)
               if src is not None and src.is_file():
                  shutil.copy2(src, dst)
               elif text_result is not None:
                  text, local_notes = text_result
                  dst.write_text(text, encoding="utf-8")
                  notes.extend(local_notes)
               files_written += 1

         _write_descriptor(mod_root, self._mod_name, "patch", self._out_dir)
         unresolved = [n.def_id for n in notes if "MANUAL" in n.note]
         self.finished.emit(PatchResult(mod_root, files_written, notes, unresolved))
      except Exception as exc:
         self.error.emit(str(exc))


class MergedModWorker(_BaseSemanticWorker):
   """Runs build_merged_mod off the main thread."""

   def __init__(self, mods: List[Mod], out_dir: Path, mod_name: str, parent=None):
      super().__init__(parent)
      self._mods = mods
      self._out_dir = out_dir
      self._mod_name = mod_name

   def run(self):
      try:
         result = build_merged_mod(self._mods, self._out_dir, self._mod_name)
         self.finished.emit(result)
      except Exception as exc:
         self.error.emit(str(exc))
