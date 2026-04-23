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

from pmm.core.clausewitz import CWPair, parse_text, unparse_pair
from pmm.core.models import Mod, ModCollection


# ── load-order resolution ─────────────────────────────────────────────────────

def resolve_load_order(mods: List[Mod], collection: ModCollection) -> List[Mod]:
   """Return mods in collection order; membership in the collection is the filter."""
   by_id = {m.id: m for m in mods}
   return [by_id[mid] for mid in collection.mods if mid in by_id]


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
   ".png", ".dds", ".jpg", ".jpeg", ".tga", ".bmp", ".gif", ".webp", ".pdn",
   ".wav", ".ogg", ".mp3", ".wem",
   ".mesh", ".anim", ".asset",
   ".py", ".dll", ".exe", ".bin", ".dat", ".zip", ".7z", ".rar",
   ".md", ".pdf", ".docx", ".xlsx", ".pptx",
})

# Files with these extensions are Clausewitz text and can be deep-diffed.
_CW_TEXT_EXTS = frozenset({
   ".txt", ".cfg", ".gui", ".gfx", ".sfx",
   ".mod", ".map",
})

# All text extensions we may encounter (CW + data formats).
_ALL_TEXT_EXTS = _CW_TEXT_EXTS | frozenset({
   ".csv", ".yml", ".yaml", ".json", ".lua", ".shader", ".fxh"
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
     • any .txt file in the topmost folder (root of the mod)
     • binary / image / non-relevant files
   """
   file_owners: Dict[str, List[Mod]] = defaultdict(list)
   for mod in mods:
      root = mod.path
      if not root.is_dir():
         continue
      for f in root.rglob("*"):
         rel = f.relative_to(root)
         # Skip hidden directories/files
         if any(part.startswith(".") for part in rel.parts):
            continue
         if not f.is_file():
            continue

         rel_str = str(rel).lower()

         # Skip special root-level files
         if rel_str in {"descriptor.mod", "changelog.txt"}:
            continue

         # Skip any .txt file in the topmost folder of the mod
         # i.e. relative path has exactly one part and ends with ".txt"
         if len(rel.parts) == 1 and f.suffix.lower() == ".txt":
            continue

         # Skip binaries
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

   Comment-only differences (lines where the non-whitespace part starts
   with '#') are ignored entirely and do not produce conflicts.
   Use ConflictScanWorker for non-blocking execution in the UI.
   """
   raw = detect_file_conflicts(mods)
   result: Dict[str, FileConflict] = {}
   for rel_path, owners in raw.items():
      severity, conflicting_defs = _classify_severity(rel_path, owners)
      if severity is None:
         # No real conflict (only comments/whitespace or dependency-only)
         continue
      result[rel_path] = FileConflict(rel_path, owners, severity, conflicting_defs)
   return result


def _owners_form_dependency_chain(owners: List[Mod]) -> bool:
   """
   Return True if every pair of overlapping mods is connected by a dependency:
   i.e. for any two owners A,B, either A depends (directly) on B or B depends on A.

   This is a heuristic: we only look at the current owners and their immediate
   dependencies. If they *can* be ordered by load order to make sense, we treat
   the conflict as load-order-solvable.
   """
   if len(owners) < 2:
      return False

   # Use mod.id to compare, because Mod.dependencies may contain names or ids
   id_to_mod = {m.id: m for m in owners}
   # Build a quick lookup: mod.id -> set of ids it depends on (restricted to owners)
   depends_on: dict[str, set[str]] = {}
   for m in owners:
      deps: set[str] = set()
      for d in m.dependencies:
         # dependency may be recorded as id or name; try both
         for candidate in owners:
            if d in [candidate.id, candidate.name]:
               deps.add(candidate.id)
      if deps:
         depends_on[m.id] = deps

   # For every unordered pair (A,B), require A→B or B→A
   ids = [m.id for m in owners]
   for i, a in enumerate(ids):
      for b in ids[i + 1:]:
         deps_a = depends_on.get(a, set())
         deps_b = depends_on.get(b, set())
         if b not in deps_a and a not in deps_b:
            return False
   return True


def _strip_comments_and_blank(text: str) -> str:
   """
   Remove comments and blank lines so that comment-only edits are treated
   as no-op.

   Rules:
     • Anything after a '#' is ignored, unless the '#' is inside a
       double-quoted string.
     • Lines that become empty (only whitespace / removed comment) are
       dropped.
   """
   result: List[str] = []

   for line in text.splitlines():
      out_chars: List[str] = []
      in_string = False
      i = 0
      while i < len(line):
         ch = line[i]
         if ch == '"' and (i == 0 or line[i - 1] != "\\"):
            in_string = not in_string
            out_chars.append(ch)
         elif ch == "#" and not in_string:
            # Start of comment outside a string → ignore rest of line
            break
         else:
            out_chars.append(ch)
         i += 1

      code = "".join(out_chars).rstrip()
      if code.strip():
         result.append(code)

   return "\n".join(result)


def _is_patch_like(mod: Mod) -> bool:
   """Heuristic: treat mods with 'patch' in name/tag as overlay mods."""
   name = (mod.name or "").lower()
   if "patch" in name or "compat" in name or name.startswith("!"):
      return True
   return any("patch" in t.lower() or "compat" in t.lower() for t in mod.tags)


def _classify_severity(rel_path: str, owners: List[Mod]) -> Tuple[
   ConflictSeverity | None, List[str]]:
   """
   Determine whether a multi-mod file overlap is a HARD or SOFT conflict.

   HARD: the file is a Clausewitz text file and ≥2 mods define the same
         top-level definition key (by name/id/token/…),
         and the overlap is *not* explained purely by declared dependencies,
         and there is a real content change beyond comments/whitespace.

   SOFT: everything else (binary/non-CW, no overlapping definitions, or
         overlaps only between mods that depend on each other).

   Returns (None, []) when the overlap should be ignored entirely
   (e.g. only comment/whitespace differences).
   """
   suffix = Path(rel_path).suffix.lower()
   if suffix not in _CW_TEXT_EXTS:
      # Non-CW text: we still show as SOFT file overlap when contents differ.
      return ConflictSeverity.SOFT, []

   key_counts: Counter[str] = Counter()
   for mod in owners:
      path = mod.path / rel_path
      if not path.is_file():
         continue
      for k in _cached_definition_names(path):
         key_counts[k] += 1

   conflicting = sorted(k for k, n in key_counts.items() if n > 1)
   if not conflicting:
      # No overlapping definitions at all → at most a SOFT file overlap.
      return ConflictSeverity.SOFT, []

      # If one of the owners is a patch‑like mod, treat as SOFT — its purpose
      # is to override, not to be reported as a real conflict.
   if any(_is_patch_like(m) for m in owners):
      return ConflictSeverity.SOFT, []

      # If all owners are related by dependencies, treat as SOFT — load order can solve it.
   if _owners_form_dependency_chain(owners):
      return ConflictSeverity.SOFT, []

   # Additional check: if all owners' files are identical once comment-only
   # lines and blank lines are stripped, then this is effectively a
   # comment-only change and should be ignored completely.
   contents: set[str] = set()
   for mod in owners:
      path = mod.path / rel_path
      if not path.is_file():
         continue
      try:
         raw = path.read_text(encoding="utf-8-sig", errors="replace")
      except OSError:
         continue
      contents.add(_strip_comments_and_blank(raw))

   if len(contents) <= 1:
      # Only comment/whitespace differences → ignore entirely.
      return None, []

   return ConflictSeverity.HARD, conflicting


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
         # severity is None when the only differences are comments/whitespace.
         # Mirroring detect_file_conflicts_ex: skip these entirely.
         if severity is None:
            continue
         result[rel_path] = FileConflict(rel_path, owners, severity, conflicting_defs)

      self.progress.emit(n, n, "classifying")
      return result
