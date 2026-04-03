"""
pmm_services
============
Business logic: load-order resolution, file-level conflict detection,
definition-level (deep) conflict analysis, and background workers.

Phase 8 additions
-----------------
  ConflictSeverity   — HARD (same definition ID overwritten) / SOFT (file only)
  FileConflict       — dataclass with rel_path, owners, severity, conflicting_defs
  detect_file_conflicts_ex() — file scan + severity in one call
  ConflictScanWorker — QThread wrapper; emits progress + finished
"""

import contextlib
import difflib
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Tuple

from PySide6.QtCore import QThread, Signal

from pmm_clausewitz import CWPair, parse_text, unparse_pair
from pmm_models import Mod, ModCollection


# ── load-order resolution ─────────────────────────────────────────────────────

def resolve_load_order(mods: List[Mod], collection: ModCollection) -> List[Mod]:
   """Return mods in collection order, filtering to enabled only."""
   by_id = {m.id: m for m in mods}
   ordered = [by_id[mid] for mid in collection.mods if mid in by_id]
   return [m for m in ordered if m.enabled]


# ── legacy launcher helper (kept for backward compat) ────────────────────────

def apply_load_order_to_launcher(
      game_user_data: Path, collection: ModCollection
) -> None:
   import json
   dlc_load = {
      "enabled_mods": [
         mid if mid.startswith("mod/") else f"mod/{mid}.mod"
         for mid in collection.mods
      ],
      "disabled_dlcs": [],
   }
   (game_user_data / "dlc_load.json").write_text(json.dumps(dlc_load, indent=2))


# ── extensions ────────────────────────────────────────────────────────────────

# Files with these extensions are skipped during file-level conflict scanning.
_BINARY_EXTS = frozenset({
   ".png", ".dds", ".jpg", ".jpeg", ".tga", ".bmp", ".gif", ".webp",
   ".wav", ".ogg", ".mp3", ".wem",
   ".mesh", ".anim", ".asset",
})

# Files with these extensions are Clausewitz text and can be deep-diffed.
_CW_TEXT_EXTS = frozenset({
   ".txt", ".cfg", ".gui", ".gfx", ".sfx",
   ".mod", ".map",
})

# All text extensions we may encounter (CW + data formats).
_ALL_TEXT_EXTS = _CW_TEXT_EXTS | frozenset({
   ".csv", ".yml", ".yaml", ".json", ".lua", ".shader", ".fxh",
})


# Cache for parse_text(...).definition_names() used by conflict scans.
# Keyed by absolute file path + stat tuple so edits invalidate naturally.
_DEF_NAMES_CACHE: dict[tuple[str, int, int], frozenset[str]] = {}
_DEF_NAMES_CACHE_LOCK = threading.Lock()
_DEF_NAMES_CACHE_MAX = 10_000


def _definition_cache_key(path: Path) -> tuple[str, int, int] | None:
   with contextlib.suppress(OSError):
      st = path.stat()
      return str(path.resolve()), st.st_mtime_ns, st.st_size
   return None


def _cached_definition_names(path: Path) -> frozenset[str]:
   key = _definition_cache_key(path)
   if key is None:
      return frozenset()

   with _DEF_NAMES_CACHE_LOCK:
      cached = _DEF_NAMES_CACHE.get(key)
   if cached is not None:
      return cached

   names: frozenset[str] = frozenset()
   with contextlib.suppress(Exception):
      text = path.read_text(encoding="utf-8-sig", errors="replace")
      names = frozenset(parse_text(text, path).definition_names())

   with _DEF_NAMES_CACHE_LOCK:
      if len(_DEF_NAMES_CACHE) >= _DEF_NAMES_CACHE_MAX:
         _DEF_NAMES_CACHE.clear()
      _DEF_NAMES_CACHE[key] = names
   return names


# ── severity ──────────────────────────────────────────────────────────────────

class ConflictSeverity(Enum):
   HARD = "hard"  # ≥2 mods define the same named definition in the same file
   SOFT = "soft"  # file overlap only — no definition-level conflict detected


@dataclass
class FileConflict:
   """A file that exists in more than one mod, with severity metadata."""
   rel_path: str
   owners: List[Mod]
   severity: ConflictSeverity
   # Definition keys that appear in ≥2 mods (non-empty only for HARD).
   conflicting_defs: List[str] = field(default_factory=list)


# ── file-level conflict scan ──────────────────────────────────────────────────

def _collect_file_owners(mods: List[Mod]) -> Dict[str, List[Mod]]:
   """
   Walk every mod's directory and return owners for each relevant file.

   Returns a dict of relative path → list of Mods that contain that file.

   Skipped:
     • dot-directories (.git, .idea, …)
     • descriptor.mod / changelog.txt at the root
     • binary / image files
   """
   file_owners: Dict[str, List[Mod]] = defaultdict(list)
   for mod in mods:
      root = mod.path
      if not root.is_dir():
         continue
      for f in root.rglob("*"):
         rel = f.relative_to(root)
         if any(part.startswith(".") for part in rel.parts):
            continue
         if not f.is_file():
            continue
         if str(rel).lower() in {"descriptor.mod", "changelog.txt"}:
            continue
         if f.suffix.lower() in _BINARY_EXTS:
            continue
         file_owners[str(rel)].append(mod)
   return file_owners


def detect_file_conflicts(mods: List[Mod]) -> Dict[str, List[Mod]]:
   """
   Walk every mod's directory and return paths present in more than one mod.
   Returns  dict[relative_path_str → list[Mod]].
   """
   file_owners = _collect_file_owners(mods)
   return {k: v for k, v in file_owners.items() if len(v) > 1}


def detect_file_conflicts_ex(mods: List[Mod]) -> Dict[str, FileConflict]:
   """
   File-level conflict scan + severity classification.

   Same as detect_file_conflicts() but each result is a FileConflict with:
     severity = HARD  when ≥2 mods define the same named definition
     severity = SOFT  for plain file overlaps (or non-CW text files)

   Use ConflictScanWorker for non-blocking execution in the UI.
   """
   raw = detect_file_conflicts(mods)
   result: Dict[str, FileConflict] = {}
   for rel_path, owners in raw.items():
      severity, conflicting_defs = _classify_severity(rel_path, owners)
      result[rel_path] = FileConflict(rel_path, owners, severity, conflicting_defs)
   return result


def _classify_severity(
      rel_path: str, owners: List[Mod]
) -> Tuple[ConflictSeverity, List[str]]:
   """
   Determine whether a multi-mod file overlap is a HARD or SOFT conflict.

   HARD: the file is a Clausewitz text file and ≥2 mods define the same
         top-level definition key (by name/id/token/…).
   SOFT: everything else.

   Returns (severity, conflicting_def_keys).
   """
   suffix = Path(rel_path).suffix.lower()
   if suffix not in _CW_TEXT_EXTS:
      return ConflictSeverity.SOFT, []

   key_counts: Counter[str] = Counter()
   for mod in owners:
      path = mod.path / rel_path
      if not path.is_file():
         continue
      for k in _cached_definition_names(path):
         key_counts[k] += 1

   conflicting = sorted(k for k, n in key_counts.items() if n > 1)
   if conflicting:
      return ConflictSeverity.HARD, conflicting
   return ConflictSeverity.SOFT, []


# ── unified diff ──────────────────────────────────────────────────────────────

@dataclass
class DefinitionDiff:
   """
   A single top-level definition that differs between two mod files.

   status:
     "changed"   – present in both mods but with different content
     "only_in_a" – present only in mod_a
     "only_in_b" – present only in mod_b
   """
   def_id: str
   status: str
   text_a: str  # unparse_pair output from mod_a, or ""
   text_b: str  # unparse_pair output from mod_b, or ""


def get_unified_diff(rel_path: str, mod_a: Mod, mod_b: Mod) -> str:
   """
   Return a unified diff string comparing rel_path in mod_a vs mod_b.
   Returns "" if either file is missing or unreadable.
   """
   path_a = mod_a.path / rel_path
   path_b = mod_b.path / rel_path
   if not path_a.is_file() or not path_b.is_file():
      return ""
   try:
      lines_a = path_a.read_text(encoding="utf-8-sig", errors="replace").splitlines(keepends=True)
      lines_b = path_b.read_text(encoding="utf-8-sig", errors="replace").splitlines(keepends=True)
   except OSError:
      return ""
   return "".join(
      difflib.unified_diff(
         lines_a, lines_b,
         fromfile=f"{mod_a.name}/{rel_path}",
         tofile=f"{mod_b.name}/{rel_path}",
      )
   )


def get_definition_diffs(
      rel_path: str, mod_a: Mod, mod_b: Mod
) -> List[DefinitionDiff]:
   """
   Parse rel_path from both mods and return definitions that differ.

   Non-CW text files (JSON, YAML, CSV, …) get a raw unified diff only;
   binary files return a single placeholder entry.
   """
   suffix = Path(rel_path).suffix.lower()

   if suffix in _BINARY_EXTS:
      return [
         DefinitionDiff(
            def_id="",
            status="changed",
            text_a=f"(binary file in {mod_a.name})",
            text_b=f"(binary file in {mod_b.name})",
         )
      ]

   if suffix not in _CW_TEXT_EXTS:
      # Not parseable as CW script — signal caller to use unified diff only
      return []

   defs_a: Dict[str, CWPair] = {}
   defs_b: Dict[str, CWPair] = {}

   def _load(path: Path, target: Dict[str, CWPair]) -> None:
      if not path.is_file():
         return
      with contextlib.suppress(Exception):
         text = path.read_text(encoding="utf-8-sig", errors="replace")
         target.update(parse_text(text, path).definitions())

   _load(mod_a.path / rel_path, defs_a)
   _load(mod_b.path / rel_path, defs_b)

   result: List[DefinitionDiff] = []
   for key in sorted(set(defs_a) | set(defs_b)):
      pair_a = defs_a.get(key)
      pair_b = defs_b.get(key)
      ta = unparse_pair(pair_a) if pair_a is not None else ""
      tb = unparse_pair(pair_b) if pair_b is not None else ""
      if ta == tb:
         continue
      if pair_a is None:
         status = "only_in_b"
      elif pair_b is None:
         status = "only_in_a"
      else:
         status = "changed"
      result.append(DefinitionDiff(def_id=key, status=status, text_a=ta, text_b=tb))

   return result


# ── background conflict scanner ───────────────────────────────────────────────

class ConflictScanWorker(QThread):
   """
   Background thread that runs a full conflict scan (file + severity).

   Signals
   -------
   progress(done: int, total: int, phase: str)
       Emitted periodically during the scan so the UI can show a progress
       indicator.  `phase` is one of "scanning" or "classifying".

   finished(conflicts: dict[str, FileConflict])
       Emitted once when the scan completes successfully.

   error(message: str)
       Emitted if an unhandled exception occurs.

   Usage
   -----
   worker = ConflictScanWorker(mods, parent=self)
   worker.progress.connect(self._on_progress)
   worker.finished.connect(self._on_finished)
   worker.error.connect(self._on_error)
   worker.start()

   To cancel a running scan call worker.cancel(); the worker will stop at
   the next mod boundary and emit finished({}).
   """

   progress = Signal(int, int, str)  # (done, total, phase)
   finished = Signal(object)  # dict[str, FileConflict]
   error = Signal(str)

   def __init__(self, mods: List[Mod], parent=None) -> None:
      super().__init__(parent)
      self._mods = mods
      self._cancelled = False

   def cancel(self) -> None:
      """Request early termination.  Does not block."""
      self._cancelled = True

   def run(self) -> None:
      try:
         result = self._run_scan()
         self.finished.emit(result)
      except Exception as exc:  # noqa: BLE001
         self.error.emit(str(exc))

   def _run_scan(self) -> Dict[str, FileConflict]:
      mods = self._mods
      total = len(mods)

      # ── Phase 1: file-level scan ──────────────────────────────────────────
      file_owners: Dict[str, List[Mod]] = defaultdict(list)

      for i, mod in enumerate(mods):
         if self._cancelled:
            return {}
         self.progress.emit(i, total, "scanning")
         # Reuse the same filtering logic as detect_file_conflicts
         for rel_path, owners in _collect_file_owners([mod]).items():
            file_owners[rel_path].extend(owners)

      self.progress.emit(total, total, "scanning")

      conflicts = {k: v for k, v in file_owners.items() if len(v) > 1}

      # ── Phase 2: severity classification ─────────────────────────────────
      result: Dict[str, FileConflict] = {}
      items = list(conflicts.items())
      n = len(items)

      for j, (rel_path, owners) in enumerate(items):
         if self._cancelled:
            return {}
         self.progress.emit(j, n, "classifying")
         severity, conflicting_defs = _classify_severity(rel_path, owners)
         result[rel_path] = FileConflict(rel_path, owners, severity, conflicting_defs)

      self.progress.emit(n, n, "classifying")
      return result
