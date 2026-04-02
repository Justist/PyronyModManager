import re
from pathlib import Path

from pmm_models import Mod

# Clausewitz key=value / key="value" / key={ list } pattern
_KV = re.compile(r'^(\w+)\s*=\s*(?:"([^"]*)"|{([^}]*)}|(\S+))', re.MULTILINE)


def _parse_block(text: str) -> dict:
   result: dict = {}
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


def discover_mods(mod_dir: Path) -> list[Mod]:
   """Scan a directory for *.mod descriptor files and return parsed Mods."""
   return [parse_descriptor(p) for p in sorted(mod_dir.glob("*.mod"))]
