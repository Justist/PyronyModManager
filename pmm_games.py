import platform
from pathlib import Path
from typing import Dict, List

from pmm_models import Game


def _docs() -> Path:
   system = platform.system()
   if system == "Windows":
      import os
      return Path(os.environ.get("USERPROFILE", "~")).expanduser() / "Documents"
   return Path.home() / "Documents"


_PARADOX = _docs() / "Paradox Interactive"

KNOWN_GAMES: List[Game] = [
   Game("stellaris", "Stellaris", 281990, user_data_path=_PARADOX / "Stellaris"),
   Game("hoi4", "Hearts of Iron IV", 394360, user_data_path=_PARADOX / "Hearts of Iron IV"),
   Game("eu4", "Europa Universalis IV", 236850, user_data_path=_PARADOX / "Europa Universalis IV"),
   Game("ck3", "Crusader Kings III", 1158310, user_data_path=_PARADOX / "Crusader Kings III"),
   Game("vic3", "Victoria 3", 529340, user_data_path=_PARADOX / "Victoria 3"),
   Game("ck2", "Crusader Kings II", 203770, user_data_path=_PARADOX / "Crusader Kings II"),
   Game("imperator", "Imperator: Rome", 859580, user_data_path=_PARADOX / "Imperator"),
]


def get_game(game_id: str) -> Game | None:
   return next((g for g in KNOWN_GAMES if g.id == game_id), None)


def get_effective_user_data(game: Game, game_paths: Dict[str, str]) -> Path | None:
   """
   Return the user-data path for a game, preferring any manual override
   stored in prefs.game_paths over the auto-detected default.
   """
   override = game_paths.get(game.id, "").strip()
   return Path(override) if override else game.user_data_path


def get_mod_dir(game: Game, game_paths: Dict[str, str] | None = None) -> Path | None:
   """Return <user_data>/mod, honouring any path override from settings."""
   base = get_effective_user_data(game, game_paths or {})
   return None if base is None else base / "mod"
