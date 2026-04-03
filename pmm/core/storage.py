from pathlib import Path
from typing import Any, TypeVar

import msgspec

T = TypeVar("T")
_CONFIG_DIR = Path.home() / ".config" / "pyirony"


def _path(filename: str) -> Path:
   _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
   return _CONFIG_DIR / filename


def save(obj: Any, filename: str) -> None:
   _path(filename).write_bytes(msgspec.json.encode(obj))


def load(cls: type[T], filename: str) -> T | None:
   p = _path(filename)
   return msgspec.json.decode(p.read_bytes(), type=cls) if p.exists() else None


def load_or_default(cls: type[T], filename: str) -> T:
   result = load(cls, filename)
   return result if result is not None else cls()
