import contextlib
import re
from pathlib import Path
from typing import Any, Dict, List

from pmm.core.models import Mod

# Clausewitz key=value / key="value" / key={ list } pattern
_KV = re.compile(r'^(\w+)\s*=\s*(?:"([^"]*)"|{([^}]*)}|(\S+))', re.MULTILINE)
_EXCLUDED_NAME_PREFIXES = ("ironymodmanager", "pyronymodmanager")


def _parse_block(text: str) -> Dict[str, Any]:
   result: Dict[str, Any] = {}
   for m in _KV.finditer(text):
      key = m.group(1)
      if m.group(2) is not None:  # quoted string
         result[key] = m.group(2)
      elif m.group(3) is not None:  # { list }
         items = re.findall(r'"([^"]+)"|(\S+)', m.group(3))
         result[key] = [a or b for a, b in items]
      else:  # bare word
         result[key] = m.group(4)
   return result


def parse_descriptor(path: Path) -> Mod:
   text = path.read_text(encoding="utf-8", errors="replace")
   d = _parse_block(text)
   # mod root is the .mod file's directory, or the path= key
   mod_root = Path(d.get("path", str(path.parent)))
   if not mod_root.is_absolute():
      mod_root = path.parent / mod_root
   return Mod(
      name=d.get("name", path.stem),
      path=mod_root,
      descriptor_path=path,
      version=d.get("version", ""),
      supported_version=d.get("supported_version", ""),
      tags=d.get("tags", []),
      dependencies=d.get("dependencies", []),
      remote_id=d.get("remote_file_id", ""),
   )


def _is_excluded_mod_name(name: str) -> bool:
   return name.strip().lower().startswith(_EXCLUDED_NAME_PREFIXES)


def _move_to_trash(descriptor: Path, mod_dir: Path) -> None:
   """
   Move a faulty .mod descriptor into a 'trash' folder under mod_dir.

   This is intentionally quiet: failures are ignored so they don't break startup.
   """
   with contextlib.suppress(BaseException):
      trash_dir = mod_dir / "trash"
      trash_dir.mkdir(parents=True, exist_ok=True)
      target = trash_dir / descriptor.name
      # If a file with the same name is already in trash, overwrite it.
      if target.exists():
         target.unlink(missing_ok=True)
      descriptor.replace(target)


def discover_mods(mod_dir: Path) -> List[Mod]:
   """
   Scan a directory for *.mod descriptor files and return parsed Mods.

   Extra safety:
     • If a .mod file's path= folder does not exist, the .mod file is considered
       faulty and is moved to mod_dir/trash/ (and skipped from the result).
   """
   mods: List[Mod] = []
   for desc in sorted(mod_dir.glob("*.mod")):
      try:
         mod = parse_descriptor(desc)
      except BaseException:
         # Malformed descriptor: move it out of the way and skip.
         _move_to_trash(desc, mod_dir)
         continue

      if _is_excluded_mod_name(mod.name):
         # Excluded by name: skip but don't treat as faulty, since the user may have intentionally
         # put an Irony/PMM mod in the mods folder.
         continue

      # Check whether the folder the .mod points to actually exists.
      if not mod.path.exists():
         # Faulty: descriptor pointing to nowhere → move to trash folder.
         _move_to_trash(desc, mod_dir)
         continue

      # Valid mod found: add to the list.
      mods.append(mod)
   return mods
