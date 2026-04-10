from pathlib import Path
from typing import Dict, List

from PySide6.QtCore import QThread, Signal

from pmm.core.models import Mod
from pmm.core.semantic import build_merged_mod, build_patch_mod
from pmm.core.services import ConflictSeverity, FileConflict


class PatchBuildWorker(QThread):
   """
   Background thread for patch mod / merged mod generation.

   mode: "patch" | "merge"

   For "patch":
       build_patch_mod(conflicts, mods, out_dir, mod_name)
   For "merge":
       build_merged_mod(mods, out_dir, mod_name)
   """
   progress = Signal(int, int, str)  # (done, total, label)
   finished = Signal(object)         # PatchResult
   error = Signal(str)

   def __init__(
         self,
         mode: str,
         mods: List[Mod],
         conflicts: Dict[str, FileConflict],  # used only in "patch" mode
         out_dir: Path,
         mod_name: str,
         parent=None,
   ) -> None:
      super().__init__(parent)
      self._mode = mode
      self._mods = mods
      self._conflicts = conflicts
      self._out_dir = out_dir
      self._mod_name = mod_name

   def run(self) -> None:
      try:
         if self._mode == "patch":
            # Only pass HARD conflicts to the semantic patch builder
            hard_conflicts = {
               k: v for k, v in self._conflicts.items()
               if v.severity == ConflictSeverity.HARD
            }
            result = build_patch_mod(
               hard_conflicts, self._mods,
               self._out_dir, self._mod_name,
            )
         else:
            result = build_merged_mod(
               self._mods, self._out_dir, self._mod_name,
            )
         self.finished.emit(result)
      except Exception as exc:  # noqa: BLE001
         self.error.emit(str(exc))
