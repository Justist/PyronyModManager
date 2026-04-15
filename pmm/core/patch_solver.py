"""
Conflict Solver — generates a "patch mod" that resolves HARD conflicts
between Paradox game mods without requiring the user to have a full
Clausewitz text editor.

Approach
--------
A patch mod is a normal Paradox mod that sits at the very end of the
load order (highest priority) and re-declares only the conflicting
definitions.  Because Paradox uses "last writer wins" for top-level
definitions within a file, the patch mod transparently overrides earlier
mods.

For each HARD-conflicting file Pyrony:

  1. Gathers the definition from every owning mod, in load order.
  2. For each conflicting definition key, presents an auto-resolution:
       • If every mod took the same definition verbatim from vanilla (i.e.
         the definition also exists with the same text in the base game),
         pick the "winning" mod (last in load order) automatically.
       • Otherwise, emit a ResolutionTask that the UI can present to the
         user (or that can be auto-accepted in headless mode).
  3. Writes the chosen definitions into
       <user_data>/mod/<patch_name>/common/<subpath>.txt
     plus a minimal descriptor.mod.

Non-CW text files (yml, csv, json, lua …) are never deep-merged here;
they always follow load order (last mod wins).  The UI can still show
their diff so the user can adjust load order manually.

Binary files are ignored entirely.

Auto-resolution heuristics
---------------------------
PICK_LAST   – accept the definition from the last mod in load order.
              Used when no "vanilla ancestor" is available.
PICK_FIRST  – accept the definition from the first mod in load order.
              Used when the user prefers "base takes precedence."
MERGE_ALL   – union of all unique pairs inside the block (additive defs).
              Used automatically for list-like top-level keys (e.g.
              `@variables`, `scripted_variables`, `on_action`).
MANUAL      – emit a ResolutionTask so the UI can let the user choose.

Design notes
------------
• No external merge tool is needed.
• An external text editor IS supported: generate the skeleton patch and
  open the conflicting file in $EDITOR (or VS Code) for manual editing.
• The patch mod is a real mod directory — it can be zipped and shared.
• The solver is UI-independent; it returns ResolutionTask objects that
  the UI drives.
"""

import contextlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from enum import auto, Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pmm.core.clausewitz import (
   CWBlock, CWPair, parse_file, unparse, unparse_pair,
)
from pmm.core.cw_merge_utils import merge_block_items_union
from pmm.core.models import Mod
from pmm.core.semantic import _strategy_for as _semantic_field_strategy, MergeStrategy
from pmm.core.services import (_CW_TEXT_EXTS, ConflictSeverity, FileConflict)


# ── Resolution strategy ───────────────────────────────────────────────────────

class Strategy(Enum):
   PICK_FIRST = auto()  # take definition from mod earliest in load order
   PICK_LAST = auto()  # take definition from mod latest in load order (default)
   MERGE_ALL = auto()  # union of all top-level items (for additive blocks)
   MANUAL = auto()  # user must pick or edit


# Top-level Clausewitz keys that are additive lists — we can safely union
# them rather than forcing the user to choose one.
_ADDITIVE_KEYS = frozenset({
   "scripted_variables",
   "on_actions",  # Stellaris / HOI4
   "on_action",  # CK3
   "namespace",  # event namespace declarations
   "@",  # @-variable blocks
})

# Map semantic MergeStrategy → solver Strategy
_SEMANTIC_TO_SOLVER: dict[MergeStrategy, Strategy] = {
    MergeStrategy.LAST_WINS:   Strategy.PICK_LAST,
    MergeStrategy.NUMERIC_ADD: Strategy.MERGE_ALL,
    MergeStrategy.NUMERIC_MAX: Strategy.MERGE_ALL,
    MergeStrategy.NUMERIC_MIN: Strategy.MERGE_ALL,
    MergeStrategy.LIST_UNION:  Strategy.MERGE_ALL,
    MergeStrategy.MANUAL:      Strategy.MANUAL,
}


def _suggest_strategy(
    def_key: str,
    versions: list[DefinitionVersion],
    rel_path: str = "",
) -> Strategy:
   """
    Infer the best resolution strategy for a conflicting definition.

    Steps:
    1. Explicit additive top-level key → MERGE_ALL (preserves existing behaviour)
    2. If the definition is a scalar, ask the semantic layer for its field strategy.
    3. If the definition is a block, inspect what fraction of its fields are
       additive/min/max.  If ≥50% are non-LAST_WINS, prefer MERGE_ALL so the
       semantic _merge_pairs path handles it instead of showing a PICK dialog.
    4. Fall back to PICK_LAST.
    """
   outer_key = def_key.split(".")[0].split("@")[0]
   if outer_key in _ADDITIVE_KEYS:
       return Strategy.MERGE_ALL

   if not versions:
       return Strategy.PICK_LAST

   base_pair = versions[-1].pair

   # Scalar top-level definition
   if not isinstance(base_pair.value, CWBlock):
       sem = _semantic_field_strategy(outer_key, rel_path)
       return _SEMANTIC_TO_SOLVER.get(sem, Strategy.PICK_LAST)

   # Block definition — sample the child fields of the last version
   block: CWBlock = base_pair.value
   child_pairs = [item for item in block.items if isinstance(item, CWPair)]
   if not child_pairs:
       return Strategy.PICK_LAST

   non_last_wins = sum(
       _semantic_field_strategy(p.key, rel_path) != MergeStrategy.LAST_WINS
       for p in child_pairs)
   if non_last_wins / len(child_pairs) >= 0.5:
       return Strategy.MERGE_ALL

   return Strategy.PICK_LAST


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class DefinitionVersion:
   """One mod's version of a single definition."""
   mod: Mod
   pair: CWPair  # the parsed definition
   text: str  # unparse_pair(pair) — canonical string


@dataclass
class ResolutionTask:
   """
   A single conflicting definition that needs a resolution decision.

   rel_path        – relative file path inside the mod (e.g. common/events/foo.txt)
   def_key         – stable definition key (e.g. "country_event.100")
   versions        – one entry per owning mod, in load order
   auto_strategy   – suggested auto-resolution (PICK_LAST unless additive)
   chosen_text     – the canonical text that will be written to the patch.
                     Set by the UI or by apply_auto_resolutions().
   resolved        – True once chosen_text is set
   """
   rel_path: str
   def_key: str
   versions: List[DefinitionVersion]
   auto_strategy: Strategy = Strategy.PICK_LAST
   chosen_text: str = ""
   resolved: bool = False

   @property
   def mod_names(self) -> List[str]:
      return [v.mod.name for v in self.versions]

   def resolve_auto(self) -> None:
      """Apply the auto_strategy to set chosen_text."""
      if self.auto_strategy == Strategy.PICK_FIRST:
         self.chosen_text = self.versions[0].text
      elif self.auto_strategy == Strategy.PICK_LAST:
         self.chosen_text = self.versions[-1].text
      elif self.auto_strategy == Strategy.MERGE_ALL:
         self.chosen_text = _merge_additive(self.versions)
      # MANUAL: do nothing — UI must set chosen_text
      if self.auto_strategy != Strategy.MANUAL:
         self.resolved = True

   def resolve_pick(self, mod: Mod) -> None:
      """Resolve by picking the version from a specific mod."""
      for v in self.versions:
         if v.mod is mod:
            self.chosen_text = v.text
            self.resolved = True
            return
      raise ValueError(f"Mod {mod.name!r} is not an owner of {self.def_key!r}")

   def resolve_custom(self, text: str) -> None:
      """Resolve with hand-edited text (from external editor or inline edit)."""
      self.chosen_text = text
      self.resolved = True


@dataclass
class FilePatchPlan:
   """All resolution tasks for a single conflicting file."""
   rel_path: str
   tasks: List[ResolutionTask] = field(default_factory=list)

   @property
   def all_resolved(self) -> bool:
      return all(t.resolved for t in self.tasks)

   @property
   def hard_count(self) -> int:
      return len(self.tasks)


# ── Merge helpers ─────────────────────────────────────────────────────────────

def _merge_additive(versions: List[DefinitionVersion]) -> str:
   """
   Union all inner items from every version of an additive block.

   Duplicate items (same text) are deduplicated.
   Items are emitted in the order they first appear, across versions in
   load order.
   """
   blocks: List[CWBlock] = []
   blocks.extend(v.pair.value for v in versions
                 if isinstance(v.pair.value, CWBlock))
   if not blocks:
      return versions[-1].text  # fallback to last version string

   merged_block, _ = merge_block_items_union(blocks)
   pair = versions[-1].pair
   return f"{pair.key} {pair.op} {unparse(merged_block)}"


# ── Core analysis ─────────────────────────────────────────────────────────────

def build_patch_plan(
      conflicts: Dict[str, FileConflict],
      mods_in_order: List[Mod],
) -> List[FilePatchPlan]:
   """
   Build a list of FilePatchPlans from the detected conflicts.

   Only HARD conflicts (definition-level) produce ResolutionTasks.
   SOFT conflicts (file-only) are skipped — load order is sufficient.

   mods_in_order must be the full ordered list from resolve_load_order()
   so that version ordering (first/last) is meaningful.
   """
   plans: List[FilePatchPlan] = []

   for rel_path, fc in sorted(conflicts.items()):
      if fc.severity != ConflictSeverity.HARD:
         continue

      suffix = Path(rel_path).suffix.lower()
      if suffix not in _CW_TEXT_EXTS:
         continue

      plan = _plan_for_file(rel_path, fc.owners, mods_in_order, fc.conflicting_defs)
      if plan.tasks:
         plans.append(plan)

   return plans


def _plan_for_file(
      rel_path: str,
      owners: List[Mod],
      load_order: List[Mod],
      conflicting_def_keys: List[str],
) -> FilePatchPlan:
   plan = FilePatchPlan(rel_path=rel_path)

   # Sort owners into canonical load order.
   order_idx = {m.id: i for i, m in enumerate(load_order)}
   sorted_owners = sorted(owners, key=lambda m: order_idx.get(m.id, 999))

   # Parse each owner's version of the file.
   defs_by_mod: Dict[str, Dict[str, CWPair]] = {}
   for mod in sorted_owners:
      path = mod.path / rel_path
      with contextlib.suppress(Exception):
         defs_by_mod[mod.id] = parse_file(path).definitions()

   for def_key in conflicting_def_keys:
      versions: List[DefinitionVersion] = []
      for mod in sorted_owners:
         pair = defs_by_mod.get(mod.id, {}).get(def_key)
         if pair is not None:
            versions.append(DefinitionVersion(
               mod=mod,
               pair=pair,
               text=unparse_pair(pair),
            ))

      if len(versions) < 2:
         continue

      # Deduplicate: if all versions have the same text, no task needed.
      unique_texts = {v.text for v in versions}
      if len(unique_texts) == 1:
         continue

      # If only the last version differs and all earlier ones are identical,
      # we can safely treat it as "last-mod-wins" without bothering the user.
      if len(unique_texts) == 2:
         first_text = versions[0].text
         if all(v.text == first_text for v in versions[:-1]):
            # Let auto_strategy PICK_LAST handle it; no manual task needed.
            continue

      # Determine strategy.
      strategy = _suggest_strategy(def_key, versions, rel_path)

      task = ResolutionTask(
         rel_path=rel_path,
         def_key=def_key,
         versions=versions,
         auto_strategy=strategy,
      )
      plan.tasks.append(task)

   return plan

# ── Auto-resolution pass ──────────────────────────────────────────────────────

def apply_auto_resolutions(plans: List[FilePatchPlan]) -> Tuple[int, int]:
   """
   Apply the auto_strategy to every ResolutionTask.

   Returns (auto_resolved, manual_needed).
   MANUAL tasks are left with resolved=False so the UI knows to show them.
   """
   auto_resolved = 0
   manual_needed = 0

   for plan in plans:
      for task in plan.tasks:
         if task.auto_strategy == Strategy.MANUAL:
            manual_needed += 1
         else:
            task.resolve_auto()
            auto_resolved += 1

   return auto_resolved, manual_needed


# ── Patch mod writer ──────────────────────────────────────────────────────────

_PATCH_DESCRIPTOR_TEMPLATE = """\
name = "{name}"
version = "1.0"
supported_version = "*"
path = "{path}"
tags = {{
    "Mod Manager"
}}
"""


def write_patch_mod(
      plans: List[FilePatchPlan],
      patch_name: str,
      game_user_data: Path,
      *,
      overwrite: bool = False,
) -> Path:
   """
   Write the patch mod to disk.

   Directory layout:
       <game_user_data>/mod/<patch_folder>/
           descriptor.mod            (also written to mod/<patch_folder>.mod)
           common/                   (or whatever subdirectory rel_path uses)
               .../<file>.txt

   Only resolved tasks are written.  Unresolved tasks are silently skipped
   (they will still be handled by load order).

   Returns the path to the patch mod's root directory.

   Raises FileExistsError if the directory already exists and overwrite=False.
   """
   folder = safe_folder_name(patch_name)
   patch_root = game_user_data / "mod" / folder

   if patch_root.exists() and not overwrite:
      raise FileExistsError(
         f"Patch mod directory already exists: {patch_root}\n"
         "Pass overwrite=True to regenerate."
      )

   patch_root.mkdir(parents=True, exist_ok=True)

   # Group tasks by rel_path.
   tasks_by_file: Dict[str, List[ResolutionTask]] = {}
   for plan in plans:
      for task in plan.tasks:
         if not task.resolved:
            continue
         tasks_by_file.setdefault(task.rel_path, []).append(task)

   for rel_path, tasks in tasks_by_file.items():
      out_path = patch_root / rel_path
      out_path.parent.mkdir(parents=True, exist_ok=True)
      lines = [
         f"# Pyrony patch — auto-generated for {rel_path}",
         f"# Resolves {len(tasks)} definition conflict(s)",
         "",
      ]
      for task in tasks:
         lines.extend((
            f"# {task.def_key}  [source: {_pick_source(task)}]",
            task.chosen_text,
            "",
         ))
      out_path.write_text("\n".join(lines), encoding="utf-8")

   # Write descriptor.mod (inside mod dir).
   descriptor_text = _PATCH_DESCRIPTOR_TEMPLATE.format(
      name=patch_name, path=patch_root.resolve().as_posix()
   )
   (patch_root / "descriptor.mod").write_text(descriptor_text, encoding="utf-8")

   # Also write <folder>.mod in the mod directory (what the launcher needs).
   outer_dot_mod = game_user_data / "mod" / f"{folder}.mod"
   outer_dot_mod.write_text(descriptor_text, encoding="utf-8")

   return patch_root


def _pick_source(task: ResolutionTask) -> str:
   """Return a human-readable label for where chosen_text came from."""
   return next(
      (v.mod.name for v in task.versions if v.text == task.chosen_text),
      "custom edit",
   )


def safe_folder_name(name: str) -> str:
   """Convert a human-readable patch name to a safe directory name."""
   import re
   return re.sub(r"[^\w-]+", "_", name).strip("_").lower() or "patch"


def patch_name_for_collection(coll_name: str) -> str:
   """
   Return the canonical patch mod name for a collection:
   "Pyrony_<playset_name>_patch", with spaces replaced by underscores.
   """
   base = coll_name.replace(" ", "_")
   return f"Pyrony_{base}_patch"


# ── External editor integration ───────────────────────────────────────────────

def open_in_editor(path: Path, editor: Optional[str] = None) -> None:
   """
   Open a file in the user's preferred text editor.

   Resolution order:
     1. The `editor` argument (if given)
     2. $VISUAL environment variable
     3. $EDITOR environment variable
     4. VS Code (`code`) if available on PATH
     5. Platform default (xdg-open / open / notepad)
   """
   chosen: str | None = (
         editor
         or os.environ.get("VISUAL")
         or os.environ.get("EDITOR")
   )

   if not chosen:
      # Try VS Code
      if sys.platform == "win32":
         probe = subprocess.run(
            ["where", "code"], capture_output=True, text=True, check=False
         )
         if probe.returncode == 0 and probe.stdout.strip():
            chosen = "code"
      else:
         probe = subprocess.run(
            ["which", "code"], capture_output=True, text=True, check=False
         )
         if probe.returncode == 0 and probe.stdout.strip():
            code_cmd = probe.stdout.strip().splitlines()[0]
            chosen = code_cmd

   if chosen:
      cmd = os.fspath(chosen)
      subprocess.Popen([cmd, str(path)])
      return

   # Platform fallback
   if sys.platform == "win32":
      os.startfile(str(path))  # type: ignore[attr-defined]
   elif sys.platform == "darwin":
      subprocess.Popen(["open", str(path)])
   else:
      subprocess.Popen(["xdg-open", str(path)])
