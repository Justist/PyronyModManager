import contextlib
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal


class ModWatcher(QThread):
   """
   Watches a mod directory for filesystem changes using watchfiles.
   Emits mods_changed() when any .mod descriptor file is created,
   deleted, or modified.  Call stop() before destroying the object.
   """

   mods_changed = Signal()

   def __init__(self, path: Path, parent=None) -> None:
      super().__init__(parent)
      self._path = path
      self._stop_event = threading.Event()

   def run(self) -> None:
      with contextlib.suppress(Exception):
         from watchfiles import watch
         for changes in watch(str(self._path), stop_event=self._stop_event):
            if any(Path(p).suffix == ".mod" for _, p in changes):
               self.mods_changed.emit()

   def stop(self) -> None:
      """Signal the watchfiles loop to exit and wait for the thread."""
      self._stop_event.set()
      self.wait(3000)
