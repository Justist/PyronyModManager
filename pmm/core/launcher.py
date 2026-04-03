"""
pmm_launcher
============
Writes the Paradox Launcher load-order files and launches the game via Steam.

Apply flow for modern games (PDX Launcher >= 2020.10)
-----------------------------------------------------
The Paradox Launcher stores its state in a SQLite database
(launcher-v2.sqlite) inside the game's user-data directory.  When the
launcher starts it reads FROM that database, then WRITES dlc_load.json
before handing off to the game engine.  Writing dlc_load.json directly
is therefore insufficient — the launcher immediately overwrites it with
whatever its own database says.

This module writes to all three sinks in order:

  1. launcher-v2.sqlite   – creates/updates a named playset and sets it
                            active; this is what the Paradox Launcher UI
                            shows and what governs the final load order.
  2. mods_registry.json   – legacy fallback read by older launcher versions
                            and the game engine directly on some titles.
  3. dlc_load.json        – oldest fallback; also used by the engine when
                            the launcher is bypassed entirely.

If launcher-v2.sqlite does not exist yet the module creates it with the
minimal schema (matching the actual Paradox Launcher schema).

Public API
----------
  write_launcher_files(game, collection, all_mods, game_paths=None) -> Path
  launch_game(game) -> None
  launch_direct(exe_path) -> None
  preview_dlc_load(collection, all_mods) -> dict

SQLite schema notes
-------------------
Real table names in the launcher DB:  playsets, playsets_mods, mods
  playsets.id           char(36)  — UUID primary key for the playset row
  playsets.loadOrder    varchar   — enum flag; "custom" for manually ordered
  playsets.createdOn    datetime  — NOT NULL, must be set on INSERT

  mods.id               char(36)  — UUID primary key (NOT the gameRegistryId)
  mods.gameRegistryId   TEXT      — "mod/ugc_XXXX.mod" or "mod/name.mod"
                                    same format as dlc_load.json entries
                                    used as the lookup key when matching mods

  playsets_mods.modId   char(36)  — references mods.id (UUID), NOT gameRegistryId
  playsets_mods.position INTEGER  — 0-based load order index
"""

import json
import platform
import shutil
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import pmm.core.games as games
from pmm.core.models import Game, Mod, ModCollection


# ── public API ────────────────────────────────────────────────────────────────

def write_launcher_files(
      game: Game,
      collection: ModCollection,
      all_mods: List[Mod],
      game_paths: Dict[str, str] | None = None,
) -> Path:
   """
   Write all load-order files for *collection* and update the launcher DB.

   Mutates collection.launcher_playset_id if a new playset UUID is assigned;
   the caller should persist prefs afterwards.

   Returns the user-data directory that was written to.
   Raises RuntimeError when no user-data path is resolvable.
   """
   user_data = games.get_effective_user_data(game, game_paths or {})
   if user_data is None:
      raise RuntimeError(
         f"No user-data path configured for {game.display_name}.\n"
         "Set it in Settings → Game user-data paths."
      )

   user_data.mkdir(parents=True, exist_ok=True)

   by_id = {m.id: m for m in all_mods}
   ordered = [by_id[mid] for mid in collection.mods if mid in by_id]

   # 1. SQLite — this is what the launcher UI reads
   playset_id = _write_launcher_db(user_data, collection, ordered)
   if playset_id and not collection.launcher_playset_id:
      collection.launcher_playset_id = playset_id

   # 2. mods_registry.json — legacy/fallback
   _write_mods_registry(user_data, ordered)

   # 3. dlc_load.json — oldest fallback and direct-launch support
   _write_dlc_load(user_data, ordered)

   return user_data


def launch_game(game: Game) -> None:
   """Launch via Steam URI protocol (steam://rungameid/<id>)."""
   _open_url(f"steam://rungameid/{game.steam_id}")


def launch_direct(exe_path: Path) -> None:
   """Launch game executable directly, bypassing Steam and the PDX Launcher."""
   if not exe_path.is_file():
      raise FileNotFoundError(f"Executable not found: {exe_path}")
   subprocess.Popen([str(exe_path)], cwd=str(exe_path.parent))


def preview_dlc_load(collection: ModCollection, all_mods: list[Mod]) -> dict:
   """Return the dlc_load.json payload as a plain dict (no file written)."""
   by_id = {m.id: m for m in all_mods}
   ordered = [by_id[mid] for mid in collection.mods if mid in by_id]
   return {"enabled_mods": [_game_registry_id(m) for m in ordered], "disabled_dlcs": []}


# ── mod identity helpers ──────────────────────────────────────────────────────

def _game_registry_id(mod: Mod) -> str:
   """
   The gameRegistryId as stored in the mods table and in dlc_load.json.
   This is how the game engine identifies each mod on disk.

   Steam Workshop mods → "mod/ugc_{remote_id}.mod"   e.g. "mod/ugc_2007364284.mod"
   Local mods          → "mod/{descriptor_filename}"  e.g. "mod/my_patch.mod"
   """
   if mod.remote_id:
      return f"mod/ugc_{mod.remote_id}.mod"
   return f"mod/{mod.descriptor_path.name}"


# ── launcher-v2.sqlite ────────────────────────────────────────────────────────

# Minimal schema that matches the real Paradox Launcher database structure.
# Used only when creating a new database from scratch.  When the database
# already exists, CREATE TABLE IF NOT EXISTS is a safe no-op for each table.
_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS "playsets" (
    "id"                    char(36)     NOT NULL,
    "name"                  varchar(255) NOT NULL,
    "isActive"              boolean,
    "loadOrder"             varchar(255),
    "createdOn"             datetime     NOT NULL,
    "updatedOn"             datetime,
    "isRemoved"             boolean      NOT NULL DEFAULT false,
    "hasNotApprovedChanges" boolean      NOT NULL DEFAULT '0',
    "state"                 TEXT         NOT NULL DEFAULT 'private',
    "owned"                 boolean      NOT NULL DEFAULT '1',
    "author"                varchar(255) NOT NULL DEFAULT '',
    "subscribersCount"      INTEGER      NOT NULL DEFAULT '0',
    "ratingsCount"          INTEGER      NOT NULL DEFAULT '0',
    "offDisk"               boolean      NOT NULL DEFAULT '0',
    PRIMARY KEY("id")
);

CREATE TABLE IF NOT EXISTS "mods" (
    "id"              char(36)     NOT NULL,
    "gameRegistryId"  TEXT,
    "steamId"         varchar(255),
    "name"            varchar(255),
    "displayName"     varchar(255),
    "version"         varchar(255),
    "requiredVersion" varchar(255),
    "dirPath"         TEXT,
    "thumbnailPath"   TEXT,
    "tags"            json         DEFAULT '[]',
    "status"          TEXT         NOT NULL,
    "source"          TEXT         NOT NULL,
    PRIMARY KEY("id")
);

CREATE TABLE IF NOT EXISTS "playsets_mods" (
    "playsetId" char(36) NOT NULL,
    "modId"     char(36) NOT NULL,
    "enabled"   boolean  DEFAULT '1',
    "position"  INTEGER,
    PRIMARY KEY("playsetId", "modId"),
    FOREIGN KEY("playsetId") REFERENCES "playsets"("id") ON DELETE CASCADE,
    FOREIGN KEY("modId")     REFERENCES "mods"("id")     ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS "settings" (
    "id"    TEXT NOT NULL,
    "value" TEXT,
    PRIMARY KEY("id")
);
"""


def _find_launcher_db(user_data: Path) -> Path | None:
   """Return the path to the launcher SQLite database, or None if absent."""
   for name in ("launcher-v2.sqlite", "launcher_v2.sqlite", "launcher-v2_dev.sqlite"):
      p = user_data / name
      if p.exists():
         return p
   return None


def _ensure_launcher_db(db_path: Path) -> None:
   """
   Create the DB file and apply the minimal schema if it does not exist.
   On an existing DB this is a safe no-op for every table.
   """
   conn = sqlite3.connect(str(db_path))
   try:
      conn.executescript(_SCHEMA)
      conn.commit()
   finally:
      conn.close()


def _write_launcher_db(
      user_data: Path,
      collection: ModCollection,
      ordered: list[Mod],
) -> str:
   """
   Upsert the collection as a named, active playset in launcher-v2.sqlite.

   Returns the playset UUID that was written (new or existing).
   Raises RuntimeError if the DB is locked (launcher is still open).

   Resolution order for the playset row:
     1. collection.launcher_playset_id  – preferred; survives launcher renames
     2. row with matching name          – fallback on first Apply
     3. fresh UUID                      – brand-new playset
   """
   db_path = _find_launcher_db(user_data)
   if db_path is None:
      db_path = user_data / "launcher-v2.sqlite"

   # Always ensure schema exists / any missing tables are created
   _ensure_launcher_db(db_path)

   try:
      conn = sqlite3.connect(str(db_path), timeout=5)
      conn.execute("PRAGMA foreign_keys = ON")
      try:
         with conn:
            return _upsert_playset(conn, collection, ordered)
      finally:
         conn.close()
   except sqlite3.OperationalError as exc:
      raise RuntimeError(
         f"Could not update launcher-v2.sqlite: {exc}\n\n"
         "Make sure the Paradox Launcher is fully closed before applying."
      ) from exc


def _upsert_playset(
      conn: sqlite3.Connection,
      collection: ModCollection,
      ordered: list[Mod],
) -> str:
   """
   Perform the full playset upsert inside an open transaction.
   Returns the playset UUID used.
   """
   now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

   # ── 1. Resolve or create the playset UUID ─────────────────────────────────
   playset_id: str = ""

   # Prefer the stored UUID — survives the user renaming the playset inside
   # the Paradox Launcher after the first Apply.
   if collection.launcher_playset_id:
      if row := conn.execute(
          "SELECT id FROM playsets WHERE id = ?",
          (collection.launcher_playset_id, ),
      ).fetchone():
         playset_id = row[0]

   # Fall back to matching by name (first Apply, or UUID was lost).
   if not playset_id:
      if row := conn.execute(
          "SELECT id FROM playsets WHERE name = ?",
          (collection.name, ),
      ).fetchone():
         playset_id = row[0]

   # Generate a fresh UUID for a brand-new playset.
   if not playset_id:
      playset_id = str(uuid.uuid4())

   # ── 2. Deactivate every other playset ─────────────────────────────────────
   conn.execute("UPDATE playsets SET isActive = 0 WHERE id != ?", (playset_id,))

   # ── 3. Upsert the playset row ─────────────────────────────────────────────
   # loadOrder = 'custom' means the user manually controls load order via
   # the position column in playsets_mods (as opposed to 'automatic').
   # createdOn must be supplied on INSERT (NOT NULL, no default).
   conn.execute(
      """
      INSERT INTO playsets
      (id, name, isActive, loadOrder, createdOn, updatedOn,
       isRemoved, hasNotApprovedChanges, state, owned,
       author, subscribersCount, ratingsCount, offDisk)
      VALUES (?, ?, 1, 'custom', ?, ?, 0, 0, 'private', 1, '', 0, 0, 0)
      ON CONFLICT(id) DO UPDATE SET name      = excluded.name,
                                    isActive  = 1,
                                    updatedOn = excluded.updatedOn
      """,
      (playset_id, collection.name, now, now),
   )

   # ── 4. Upsert each mod into the mods catalogue; collect row UUIDs ─────────
   # playsets_mods.modId references mods.id (a UUID), NOT gameRegistryId.
   # We look up the existing UUID by gameRegistryId so we never create
   # duplicate rows for a mod the launcher already knows about.
   mod_uuids: list[str] = []
   mod_uuids.extend(_upsert_mod_row(conn, mod) for mod in ordered)
   # ── 5. Replace playsets_mods rows for this playset ────────────────────────
   # DELETE + re-INSERT is safer than UPSERT because every position value
   # may have changed whenever the user reorders the collection.
   conn.execute("DELETE FROM playsets_mods WHERE playsetId = ?", (playset_id,))
   conn.executemany(
      "INSERT INTO playsets_mods (playsetId, modId, enabled, position) VALUES (?,?,1,?)",
      [(playset_id, mod_uuid, pos) for pos, mod_uuid in enumerate(mod_uuids)],
   )

   return playset_id


def _upsert_mod_row(conn: sqlite3.Connection, mod: Mod) -> str:
   """
   Ensure *mod* has a row in the mods table and return its UUID (mods.id).

   Lookup order:
     1. gameRegistryId  — primary; e.g. "mod/ugc_12345678.mod"
     2. steamId         — fallback for Workshop mods whose gameRegistryId
                          may differ between launcher versions
     3. INSERT new row  — mod not yet known to this launcher installation
   """
   gid = _game_registry_id(mod)

   # 1. Lookup by gameRegistryId
   row = conn.execute(
      "SELECT id FROM mods WHERE gameRegistryId = ? LIMIT 1", (gid,)
   ).fetchone()

   # 2. Fallback: lookup by steamId for Workshop mods
   if row is None and mod.remote_id:
      row = conn.execute(
         "SELECT id FROM mods WHERE steamId = ? LIMIT 1", (mod.remote_id,)
      ).fetchone()

   if row is not None:
      mod_uuid = row[0]
      # Update mutable metadata; never overwrite id or gameRegistryId
      conn.execute(
         """
         UPDATE mods
         SET gameRegistryId  = ?,
             displayName     = ?,
             version         = ?,
             requiredVersion = ?,
             dirPath         = ?,
             thumbnailPath   = ?,
             tags            = ?
         WHERE id = ?
         """,
         (
            gid,
            mod.name,
            mod.version,
            mod.supported_version or None,
            str(mod.path),
            _find_thumbnail(mod),
            json.dumps(mod.tags),
            mod_uuid,
         ),
      )
      return mod_uuid

   # 3. Insert a new row with a fresh UUID
   mod_uuid = str(uuid.uuid4())
   conn.execute(
      """
      INSERT INTO mods
      (id, gameRegistryId, steamId, name, displayName,
       version, requiredVersion, dirPath, thumbnailPath,
       tags, status, source)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready_to_play', ?)
      """,
      (
         mod_uuid,
         gid,
         mod.remote_id or None,
         mod.name,
         mod.name,
         mod.version,
         mod.supported_version or None,
         str(mod.path),
         _find_thumbnail(mod),
         json.dumps(mod.tags),
         "steam" if mod.remote_id else "local",
      ),
   )
   return mod_uuid


# ── mods_registry.json (legacy fallback) ─────────────────────────────────────

def _write_mods_registry(user_data: Path, ordered: list[Mod]) -> None:
   """
   Write <user_data>/game_data/mods_registry.json.

   Keys use gameRegistryId ("mod/ugc_XXXX.mod") to match the SQLite DB.
   Read by older PDX Launcher versions and some game engines directly.
   """
   dest = user_data / "game_data"
   dest.mkdir(exist_ok=True)
   path = dest / "mods_registry.json"
   _backup(path)

   registry: dict[str, dict] = {}
   for mod in ordered:
      gid = _game_registry_id(mod)
      entry: dict = {
         "id": gid,
         "displayName": mod.name,
         "status": "ready_to_play",
         "dirPath": str(mod.path),
         "thumbnailPath": _find_thumbnail(mod),
         "version": mod.version,
         "tags": mod.tags,
      }
      if mod.remote_id:
         entry["source"] = "steam"
         entry["steamId"] = mod.remote_id
      else:
         entry["source"] = "local"
      registry[gid] = entry

   _write_json(path, registry)


# ── dlc_load.json (oldest fallback) ──────────────────────────────────────────

def _write_dlc_load(user_data: Path, ordered: list[Mod]) -> None:
   """
   Write <user_data>/dlc_load.json.

   Uses the same gameRegistryId format as the SQLite mods table.
   Overwritten by the Paradox Launcher on startup, but remains the
   authoritative source when the game is launched directly without the
   launcher (e.g. from the executable or via -skipLauncher).
   """
   path = user_data / "dlc_load.json"
   _backup(path)
   _write_json(path, {
      "enabled_mods": [_game_registry_id(m) for m in ordered],
      "disabled_dlcs": [],
   })


# ── backup / IO helpers ───────────────────────────────────────────────────────

def _find_thumbnail(mod: Mod) -> str:
   """Return the absolute path to the mod's thumbnail, or '' if none exists."""
   for name in ("thumbnail.png", "thumbnail.jpg", "thumbnail.webp"):
      candidate = mod.path / name
      if candidate.is_file():
         return str(candidate)
   return ""


def _backup(path: Path) -> None:
   """Copy path to a UTC-timestamped .bak; keep the 5 most recent only."""
   if not path.exists():
      return
   ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
   bak = path.with_name(f"{path.stem}.{ts}.bak")
   shutil.copy2(path, bak)
   for old in sorted(path.parent.glob(f"{path.stem}.*.bak"))[:-5]:
      old.unlink(missing_ok=True)


def _write_json(path: Path, data: object) -> None:
   path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── URL / process launch ──────────────────────────────────────────────────────

def _open_url(url: str) -> None:
   system = platform.system()
   try:
      if system == "Windows":
         # shell=False avoids a visible console window on Windows
         subprocess.Popen(["cmd", "/c", "start", "", url], shell=False)
      elif system == "Darwin":
         subprocess.Popen(["open", url])
      elif system == "Linux":
         subprocess.Popen(["xdg-open", url])
      else:
         raise RuntimeError(f"Unsupported platform: {system!r}")
   except OSError as exc:
      raise RuntimeError(f"Failed to open Steam URL '{url}': {exc}") from exc
