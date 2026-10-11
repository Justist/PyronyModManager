"""Public patch/flatten facade for conservative semantic interpretation.

The public API is intentionally stable for ``patch_worker.py`` and the UI.
Pyrony proves equivalence where possible; it does not invent arithmetic,
ordering, or Boolean composition choices for the user.
"""

import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List

from pmm.core.clausewitz import parse_file, unparse_pair
from pmm.core.models import Mod
from pmm.core.semantics.merge_policy import decide, DecisionKind
from pmm.core.semantics.model import SemanticContext
from pmm.core.semantics.stellaris import stellaris_registry
from pmm.core.services import _CW_TEXT_EXTS, ConflictSeverity, FileConflict

_GAME_READ_ROOT_DIRS = frozenset({
    "common", "events", "gfx", "gui", "interface", "localisation", "map",
    "music", "sound", "history", "decisions", "missions",
    "prescripted_countries", "prescripted_species_systems", "flags",
    "portraits", "fonts", "tutorial", "ambient_objects", "masks", "video",
    "assets",
})
_IGNORED_DIR_NAMES = frozenset({
    "old", "backup", "backups", "tmp", "temp", "cache", "__pycache__",
    "build",
})


@dataclass
class MergeNote:
    def_id: str
    rel_path: str
    note: str


@dataclass
class PatchResult:
    mod_dir: Path
    files_written: int
    notes: list[MergeNote] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


def _is_merge_source_file(mod_root: Path, file_path: Path) -> bool:
    if not file_path.is_file():
        return False
    relative = file_path.relative_to(mod_root)
    parts = [part.casefold() for part in relative.parts]
    return (
            bool(parts)
            and not any(part.startswith(".") for part in parts)
            and all(part not in _IGNORED_DIR_NAMES for part in parts[:-1])
            and relative.suffix.casefold() != ".mod"
            and parts[0] in _GAME_READ_ROOT_DIRS
    )


def _file_decisions(rel_path: str, paths: list[Path]) -> tuple[bool, list[MergeNote]]:
    """Return whether all definitions are equivalent and explanatory notes."""
    registry = stellaris_registry()
    documents = [parse_file(path) for path in paths]
    definitions = [document.definitions() for document in documents]
    if not definitions:
        return True, []
    if any(current.keys() != definitions[0].keys()
           for current in definitions[1:]):
        return False, []
    role = registry.role_for(rel_path)
    context = SemanticContext("stellaris", PurePosixPath(rel_path), role)
    notes: list[MergeNote] = []
    for definition_key in definitions[0]:
        versions = [current[definition_key] for current in definitions]
        if all(unparse_pair(version) == unparse_pair(versions[0])
               for version in versions[1:]):
            continue
        decision = decide(versions, context, registry)
        if decision.kind is not DecisionKind.EQUIVALENT:
            return False, notes
        notes.append(MergeNote(definition_key, rel_path, decision.reason))
    return True, notes


def build_patch_mod(conflicts: Dict[str, FileConflict], mods: List[Mod],
                    out_dir: Path, mod_name: str = "patchmod") -> PatchResult:
    """Write only necessary hard-conflict overrides.
 
    Equivalent scripted-trigger files produce no patch.  Unknown or different
    content uses the highest-priority complete source, exactly matching normal
    load order rather than synthesising an unsafe hybrid.
    """
    del mods  # Conflict owners are already in load order.
    mod_root = out_dir / mod_name
    mod_root.mkdir(parents=True, exist_ok=True)
    notes: list[MergeNote] = []
    unresolved: list[str] = []
    files_written = 0
    for rel_path, conflict in conflicts.items():
        if conflict.severity != ConflictSeverity.HARD or not conflict.owners:
            continue
        sources = [mod.path / rel_path for mod in conflict.owners]
        suffix = Path(rel_path).suffix.casefold()
        if suffix in _CW_TEXT_EXTS and all(source.is_file() for source in sources):
            try:
                equivalent, equivalence_notes = _file_decisions(rel_path, sources)
            except (OSError, ValueError) as error:
                equivalent = False
                equivalence_notes = []
                unresolved.append(rel_path)
                notes.append(MergeNote(rel_path, rel_path,
                                       f"Parser/semantic error: {error}"))
            if equivalent:
                notes.extend(equivalence_notes)
                notes.append(MergeNote(rel_path, rel_path,
                                       "Semantically equivalent; no patch emitted"))
                continue
        source = sources[-1]
        destination = mod_root / rel_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            shutil.copy2(source, destination)
            files_written += 1
            notes.append(MergeNote(
                rel_path, rel_path,
                "Highest-priority complete source selected; unsafe synthesis avoided",
            ))
    _write_descriptor(mod_root, mod_name, "patch", out_dir)
    return PatchResult(mod_root, files_written, notes, unresolved)


def build_merged_mod(mods: List[Mod], out_dir: Path,
                     mod_name: str = "merged_playset") -> PatchResult:
    """Flatten files conservatively using highest-priority complete sources.
 
    This first implementation deliberately does not claim complete emulation of
    directory-level ``replace_path``.  The definition index understands it for
    analysis; exact flattening remains gated on full CWT loading rules.
    """
    all_files: dict[str, list[Mod]] = {}
    for mod in mods:
        if not mod.path.is_dir():
            continue
        for file_path in mod.path.rglob("*"):
            if _is_merge_source_file(mod.path, file_path):
                relative = file_path.relative_to(mod.path).as_posix()
                all_files.setdefault(relative, []).append(mod)
    mod_root = out_dir / mod_name
    mod_root.mkdir(parents=True, exist_ok=True)
    notes: list[MergeNote] = []
    files_written = 0
    for rel_path, owners in all_files.items():
        destination = mod_root / rel_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = owners[-1].path / rel_path
        if source.is_file():
            shutil.copy2(source, destination)
            files_written += 1
            if len(owners) > 1:
                notes.append(MergeNote(
                    rel_path, rel_path,
                    "Highest-priority complete file selected; unsafe synthesis avoided",
                ))
    _write_descriptor(mod_root, mod_name, "merged", out_dir)
    return PatchResult(mod_root, files_written, notes, [])


def _write_descriptor(mod_root: Path, mod_name: str, kind: str,
                      outer_mod_dir: Path) -> None:
    inner = (f'name="{mod_name}"\nversion="1.0"\nsupported_version="*"\n'
             'tags={\n\t"Utilities"\n}\n'
             f'# Generated by PyronyModManager ({kind} mode)\n')
    outer = (f'name="{mod_name}"\nversion="1.0"\nsupported_version="*"\n'
             f'path="{mod_root.resolve().as_posix()}"\n'
             'tags={\n\t"Utilities"\n}\n'
             f'# Generated by PyronyModManager ({kind} mode)\n')
    (mod_root / "descriptor.mod").write_text(inner, encoding="utf-8")
    (outer_mod_dir / f"{mod_root.name}.mod").write_text(outer, encoding="utf-8")
