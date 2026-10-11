"""Shared semantic types; no game- or UI-specific code belongs here."""
from __future__ import annotations

from dataclasses import dataclass
from enum import auto, Enum
from pathlib import PurePosixPath


class SemanticRole(Enum):
    UNKNOWN = auto()
    DEFINITION = auto()
    TRIGGER = auto()
    EFFECT = auto()
    MODIFIER_MAP = auto()
    ORDERED_SEQUENCE = auto()
    UNORDERED_SET = auto()


class KeyKind(Enum):
    KEYWORD = auto()
    IDENTIFIER = auto()


@dataclass(frozen=True, slots=True)
class SemanticKey:
    kind: KeyKind
    value: str


@dataclass(frozen=True, slots=True)
class SemanticContext:
    game: str
    path: PurePosixPath
    role: SemanticRole = SemanticRole.UNKNOWN
    ancestors: tuple[str, ...] = ()
    definition_type: str | None = None
    
    def child(self, key: str, role: SemanticRole | None = None) -> "SemanticContext":
        return SemanticContext(self.game, self.path, role or self.role,
                               self.ancestors + (key,), self.definition_type)
