# Pyrony Mod Manager

> A lightweight, Python-native mod manager for Paradox Interactive games —
> inspired by and based on [IronyModManager](https://github.com/bcssov/IronyModManager),
> rebuilt with a flat structure and minimal dependencies.

![Python](https://img.shields.io/badge/python-%3E%3D3.14-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Version](https://img.shields.io/badge/version-0.2.0-informational)

---

## Features at a glance

- **Collections (playsets)** — create, rename, delete, and switch between named mod lists per game
- **Paradox Launcher integration** — writes directly to `launcher-v2.sqlite`, `mods_registry.json`, and `dlc_load.json`
- **Conflict detection** — file-level and definition-level (Clausewitz AST), with HARD / SOFT severity
- **Import & export** — share playsets as Paradox Launcher-compatible JSON or as self-contained ZIP archives
- **Live mod-directory watching** — detects newly installed or removed mods without restarting
- **Auto-update check** — queries the GitHub Releases API on startup; shows a banner when a newer version exists

---

## Supported games

| ID | Game | Steam App ID |
|---|---|---|
| `stellaris` | Stellaris | 281990 |
| `hoi4` | Hearts of Iron IV | 394360 |
| `eu4` | Europa Universalis IV | 236850 |
| `ck3` | Crusader Kings III | 1158310 |
| `vic3` | Victoria 3 | 529340 |
| `ck2` | Crusader Kings II | 203770 |
| `imperator` | Imperator: Rome | 859580 |

The user-data directory (e.g. `Documents/Paradox Interactive/Stellaris`) is
auto-detected on Windows and Linux. You can override the path for any game in
**Settings → Game user-data paths**.

---

## Requirements

| Dependency | Why |
|---|---|
| Python ≥ 3.14 | Most recent Python version |
| `PySide6 >= 6.10.1` | Qt 6 UI (LGPL) which supports Python ≥ 3.14 |
| `msgspec` | Fast JSON serialisation for preferences |
| `httpx` | HTTP client for the update checker |
| `watchfiles` | Efficient mod-directory hot-reload |

---

## Installation

```bash
git clone https://github.com/Justist/PyronyModManager.git
cd PyronyModManager
pip install -e .
```

Then launch with:

```bash
pyirony
```

Or directly:

```bash
python app.py
```

---

## Project structure

```text
PyronyModManager/
├── app.py                    # Entry point — creates QApplication + MainWindow
├── pyproject.toml
└── pmm/
    ├── core/                 # Pure business logic (no Qt in most modules)
    │   ├── clausewitz.py     # Clausewitz script tokeniser / AST builder
    │   ├── games.py          # Known-game registry + path resolution
    │   ├── launcher.py       # Writes launcher-v2.sqlite, mods_registry.json, dlc_load.json
    │   ├── models.py         # Dataclasses: Mod, ModCollection, Game, Preferences
    │   ├── parser.py         # .mod descriptor file parser
    │   ├── playset_io.py     # Import / export JSON and ZIP playsets
    │   ├── services.py       # Load-order resolution + conflict detection
    │   ├── storage.py        # JSON persistence via msgspec (~/.config/pyirony/)
    │   ├── updater.py        # GitHub Releases update checker (QThread)
    │   └── watcher.py        # Mod-directory file watcher (QThread, watchfiles)
    └── ui/
        ├── collection_dialogs.py  # New / Rename collection dialog
        ├── conflict_view.py       # Conflicts tab with diff viewer
        ├── main_window.py         # Main window — toolbar, tabs, wiring
        ├── mod_list.py            # Dual-list load-order widget
        ├── settings_dialog.py     # Settings dialog
        └── update_banner.py       # In-app update notification banner
```

---

## Paradox Launcher integration

When you click **✔ Apply**, Pyrony writes to three locations in the game's
user-data directory, in order of priority:

1. **`launcher-v2.sqlite`** — the database read by the modern Paradox Launcher
   (≥ 2020.10). Pyrony creates or updates a named playset and sets it active.
   All other playsets are deactivated so the launcher's own UI stays consistent.

2. **`game_data/mods_registry.json`** — the legacy mod catalogue read by older
   launcher versions and by some game engines directly.

3. **`dlc_load.json`** — the oldest fallback, used when the game is launched
   with `-skipLauncher` or directly via the executable.

Writing to all three means Pyrony works regardless of whether you start through
Steam, the Paradox Launcher, or directly.

> **Note:** The Paradox Launcher must be fully closed before clicking Apply.
> The SQLite database is locked while the launcher is running.

---

## Conflict detection

The **Conflicts** tab scans the active collection for file overlaps and
classifies each one:

| Severity | Meaning |
|---|---|
| 🔴 **HARD** | Two or more mods define the same top-level Clausewitz key in the same file |
| 🟡 **SOFT** | Files overlap but no definition-level collision is found, or the file is a non-Clausewitz text type |

Binary files (`.png`, `.dds`, `.ogg`, `.mesh`, …) are skipped entirely.

The view also shows:

- Which mods own the conflicting file, in load-order
- A **full unified diff** of the file between the two top mods
- A **definition-level diff** listing keys that are added, removed, or changed

The underlying parser (`pmm.core.clausewitz`) builds a lightweight AST from
Clausewitz script, extracting top-level definition keys such as
`country_event.100`, `province@3`, or `technology = { … }`.

---

## Import / Export playsets

The **⇅ I/O** button in the toolbar opens a menu with four options.

### Import / Export — JSON

The JSON format is compatible with the Paradox Launcher's own playset export:

```json
{
  "name": "Iron Man Run",
  "game": "hoi4",
  "version": "1",
  "mods": [
    {
      "gameRegistryId": "mod/ugc_2007364284.mod",
      "steamId": "2007364284",
      "name": "Expert AI",
      "enabled": true,
      "position": 0
    },
    {
      "gameRegistryId": "mod/my_focus.mod",
      "name": "Custom Focuses",
      "enabled": true,
      "position": 1
    }
  ]
}
```

- **Export** — saves `<collection name>.json` to a location you choose.
- **Import** — reads any compatible JSON file; names the new collection after
  the filename stem, for example `elephant.json` becomes **elephant**.
  Mods not currently installed are listed in a warning dialog.

### Import / Export — ZIP

A ZIP export bundles the entire playset for sharing or backup:

```text
Iron Man Run.zip
├── playset.json
└── mods/
    ├── ugc_2007364284/
    └── my_focus/
```

- **Export** — packs every mod folder into a ZIP named after the collection.
- **Import** — extracts each mod folder to the game's `mod/` directory and
  creates a new collection named after the ZIP filename stem.
- If a mod folder already exists, Pyrony asks whether to overwrite it or skip it.

---

## Load-order UI

The main tab is a side-by-side dual list.

**Left — Available mods:** all `.mod` descriptors found in the game's `mod/`
directory. A filter bar supports a small query language:

```text
ui dyn
version=4.3
version=4*
ai && version=4.3
ai || graphics
name~/ai.*fix/
version~/^4\.1/
```

**Right — Active playset:** mods in explicit load order. It supports drag-and-drop
reordering with multi-selection, plus ▲ / ▼ buttons for keyboard-friendly changes.

Right-clicking any mod offers:

- **Open in File Explorer**
- **Open in Steam Workshop** for mods with a `remote_file_id`

---

## Preferences & storage

All preferences are stored as plain JSON in:

| Platform | Path |
|---|---|
| Windows | `%USERPROFILE%\\.config\\pyirony\\prefs.json` |
| Linux / macOS | `~/.config/pyirony/prefs.json` |

Stored preferences include:

- Last active game and collection
- Per-game user-data path overrides
- UI font size for mod-list entries
- Whether to check for updates on startup

---

## Update checker

On startup, Pyrony queries:

```text
https://api.github.com/repos/Justist/PyronyModManager/releases/latest
```

If a newer version is available, a banner appears at the top of the window with
a link to the release page. The check can be disabled in Settings.

---

## Contributing

The codebase is intentionally flat. Each module has one clear responsibility.
Before adding a new module, check whether the logic fits in an existing one.

```bash
pip install -e .
pyirony
```

There are currently no automated tests. Contributions that add tests for
`pmm.core.*` are especially useful because most of that code is UI-independent.

---

## Credits

Pyrony is a Python reimplementation of
[IronyModManager](https://github.com/bcssov/IronyModManager) by
[@bcssov](https://github.com/bcssov). It borrows the overall launcher-integration
approach and conflict-analysis direction while aiming for a simpler, flatter
Python codebase.

---

## Disclaimer

Pyrony Mod Manager is an independent project and is not affiliated with Paradox Interactive
in any way. Use at your own risk. Always back up your save files and mods before using any mod manager.

Pyrony Mod Manager has largely been made using genAI, including Claude Sonnet 4.6, Sourcery, and GitHub Copilot. 
AI usage includes code generation, refactoring, documentation, and understanding the code of Irony Mod Manager.
If you have any moral or otherwise objections to AI-generated code, please consider using Irony Mod Manager instead.

---

## License

MIT — see [LICENSE](LICENSE).
