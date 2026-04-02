import contextlib
import difflib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from pmm_clausewitz import CWPair
from pmm_models import Mod, ModCollection


# ── existing functions (unchanged) ───────────────────────────────────────────

def resolve_load_order(mods: list[Mod], collection: ModCollection) -> list[Mod]:
    """Return mods in collection order, filtering to enabled only."""
    by_id = {m.id: m for m in mods}
    ordered = [by_id[mid] for mid in collection.mods if mid in by_id]
    return [m for m in ordered if m.enabled]


def detect_file_conflicts(mods: list[Mod]) -> dict[str, list[Mod]]:
    """
    Walk each mod's directory and map relative_path → [mods that provide it].
    Returns only paths present in more than one mod.
    """
    file_owners: dict[str, list[Mod]] = defaultdict(list)
    for mod in mods:
        if not mod.path.is_dir():
            continue
        for f in mod.path.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(mod.path))
                file_owners[rel].append(mod)
    return {k: v for k, v in file_owners.items() if len(v) > 1}


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


# ── Phase 4: deep conflict analysis ──────────────────────────────────────────

@dataclass
class DefinitionDiff:
    """
    Describes a single top-level definition that differs between two mod files.

    status:
      "changed"   – present in both mods but with different content
      "only_in_a" – present only in mod_a
      "only_in_b" – present only in mod_b (new definition added by mod_b)
    """
    def_id:  str
    status:  str
    text_a:  str   # unparse_pair output from mod_a, or ""
    text_b:  str   # unparse_pair output from mod_b, or ""


def get_unified_diff(rel_path: str, mod_a: Mod, mod_b: Mod) -> str:
    """
    Return a unified diff string comparing rel_path as found in mod_a vs mod_b.
    Returns an empty string if either file is missing or unreadable.
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
            lines_a,
            lines_b,
            fromfile=f"{mod_a.name}/{rel_path}",
            tofile=f"{mod_b.name}/{rel_path}",
        )
    )


def get_definition_diffs(rel_path: str, mod_a: Mod, mod_b: Mod) -> list[DefinitionDiff]:
    """
    Parse rel_path from both mods with the Clausewitz parser and return a list
    of top-level definitions that differ between them.

    Binary files (DDS, OGG, TGA, …) are not parsed; they return a single
    placeholder entry so the diff tab still appears.
    """
    from pmm_clausewitz import parse_text, unparse_pair

    _TEXT_EXTS = {
        ".txt", ".cfg", ".gui", ".gfx", ".asset", ".sfx",
        ".mod", ".map", ".csv", ".yml", ".yaml",
    }
    suffix = Path(rel_path).suffix.lower()
    if suffix not in _TEXT_EXTS:
        return [DefinitionDiff(
            def_id="<binary>",
            status="changed",
            text_a=f"(binary file in {mod_a.name})",
            text_b=f"(binary file in {mod_b.name})",
        )]

    defs_a: dict[str, CWPair] = {}
    defs_b: dict[str, CWPair] = {}
    path_a = mod_a.path / rel_path
    path_b = mod_b.path / rel_path

    def _load_defs(path: Path, target: dict[str, object]) -> None:
       if not path.is_file():
          return
       with contextlib.suppress(Exception):
          text = path.read_text(encoding="utf-8-sig", errors="replace")
          target.update(parse_text(text, path).definitions())

    _load_defs(path_a, defs_a)
    _load_defs(path_b, defs_b)

    result: list[DefinitionDiff] = []
    all_keys = sorted(set(defs_a) | set(defs_b))
    for key in all_keys:
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