"""
pmm.core.playset_io
===================
Import and export Paradox Launcher playset JSON files and
full-playset ZIP archives.

JSON format  (Paradox Launcher-compatible)
------------------------------------------
{
  "name":    "My Collection",
  "game":    "stellaris",
  "version": "1",
  "mods": [
    {
      "gameRegistryId": "mod/ugc_2007364284.mod",
      "steamId":        "2007364284",
      "name":           "Awesome Mod",
      "enabled":        true,
      "position":       0
    },
    …
  ]
}

ZIP format
----------
<collection_name>.zip
├── playset.json          ← same structure as above
└── mods/
    ├── <mod_folder>/     ← full content of each mod directory
    │   ├── descriptor.mod
    │   └── …
    └── …

Naming conventions
------------------
Export  →  filename stem = sanitized collection name
           "My Collection" → "My Collection.json" / "My Collection.zip"
Import  →  collection name = filename stem (before first dot)
           "elephant.json" → collection named "elephant"
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Callable, Dict, List

from PySide6.QtCore import QThread, Signal

from pmm.core.models import Mod, ModCollection


# ── filename helpers ──────────────────────────────────────────────────────────

def _safe_stem(name: str) -> str:
   """Strip characters that are forbidden in Windows filenames."""
   return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "playset"


def suggested_json_filename(collection: ModCollection) -> str:
   return f"{_safe_stem(collection.name)}.json"


def suggested_zip_filename(collection: ModCollection) -> str:
   return f"{_safe_stem(collection.name)}.zip"


# ── mod identity helpers ──────────────────────────────────────────────────────

def _game_registry_id(mod: Mod) -> str:
   """
   The identifier used in dlc_load.json and the mods table:
     Workshop mods  →  "mod/ugc_{remote_id}.mod"
     Local mods     →  "mod/{descriptor_filename}"
   """
   if mod.remote_id:
      return f"mod/ugc_{mod.remote_id}.mod"
   return f"mod/{mod.descriptor_path.name}"


def _build_lookup_dicts(
      all_mods: List[Mod],
) -> tuple[Dict[str, Mod], Dict[str, Mod]]:
   """Return (by_gameRegistryId, by_steamId) lookup dicts."""
   by_gid = {_game_registry_id(m): m for m in all_mods}
   by_steam = {m.remote_id: m for m in all_mods if m.remote_id}
   return by_gid, by_steam


# ── ImportResult ──────────────────────────────────────────────────────────────

class ImportResult:
   """
   Returned by both import_launcher_json and import_playset_zip.

   collection      — the ModCollection to add to Preferences
   matched         — mods resolved against the installed set
   unmatched       — gameRegistryId strings not found locally
   extracted_mods  — folder names extracted from a ZIP (empty for JSON)
   skipped_mods    — folders skipped because they already existed and
                     overwrite=False was passed (ZIP only)
   """

   def __init__(
         self,
         collection: ModCollection,
         matched: int,
         unmatched: list[str],
         extracted_mods: list[str],
         skipped_mods: list[str] | None = None,
   ) -> None:
      self.collection = collection
      self.matched = matched
      self.unmatched = unmatched
      self.extracted_mods = extracted_mods
      self.skipped_mods = skipped_mods or []

   @property
   def ok(self) -> bool:
      return self.matched > 0 or bool(self.extracted_mods)

   def summary(self) -> str:
      parts: list[str] = []
      if self.matched:
         parts.append(f"{self.matched} mod(s) matched")
      if self.extracted_mods:
         parts.append(f"{len(self.extracted_mods)} extracted from ZIP")
      if self.skipped_mods:
         parts.append(f"{len(self.skipped_mods)} skipped (already installed)")
      if self.unmatched:
         parts.append(f"{len(self.unmatched)} not found locally")
      return "; ".join(parts) if parts else "no mods found"


# ── JSON export ───────────────────────────────────────────────────────────────

def export_launcher_json(
      collection: ModCollection,
      all_mods: List[Mod],
      dest_path: Path,
) -> None:
   """
   Write *collection* as a Paradox Launcher-compatible JSON file.

   *dest_path* should end with '.json'.  Its parent directory must exist.
   """
   by_id = {m.id: m for m in all_mods}
   entries: list[dict] = []
   for pos, mid in enumerate(collection.mods):
      mod = by_id.get(mid)
      if mod is None:
         continue
      entry: dict = {
         "gameRegistryId": _game_registry_id(mod),
         "name": mod.name,
         "enabled": True,
         "position": pos,
      }
      if mod.remote_id:
         entry["steamId"] = mod.remote_id
      entries.append(entry)

   payload = {
      "name": collection.name,
      "game": collection.game_id,
      "version": "1",
      "mods": entries,
   }
   dest_path.write_text(
      json.dumps(payload, indent=2, ensure_ascii=False),
      encoding="utf-8",
   )


# ── JSON import ───────────────────────────────────────────────────────────────

def import_launcher_json(
      path: Path,
      game_id: str,
      all_mods: List[Mod],
      collection_name: str | None = None,
) -> ImportResult:
   """
   Read a Paradox Launcher playset JSON and create a ModCollection.

   *collection_name* defaults to the file's stem
     e.g.  'elephant.json'  →  collection named 'elephant'

   Mods are matched against *all_mods* by gameRegistryId then steamId.
   Unmatched entries are recorded in ImportResult.unmatched but are not
   added to the collection (they may not be installed yet).
   """
   name = collection_name or path.stem
   data = json.loads(path.read_text(encoding="utf-8"))

   raw_entries: list[dict] = data.get("mods", [])
   raw_entries.sort(key=lambda e: e.get("position", 0))

   by_gid, by_steam = _build_lookup_dicts(all_mods)

   ordered_ids: list[str] = []
   unmatched: list[str] = []

   for entry in raw_entries:
      gid = entry.get("gameRegistryId", "")
      steam_id = entry.get("steamId", "")
      mod = by_gid.get(gid) or by_steam.get(steam_id)
      if mod:
         ordered_ids.append(mod.id)
      else:
         unmatched.append(gid or steam_id or entry.get("name", "(unknown)"))

   return ImportResult(
      collection=ModCollection(name=name, game_id=game_id, mods=ordered_ids),
      matched=len(ordered_ids),
      unmatched=unmatched,
      extracted_mods=[],
   )


# ── ZIP export ────────────────────────────────────────────────────────────────

def export_playset_zip(
      collection: ModCollection,
      all_mods: List[Mod],
      dest_path: Path,
      *,
      progress_callback: Callable[[int, int, str], None] | None = None,
) -> None:
   """
   Write *collection* and every mod folder it references into a ZIP archive.

   *dest_path* should end with '.zip'.

   ZIP structure:
     playset.json                 ← collection metadata
     mods/<mod_folder_name>/…    ← full content of each mod directory

   *progress_callback*(done, total, mod_name) is called for each mod.
   """
   by_id = {m.id: m for m in all_mods}
   ordered: List[Mod] = [by_id[mid] for mid in collection.mods if mid in by_id]

   # Build JSON metadata (write first so the ZIP is valid even if partial)
   entries = [
      {
         "gameRegistryId": _game_registry_id(m),
         "name": m.name,
         "enabled": True,
         "position": pos,
         **({"steamId": m.remote_id} if m.remote_id else {}),
      }
      for pos, m in enumerate(ordered)
   ]
   playset_meta = {
      "name": collection.name,
      "game": collection.game_id,
      "version": "1",
      "mods": entries,
   }

   total = len(ordered)
   with zipfile.ZipFile(dest_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
      zf.writestr(
         "playset.json",
         json.dumps(playset_meta, indent=2, ensure_ascii=False),
      )
      for i, mod in enumerate(ordered):
         if progress_callback:
            progress_callback(i, total, mod.name)
         if not mod.path.is_dir():
            continue
         folder_name = mod.path.name
         for file in mod.path.rglob("*"):
            if file.is_file():
               arc = Path("mods") / folder_name / file.relative_to(mod.path)
               zf.write(file, arcname=str(arc))

   if progress_callback:
      progress_callback(total, total, "")


# ── ZIP import ────────────────────────────────────────────────────────────────

def import_playset_zip(
      path: Path,
      game_id: str,
      mod_install_dir: Path,
      *,
      overwrite: bool = False,
      collection_name: str | None = None,
      progress_callback: Callable[[int, int, str], None] | None = None,
) -> ImportResult:
   """
   Extract a playset ZIP into *mod_install_dir* and create a ModCollection.

   *collection_name* defaults to path.stem
     e.g.  'elephant.zip'  →  collection named 'elephant'

   overwrite=False (default) — skip mod folders that already exist
   overwrite=True            — replace existing mod folders

   The caller should call _refresh_game() after this returns so that
   newly extracted mods appear in all_mods before the collection is used.
   """
   name = collection_name or path.stem
   mod_install_dir.mkdir(parents=True, exist_ok=True)

   with zipfile.ZipFile(path, "r") as zf:
      # ── read metadata ─────────────────────────────────────────────────────
      try:
         meta = json.loads(zf.read("playset.json").decode("utf-8"))
      except (KeyError, json.JSONDecodeError) as exc:
         raise ValueError(
            f"Not a valid playset ZIP — playset.json missing or corrupt: {exc}"
         ) from exc

      raw_entries: list[dict] = sorted(
         meta.get("mods", []), key=lambda e: e.get("position", 0)
      )

      # ── collect mod folders from the archive ──────────────────────────────
      mod_folder_files: dict[str, list[zipfile.ZipInfo]] = {}
      for info in zf.infolist():
         parts = Path(info.filename).parts
         if len(parts) >= 2 and parts[0] == "mods" and not info.is_dir():
            mod_folder_files.setdefault(parts[1], []).append(info)

      # ── extract each mod folder ───────────────────────────────────────────
      extracted: list[str] = []
      skipped: list[str] = []
      total = len(mod_folder_files)

      for i, (folder, members) in enumerate(mod_folder_files.items()):
         if progress_callback:
            progress_callback(i, total, folder)
         dest_folder = mod_install_dir / folder
         if dest_folder.exists() and not overwrite:
            skipped.append(folder)
            continue
         dest_folder.mkdir(parents=True, exist_ok=True)
         for info in members:
            rel = Path(info.filename).relative_to(Path("mods") / folder)
            out = dest_folder / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(zf.read(info))
         extracted.append(folder)

   if progress_callback:
      progress_callback(total, total, "")

   # ── build the collection's ordered mod-ID list ────────────────────────────
   # Mod.id = remote_id (Workshop) or descriptor_path.stem (local).
   # For Workshop mods  steamId  == remote_id == Mod.id  ✓
   # For local mods     Path("mod/foo.mod").stem == "foo" == Mod.id  ✓
   ordered_ids: list[str] = []
   unmatched: list[str] = []

   for entry in raw_entries:
      steam_id = entry.get("steamId", "")
      gid = entry.get("gameRegistryId", "")
      if steam_id:
         ordered_ids.append(steam_id)
      elif gid:
         ordered_ids.append(Path(gid).stem)
      else:
         unmatched.append(entry.get("name", "(unknown)"))

   return ImportResult(
      collection=ModCollection(name=name, game_id=game_id, mods=ordered_ids),
      matched=len(ordered_ids),
      unmatched=unmatched,
      extracted_mods=extracted,
      skipped_mods=skipped,
   )


# ── background workers ────────────────────────────────────────────────────────

class ZipExportWorker(QThread):
   """
   Background thread for export_playset_zip.

   Signals
   -------
   progress(done, total, mod_name)   — emitted for each mod packed
   finished(dest_path)               — emitted on success
   error(message)                    — emitted on exception
   """

   progress = Signal(int, int, str)
   finished = Signal(Path)
   error = Signal(str)

   def __init__(
         self,
         collection: ModCollection,
         all_mods: List[Mod],
         dest_path: Path,
         parent=None,
   ) -> None:
      super().__init__(parent)
      self._collection = collection
      self._all_mods = all_mods
      self._dest_path = dest_path

   def run(self) -> None:
      try:
         export_playset_zip(
            self._collection,
            self._all_mods,
            self._dest_path,
            progress_callback=lambda d, t, n: self.progress.emit(d, t, n),
         )
         self.finished.emit(self._dest_path)
      except Exception as exc:  # noqa: BLE001
         self.error.emit(str(exc))


class ZipImportWorker(QThread):
   """
   Background thread for import_playset_zip.

   Signals
   -------
   progress(done, total, folder_name)  — emitted for each mod folder extracted
   finished(result)                    — emitted on success (ImportResult)
   error(message)                      — emitted on exception
   """

   progress = Signal(int, int, str)
   finished = Signal(object)  # ImportResult
   error = Signal(str)

   def __init__(
         self,
         path: Path,
         game_id: str,
         mod_install_dir: Path,
         overwrite: bool,
         collection_name: str | None,
         parent=None,
   ) -> None:
      super().__init__(parent)
      self._path = path
      self._game_id = game_id
      self._mod_install_dir = mod_install_dir
      self._overwrite = overwrite
      self._collection_name = collection_name

   def run(self) -> None:
      try:
         result = import_playset_zip(
            self._path,
            self._game_id,
            self._mod_install_dir,
            overwrite=self._overwrite,
            collection_name=self._collection_name,
            progress_callback=lambda d, t, n: self.progress.emit(d, t, n),
         )
         self.finished.emit(result)
      except Exception as exc:  # noqa: BLE001
         self.error.emit(str(exc))
