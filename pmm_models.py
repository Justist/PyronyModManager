from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List


@dataclass
class Mod:
   name: str
   path: Path
   descriptor_path: Path
   enabled: bool = True
   order: int = 0
   version: str = ""
   supported_version: str = ""
   tags: List[str] = field(default_factory=list)
   dependencies: List[str] = field(default_factory=list)
   remote_id: str = ""

   @property
   def id(self) -> str:
      return self.remote_id or self.descriptor_path.stem


@dataclass
class ModCollection:
   name: str
   game_id: str
   mods: List[str] = field(default_factory=list)
   launcher_playset_id: str = ""


@dataclass
class Game:
   id: str
   display_name: str
   steam_id: int
   install_path: Path | None = None
   user_data_path: Path | None = None
   launcher_settings_path: Path | None = None


@dataclass
class Preferences:
   active_game_id: str = ""
   active_collection: str = ""
   language: str = "en"
   check_for_updates: bool = True
   theme: str = "dark"
   collections: List[ModCollection] = field(default_factory=list)
   # Per-game user-data path overrides set by the Settings dialog.
   # Keys are Game.id; values are absolute path strings.
   # Missing keys fall back to the auto-detected path in pmm_games.
   game_paths: Dict[str, str] = field(default_factory=dict)
