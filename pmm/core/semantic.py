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
from pathlib import Path
from typing import Dict, List, Tuple

from pmm.core.clausewitz import (
   CWBlock, CWPair, CWRaw,
   parse_file, unparse, unparse_pair,
)
from pmm.core.cw_merge_utils import merge_block_items_union
from pmm.core.models import Mod
from pmm.core.services import _CW_TEXT_EXTS, ConflictSeverity, FileConflict


# ── merge strategy per value type ────────────────────────────────────────────

class MergeStrategy(Enum):
   LAST_WINS = auto()  # plain override — last mod in load order wins
   NUMERIC_ADD = auto()  # additive numeric fields (modifiers, effects)
   NUMERIC_MAX = auto()  # take maximum (tech costs, thresholds)
   NUMERIC_MIN = auto()  # take minimum (cooldowns, build times)
   LIST_UNION = auto()  # merge list blocks without duplicates
   MANUAL = auto()  # tool cannot decide; emit a conflict comment


# Field names whose values should be ADDED across mods
_ADDITIVE_FIELDS = frozenset({
   # Stellaris modifiers
   "pop_growth_speed", "ship_fire_rate_mult", "country_resource_max_add",
   "planet_building_cost_mult", "ship_hull_mult", "army_damage_mult",
   # HOI4 modifiers
   "production_speed_buildings_factor", "research_time_factor",
   "stability_factor", "war_support_factor",
   # EU4 modifiers
   "global_tax_modifier", "global_manpower_modifier", "stability_cost_modifier",
   # CK3
   "monthly_prestige", "monthly_piety", "fertility",
   # Generic
   "add", "factor",
})

# Field names that should take the maximum
_MAX_FIELDS = frozenset({
   "max_speed", "max_range", "max_manpower", "max_firerate",
   "max_count", "ai_chance",
})

# Field names that should take the minimum
_MIN_FIELDS = frozenset({
   "cost", "build_time", "days_of_supply", "cooldown",
   "research_cost", "production_cost",
})

# Keys whose block children should be union-merged (list-like containers)
_LIST_BLOCK_KEYS = frozenset({
   "potential", "trigger", "allow", "on_action",
   "add_trait", "remove_trait",
})


def _strategy_for(key: str) -> MergeStrategy:
   if key in _ADDITIVE_FIELDS:
      return MergeStrategy.NUMERIC_ADD
   if key in _MAX_FIELDS:
      return MergeStrategy.NUMERIC_MAX
   if key in _MIN_FIELDS:
      return MergeStrategy.NUMERIC_MIN
   if key in _LIST_BLOCK_KEYS:
      return MergeStrategy.LIST_UNION
   return MergeStrategy.LAST_WINS


# ── per-definition merge ───────────────────────────────────────────────────────

@dataclass
class MergeNote:
   def_id: str
   rel_path: str
   note: str  # human-readable explanation of the decision


def _merge_scalar(key: str, values: list[str]) -> Tuple[str, str]:
   """
   Merge a list of scalar string values according to the field's strategy.
   Returns (merged_value, note).
   """
   strategy = _strategy_for(key)

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

   merged_items: list = []
   # First pass: collect raw (non-pair) items from the last mod
   merged_items.extend(item for item in base.value.items if not isinstance(item, CWPair))
   # Second pass: merge each field
   seen: set[str] = set()
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

         strategy = _strategy_for(item.key)
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
            merged_val, note = _merge_scalar(item.key, vals)
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
      contributing_mods: list[Mod],  # in load order (first = lowest priority)
      notes: list[MergeNote],
      hard_only: bool = False,  # patch mode: only emit conflicting defs
) -> str:
   """
   Merge one Clausewitz file from multiple mods.
   Returns the merged file text.
   """
   per_mod_defs: list[dict[str, CWPair]] = []
   for mod in contributing_mods:
      path = mod.path / rel_path
      if not path.is_file():
         per_mod_defs.append({})
         continue
      with contextlib.suppress(Exception):
         per_mod_defs.append(parse_file(path).definitions())
         continue
      per_mod_defs.append({})

   # All definition keys across all mods
   all_keys: set[str] = set()
   for d in per_mod_defs:
      all_keys.update(d.keys())

   lines: list[str] = [f"# Merged by PyronyModManager — {rel_path}\n"]

   for def_key in sorted(all_keys):
      versions: list[CWPair] = [
         d[def_key] for d in per_mod_defs if def_key in d
      ]
      if hard_only and len(versions) < 2:
         continue  # patch mode: skip defs that only one mod defines

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

   For every file present in any mod:
     • CW text files → definition-level merge
     • Binary / non-CW files → highest-priority (last mod) wins
   """
   # Collect all unique relative paths across all mods
   all_files: dict[str, list[Mod]] = {}
   for mod in mods:
      if not mod.path.is_dir():
         continue
      for f in mod.path.rglob("*"):
         if not f.is_file():
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
   desc_text = (
      f'name="{mod_name}"\n'
      f'version="1.0"\n'
      f'supported_version="*"\n'
      f'tags={{\n\t"Utilities"\n}}\n'
      f'# Generated by PyronyModManager ({kind} mode)\n'
   )

   # Internal descriptor.mod inside the mod folder
   (mod_root / "descriptor.mod").write_text(desc_text, encoding="utf-8")

   # Outer <folder>.mod descriptor in the mod directory
   folder = mod_root.name
   outer = outer_mod_dir / f"{folder}.mod"
   outer.write_text(desc_text, encoding="utf-8")
