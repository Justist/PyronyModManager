# Pyrony Mod Manager

Pyrony Mod Manager is a desktop mod manager for Paradox games, heavily inspired by and based on [IronyModManager](https://github.com/bcssov/IronyModManager).

It focuses on:

- Managing per‑game mod *collections* (playsets)
- Applying load order into the Paradox Launcher’s database
- Detecting and inspecting file‑level and definition‑level mod conflicts
- Staying fast and lightweight, with a native Qt UI (PySide6)

Pyrony is written in Python and targets modern Paradox titles using the `launcher-v2.sqlite` format.

---

## Supported games

Out of the box, Pyrony knows about these games:

- Stellaris
- Hearts of Iron IV
- Europa Universalis IV
- Crusader Kings III
- Victoria 3
- Crusader Kings II
- Imperator: Rome

For each game, Pyrony will try to auto‑detect the user‑data directory (e.g. `Documents/Paradox Interactive/<Game>` on Windows). You can override this per‑game in the Settings dialog.

---

## Features

### Collections (playsets)

- Create, rename, and delete collections per game.
- Each collection is an ordered list of mods.
- The active collection’s order is what gets applied to the game.

### Paradox Launcher integration

Pyrony writes to the same places as the official launcher:

1. `launcher-v2.sqlite`  
   - Creates/updates a named playset.
   - Marks it as active.
   - Keeps mod order in sync with your collection.

2. `game_data/mods_registry.json`  
   - Legacy/fallback mod registry.

3. `dlc_load.json`  
   - Oldest fallback.
   - Still used when launching the game directly (e.g. with `-skipLauncher`).

This means you can use Pyrony to manage load order and still start the game from the official launcher or Steam.

### Conflict detection & diffing

Pyrony provides a dedicated **Conflicts** tab:

- Scans all active mods for overlapping files.
- Classifies conflicts as:
  - **HARD** – two or more mods override the same Clausewitz *definition* (e.g. the same event or country).
  - **SOFT** – files overlap but definitions do not clearly collide.
- Shows:
  - Conflict list with severity icons and summary.
  - Per‑file owners (which mods touch this file).
  - Unified diff (full file) view.
  - Definition‑level diff:
    - Extracts and compares top‑level Clausewitz definitions.
    - Highlights definitions that exist only in one mod or whose contents differ.

Under the hood this uses a custom Clausewitz parser (`pmm.core.clausewitz`) to build a small AST and compare definition keys such as `country_event.100`, `province@3`, etc.

### Load order UI

The main tab is a dual‑list view:

- **Available mods** (left):
  - All `.mod` descriptors discovered in the game’s `mod` directory.
  - Filter bar with a small query language:
    - Free text filters by name (case‑insensitive).
    - `version=...` filters by supported version, with `*` wildcard:
      - `version=4.3` → contains `4.3`.
      - `version=4*` → versions starting with `4` (4.0, 4.1, …).
      - `version=*` → any non‑empty supported version.
    - Combine with `and` / `or`:
      - `ai and version=4.3`
      - `ai or graphics`
    - Optional regex:
      - `name~/ai.*fix/`
      - `version~/^4\.1/`
  - Right‑click → “Filter help…” with examples.

- **Active playset** (right):
  - Checked mods in explicit load order.
  - Drag‑and‑drop reordering with multi‑selection.
  - ▲ / ▼ buttons for keyboard‑friendly order tweaks.

Right‑clicking a mod (in either list) offers:

- **Open in File Explorer** – opens the mod’s folder.
- **Open in Steam Workshop** – opens the mod’s workshop page in the Steam client (if it has a `remote_file_id`).

### Settings & preferences

Persisted user preferences include:

- Last active game and collection.
- Per‑game user‑data path overrides.
- Whether to check for updates on startup.
- UI font size (used for the mod lists).

Settings are stored as compact JSON under:

```text
~/.config/pyirony/prefs.json
