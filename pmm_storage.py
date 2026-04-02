from pathlib import Path
from typing import Type, TypeVar

import msgspec

T = TypeVar("T")
_CONFIG_DIR = Path.home() / ".config" / "pyirony"


def _path(filename: str) -> Path:
   _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
   return _CONFIG_DIR / filename


def save(obj: msgspec.Struct | object, filename: str) -> None:
   _path(filename).write_bytes(msgspec.json.encode(obj))


def load(cls: Type[T], filename: str) -> T | None:
   p = _path(filename)
   return msgspec.json.decode(p.read_bytes(), type=cls) if p.exists() else None


def load_or_default(cls: Type[T], filename: str) -> T:
   result = load(cls, filename)
   return result if result is not None else cls()  # type: ignore
