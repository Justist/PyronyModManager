"""
Semantic (definition-aware) merge engine for Clausewitz files.

Two public entry points:
  build_patch_mod(conflicts, mods, out_dir, mod_name)
      → writes a minimal patch mod resolving all HARD conflicts

  build_merged_mod(mods, out_dir, mod_name)
      → flattens the entire playset into one standalone mod
"""

import contextlib
import shutil
from dataclasses import dataclass, field
from enum import auto, Enum
from pathlib import Path, PurePosixPath
from typing import Dict, List, Set, Tuple

from pmm.core.clausewitz import (
   CWBlock, CWPair, CWRaw,
   parse_file, unparse, unparse_pair,
)
from pmm.core.cw_merge_utils import merge_block_items_union
from pmm.core.models import Mod
from pmm.core.services import _CW_TEXT_EXTS, ConflictSeverity, FileConflict


_GAME_READ_ROOT_DIRS = frozenset({
   "common",
   "events",
   "gfx",
   "gui",
   "interface",
   "localisation",
   "map",
   "music",
   "sound",
   "history",
   "decisions",
   "missions",
   "prescripted_countries",
   "prescripted_species_systems",
   "flags",
   "portraits",
   "fonts",
   "tutorial",
   "ambient_objects",
   "masks",
   "video",
   "assets",
})

_IGNORED_DIR_NAMES = frozenset({
   "old",
   "backup",
   "backups",
   "tmp",
   "temp",
   "cache",
   "__pycache__",
   "build",
})


def _is_merge_source_file(mod_root: Path, file_path: Path) -> bool:
   """Return True if a file should be consumed by unified-mod generation."""
   if not file_path.is_file():
      return False

   rel = file_path.relative_to(mod_root)
   parts = [p.lower() for p in rel.parts]
   if not parts:
      return False

   # Skip hidden and non-game utility folders/files.
   if any(part.startswith(".") for part in parts):
      return False
   if any(part in _IGNORED_DIR_NAMES for part in parts[:-1]):
      return False

   # A merged mod gets its own generated descriptors; do not copy .mod files.
   if rel.suffix.lower() == ".mod":
      return False

   # Only include known top-level game data folders.
   return parts[0] in _GAME_READ_ROOT_DIRS


# ── merge strategy per value type ────────────────────────────────────────────

class MergeStrategy(Enum):
   LAST_WINS = auto()  # plain override — last mod in load order wins
   NUMERIC_ADD = auto()  # additive numeric fields (modifiers, effects)
   NUMERIC_MAX = auto()  # take maximum (tech costs, thresholds)
   NUMERIC_MIN = auto()  # take minimum (cooldowns, build times)
   LIST_UNION = auto()  # merge list blocks without duplicates
   MANUAL = auto()  # tool cannot decide; emit a conflict comment


# ── Path-prefix → field-name → strategy ──────────────────────────────────────
#
# Entries are tested in order; the FIRST matching prefix wins.
# Each entry: (path_prefix_lower, {field: MergeStrategy}, default_for_numerics)
#
# The empty-string entry at the end is the unconditional fallback.
#
_PATH_FIELD_TABLE: list[
   tuple[str, dict[str, MergeStrategy], MergeStrategy]
] = [
   # ── Events / decisions / on_actions ──────────────────────────────────
   # These are scripted state machines.  Never merge field-by-field.
   ("events/", {}, MergeStrategy.LAST_WINS),
   ("common/decisions", {}, MergeStrategy.LAST_WINS),
   ("common/on_actions", {"on_action": MergeStrategy.LIST_UNION},
    MergeStrategy.LIST_UNION),

   # ── Scripted triggers / effects (usually additive) ────────────────────
   ("common/scripted_triggers", {"ai_weight": MergeStrategy.LAST_WINS},
    MergeStrategy.LIST_UNION),
   ("common/scripted_effects", {"ai_weight": MergeStrategy.LAST_WINS},
    MergeStrategy.LIST_UNION),

   # ── Localisation (just last‑wins) ─────────────────────────────────────
   ("localisation", {}, MergeStrategy.LAST_WINS),

   # ── Modifiers (Stellaris / EU4 / CK3) ─────────────────────────────────
   # Every numeric child is additive; icon/category are presentation only.
   ("common/modifiers", {"icon": MergeStrategy.LAST_WINS,
                         "category": MergeStrategy.LAST_WINS},
    MergeStrategy.NUMERIC_ADD),
   ("common/static_modifiers", {"icon": MergeStrategy.LAST_WINS},
    MergeStrategy.NUMERIC_ADD),
   ("common/opinion_modifiers", {"icon": MergeStrategy.LAST_WINS},
    MergeStrategy.NUMERIC_ADD),

   # ── Technology ─────────────────────────────────────────────────────────
   ("common/technology", {"cost": MergeStrategy.NUMERIC_MIN,
                          "research_cost": MergeStrategy.NUMERIC_MIN,
                          "time": MergeStrategy.NUMERIC_MIN,
                          "ai_chance": MergeStrategy.NUMERIC_MAX,
                          "icon": MergeStrategy.LAST_WINS},
    MergeStrategy.LAST_WINS),
   ("technologies/", {"research_cost": MergeStrategy.NUMERIC_MIN,
                      "path": MergeStrategy.LIST_UNION},
    MergeStrategy.LAST_WINS),

   # ── Buildings ──────────────────────────────────────────────────────────
   ("common/buildings", {"cost": MergeStrategy.NUMERIC_MIN,
                         "time": MergeStrategy.NUMERIC_MIN,
                         "max": MergeStrategy.NUMERIC_MAX,
                         "modifier": MergeStrategy.NUMERIC_ADD,
                         "icon": MergeStrategy.LAST_WINS},
    MergeStrategy.LAST_WINS),

   # ── Traits (CK3 / Stellaris / EU4) ────────────────────────────────────
   ("common/traits", {"opposites": MergeStrategy.LIST_UNION,
                      "prerequisites": MergeStrategy.LIST_UNION,
                      "icon": MergeStrategy.LAST_WINS},
    MergeStrategy.NUMERIC_ADD),

   # ── National focuses (HOI4) ────────────────────────────────────────────
   ("common/national_focus", {"cost": MergeStrategy.NUMERIC_MIN,
                              "prerequisite": MergeStrategy.LIST_UNION,
                              "mutually_exclusive": MergeStrategy.LIST_UNION,
                              "icon": MergeStrategy.LAST_WINS},
    MergeStrategy.LAST_WINS),

   # ── Policies / edicts ──────────────────────────────────────────────────
   ("common/policies", {"cost": MergeStrategy.NUMERIC_MIN,
                        "ai_will_do": MergeStrategy.LAST_WINS,
                        "modifier": MergeStrategy.NUMERIC_ADD},
    MergeStrategy.LAST_WINS),

   # ── Ethics / factions (Stellaris) ─────────────────────────────────────
   ("common/ethics", {"ethic_pop_modifier": MergeStrategy.NUMERIC_ADD,
                      "country_modifier": MergeStrategy.NUMERIC_ADD,
                      "icon": MergeStrategy.LAST_WINS},
    MergeStrategy.LAST_WINS),

   # ── Scripted variables / inline @-vars ────────────────────────────────
   ("common/scripted_variables", {}, MergeStrategy.LAST_WINS),

   # ── Fallback — anything not listed above ──────────────────────────────
   ("", {}, MergeStrategy.LAST_WINS),
]


def _strategy_for(key: str, rel_path: str = "") -> MergeStrategy:
   """
    Return the MergeStrategy for a field `key` inside a file at `rel_path`.

    Uses a two-level table: path prefix → {field_name: strategy}.
    Falls back to the path's default_for_numerics when the field is
    unknown but its value looks numeric; otherwise LAST_WINS.
    """
   norm = PurePosixPath(rel_path.replace("\\", "/")).as_posix().lower()
   return next(
      (field_map[key] if key in field_map else default_numeric
       for prefix, field_map, default_numeric in _PATH_FIELD_TABLE
       if not prefix or norm.startswith(prefix)),
      MergeStrategy.LAST_WINS,
   )


# ── per-definition merge ───────────────────────────────────────────────────────

@dataclass
class MergeNote:
   def_id: str
   rel_path: str
   note: str  # human-readable explanation of the decision


def _merge_scalar(key: str, values: List[str], rel_path: str = "") -> Tuple[str, str]:
   """
   Merge a list of scalar string values according to the field's strategy.
   Returns (merged_value, note).
   """
   strategy = _strategy_for(key, rel_path)

   def _to_float(v: str) -> float | None:
      with contextlib.suppress(ValueError):
         return float(v)
      return None

   nums = [_to_float(v) for v in values]
   # Keep a concrete list of floats for arithmetic once we've verified they are all numeric.
   floats = [n for n in nums if n is not None]

   if not floats or not values:
      # If any value is non-numeric, or if there are no values, we can't apply a numeric strategy.
      # Fall back to LAST_WINS but note the presence of non-numeric values.
      note = f"{strategy.name} not applicable (non-numeric values: {values})"
      return values[-1] if values else "", note

   if strategy == MergeStrategy.NUMERIC_ADD and len(floats) == len(values):
      total = sum(floats)
      result = str(int(total)) if total == int(total) else str(round(total, 6))
      return result, f"ADD {values} → {result}"

   if strategy == MergeStrategy.NUMERIC_MAX and len(floats) == len(values):
      result_f = max(floats)
      result = str(int(result_f)) if result_f == int(result_f) else str(result_f)
      return result, f"MAX {values} → {result}"

   if strategy == MergeStrategy.NUMERIC_MIN and len(floats) == len(values):
      result_f = min(floats)
      result = str(int(result_f)) if result_f == int(result_f) else str(result_f)
      return result, f"MIN {values} → {result}"

   # Default: last wins
   return values[-1], f"LAST_WINS {values} → {values[-1]}"


def _merge_pairs(
      pairs: list[CWPair],
      def_id: str,
      rel_path: str,
      notes: list[MergeNote],
) -> CWPair:
   """
   Merge multiple CWPair versions of the same definition.
   The base is the first mod's version; each subsequent mod's fields
   are applied using the strategy for that field name.
   """
   # Start from the last version (highest priority in load order)
   base = pairs[-1]
   if not isinstance(base.value, CWBlock):
      # Scalar top-level definition — apply scalar merge
      vals = [p.value for p in pairs if isinstance(p.value, str)]
      merged, note = _merge_scalar(base.key, vals)
      notes.append(MergeNote(def_id, rel_path, note))
      return CWPair(key=base.key, op=base.op, value=merged, line=0)

   # Block definition — merge field by field
   # Collect all CWPair children per key across all mods
   all_children: dict[str, list[CWPair]] = {}
   for pair in pairs:
      if not isinstance(pair.value, CWBlock):
         continue
      for item in pair.value.items:
         if isinstance(item, CWPair):
            all_children.setdefault(item.key, []).append(item)

   merged_items: List = []
   # Collect raw items (non-pair) from ALL mods, deduplicated by text content.
   # These are @-variables, blank lines, and raw comments.
   raw_seen: Set[str] = set()
   for pair in pairs:  # pairs is in load order: earliest first
      if not isinstance(pair.value, CWBlock):
         continue
      for item in pair.value.items:
         if isinstance(item, CWPair):
            continue
         item_text = item if isinstance(item, str) else unparse(item)
         if item_text not in raw_seen:
            raw_seen.add(item_text)
            merged_items.append(item)
   # Second pass: merge each field
   seen: Set[str] = set()
   for pair in pairs:
      if not isinstance(pair.value, CWBlock):
         continue
      for item in pair.value.items:
         if not isinstance(item, CWPair) or item.key in seen:
            continue
         versions = all_children.get(item.key, [item])
         if len(versions) == 1:
            merged_items.append(versions[0])
            seen.add(item.key)
            continue

         strategy = _strategy_for(item.key, rel_path)
         if strategy == MergeStrategy.LAST_WINS:
            merged_items.append(versions[-1])
            notes.append(MergeNote(
               def_id, rel_path,
               f"  field '{item.key}': LAST_WINS → {unparse(versions[-1].value)}"
            ))
         elif strategy in (
               MergeStrategy.NUMERIC_ADD,
               MergeStrategy.NUMERIC_MAX,
               MergeStrategy.NUMERIC_MIN,
         ):
            vals = [v.value for v in versions if isinstance(v.value, str)]
            merged_val, note = _merge_scalar(item.key, vals, rel_path)
            merged_items.append(
               CWPair(key=item.key, op=versions[0].op, value=merged_val, line=0)
            )
            notes.append(MergeNote(def_id, rel_path, f"  field '{item.key}': {note}"))
         elif strategy == MergeStrategy.LIST_UNION:
            # Merge block items from all versions (union, no duplicates by text)
            blocks: list[CWBlock] = [
               v.value for v in versions if isinstance(v.value, CWBlock)
            ]
            merged_block, count = merge_block_items_union(blocks)
            merged_items.append(
               CWPair(key=item.key, op=versions[0].op, value=merged_block, line=0)
            )
            notes.append(
               MergeNote(
                  def_id,
                  rel_path,
                  f"  field '{item.key}': LIST_UNION ({count} items)",
               )
            )
         else:
            # MANUAL — emit both with a # CONFLICT comment
            merged_items.extend(
               CWRaw(f"# CONFLICT version {i + 1} of {len(versions)}: "
                     f"{unparse_pair(v)}") for i, v in enumerate(versions))
            merged_items.append(versions[-1])
            notes.append(MergeNote(def_id, rel_path,
                                   f"  field '{item.key}': MANUAL — review required"))
         seen.add(item.key)

   merged_block = CWBlock(items=merged_items)
   return CWPair(key=base.key, op=base.op, value=merged_block, line=0)


# ── file-level merge ───────────────────────────────────────────────────────────

def _merge_cw_file(
      rel_path: str,
      contributing_mods: list[Mod],
      notes: list[MergeNote],
      hard_only: bool = False,
) -> str:
   per_mod_defs: List[Dict[str, CWPair]] = []
   per_mod_order: List[List[str]] = []  # ← key insertion order per mod

   for mod in contributing_mods:
      path = mod.path / rel_path
      if not path.is_file():
         per_mod_defs.append({})
         per_mod_order.append([])
         continue
      with contextlib.suppress(Exception):
         defs = parse_file(path).definitions()
         per_mod_defs.append(defs)
         per_mod_order.append(list(defs.keys()))  # preserves file order
         continue
      per_mod_defs.append({})
      per_mod_order.append([])

   # Stable merge of key orderings: use the last mod's order as the base,
   # then append keys that only exist in earlier mods (preserves their order too).
   seen: Set[str] = set()
   ordered_keys: List[str] = []
   # Walk last mod first for the primary order, then earlier mods for extras
   for key_list in reversed(per_mod_order):
      for k in key_list:
         if k not in seen:
            seen.add(k)
            ordered_keys.append(k)

   lines: List[str] = [f"# Merged by PyronyModManager — {rel_path}\n"]

   for def_key in ordered_keys:  # ← was sorted(all_keys)
      versions = [d[def_key] for d in per_mod_defs if def_key in d]
      if hard_only and len(versions) < 2:
         continue
      merged = _merge_pairs(versions, def_key, rel_path, notes)
      lines.extend((unparse_pair(merged), ""))

   return "\n".join(lines)


# ── Mode A: patch mod ──────────────────────────────────────────────────────────

@dataclass
class PatchResult:
   mod_dir: Path
   files_written: int
   notes: list[MergeNote] = field(default_factory=list)
   unresolved: list[str] = field(default_factory=list)  # MANUAL items


def build_patch_mod(
      conflicts: Dict[str, FileConflict],
      mods: List[Mod],  # full load-ordered list
      out_dir: Path,
      mod_name: str = "patchmod",
) -> PatchResult:
   """
   Write a minimal patch mod to out_dir/mod_name/ that resolves all HARD
   conflicts by merging the conflicting definitions semantically.

   Only files with HARD conflicts are written.  Non-conflicting files from
   the other mods are untouched.
   """
   mod_root = out_dir / mod_name
   mod_root.mkdir(parents=True, exist_ok=True)

   notes: list[MergeNote] = []
   files_written = 0

   hard_conflicts = {
      k: v for k, v in conflicts.items()
      if v.severity == ConflictSeverity.HARD
   }

   for rel_path, fc in hard_conflicts.items():
      suffix = Path(rel_path).suffix.lower()
      if suffix not in _CW_TEXT_EXTS:
         # Binary or non-CW conflict — just copy the highest-priority version
         src = fc.owners[-1].path / rel_path
         dst = mod_root / rel_path
         dst.parent.mkdir(parents=True, exist_ok=True)
         if src.is_file():
            shutil.copy2(src, dst)
            files_written += 1
         continue

      merged_text = _merge_cw_file(rel_path, fc.owners, notes, hard_only=True)
      dst = mod_root / rel_path
      dst.parent.mkdir(parents=True, exist_ok=True)
      dst.write_text(merged_text, encoding="utf-8")
      files_written += 1

   # Write descriptor
   _write_descriptor(mod_root, mod_name, "patch", out_dir)
   unresolved = [n.def_id for n in notes if "MANUAL" in n.note]
   return PatchResult(mod_root, files_written, notes, unresolved)


# ── Mode B: merged (flattened) standalone mod ─────────────────────────────────

def build_merged_mod(
      mods: List[Mod],
      out_dir: Path,
      mod_name: str = "merged_playset",
) -> PatchResult:
   """
   Flatten the entire playset into a single standalone mod.

   For every game-relevant file present in any mod:
     • CW text files → definition-level merge
     • Binary / non-CW files → highest-priority (last mod) wins
   """
   # Collect all unique relative paths across all mods
   all_files: dict[str, list[Mod]] = {}
   for mod in mods:
      if not mod.path.is_dir():
         continue
      for f in mod.path.rglob("*"):
         if not _is_merge_source_file(mod.path, f):
            continue
         rel = str(f.relative_to(mod.path))
         all_files.setdefault(rel, []).append(mod)

   mod_root = out_dir / mod_name
   mod_root.mkdir(parents=True, exist_ok=True)
   notes: list[MergeNote] = []
   files_written = 0

   for rel_path, owners in all_files.items():
      dst = mod_root / rel_path
      dst.parent.mkdir(parents=True, exist_ok=True)
      suffix = Path(rel_path).suffix.lower()

      if len(owners) == 1 or suffix not in _CW_TEXT_EXTS:
         # Single owner or binary — copy highest priority
         src = owners[-1].path / rel_path
         if src.is_file():
            shutil.copy2(src, dst)
            files_written += 1
      else:
         # Multiple owners + CW text — full semantic merge
         merged_text = _merge_cw_file(rel_path, owners, notes, hard_only=False)
         dst.write_text(merged_text, encoding="utf-8")
         files_written += 1

   _write_descriptor(mod_root, mod_name, "merged", out_dir)
   unresolved = [n.def_id for n in notes if "MANUAL" in n.note]
   return PatchResult(mod_root, files_written, notes, unresolved)


# ── descriptor writer ──────────────────────────────────────────────────────────

def _write_descriptor(mod_root: Path, mod_name: str, kind: str, outer_mod_dir: Path) -> None:
   """
      Write the internal descriptor.mod and the outer <folder>.mod descriptor.

      Paradox (and Pyrony) require a .mod file in the mod directory; the mod's
      own descriptor.mod is used by the launcher.
      """
   inner_desc_text = f"""name="{mod_name}"
version="1.0"
supported_version="*"
tags={{
\t"Utilities"
}}
# Generated by PyronyModManager ({kind} mode)
"""
   outer_desc_text = f"""name="{mod_name}"
version="1.0"
supported_version="*"
path="{mod_root.resolve().as_posix()}"
tags={{
\t"Utilities"
}}
# Generated by PyronyModManager ({kind} mode)
"""

   # Internal descriptor.mod inside the mod folder
   (mod_root / "descriptor.mod").write_text(inner_desc_text, encoding="utf-8")

   # Outer <folder>.mod descriptor in the mod directory
   folder = mod_root.name
   outer = outer_mod_dir / f"{folder}.mod"
   outer.write_text(outer_desc_text, encoding="utf-8")
