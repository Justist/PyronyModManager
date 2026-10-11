"""Immutable semantic profile reader and context resolver."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .model import KeyKind, SemanticContext, SemanticKey, SemanticRole


@dataclass(frozen=True, slots=True)
class BooleanOperator:
    key: str
    kind: str
    minimum: int = 0
    maximum: int | None = None


@dataclass(frozen=True, slots=True)
class DefinitionRule:
    name: str
    paths: tuple[str, ...]
    name_field: str | None = None
    root_keys: tuple[str, ...] = ()
    type_per_file: bool = False
    skip_root_key: bool = False


@dataclass(frozen=True, slots=True)
class ContextRule:
    path_prefix: str
    role: SemanticRole
    ancestors: tuple[str, ...] = ()


class SemanticRegistry:
    def __init__(self, profile: dict[str, Any]) -> None:
        self.profile = profile
        self.game = str(profile["game"])
        self.boolean_operators = {
            key.casefold(): BooleanOperator(
                key.casefold(), value["kind"], int(value.get("min", 0)),
                value.get("max"))
            for key, value in profile.get("booleanOperators", {}).items()
        }
        self.definition_rules = tuple(
            DefinitionRule(
                item["name"],
                tuple(self._normal_path(path) for path in item.get("paths", ())),
                (item.get("nameField") or None),
                tuple(key.casefold() for key in item.get("rootKeys", ())),
                bool(item.get("typePerFile", False)),
                bool(item.get("skipRootKey", False)),
            ) for item in profile.get("definitionTypes", ())
        )
        self.context_rules = tuple(
            ContextRule(self._normal_path(item["path"]),
                        SemanticRole[item["role"]],
                        tuple(part.casefold() for part in item.get("ancestors", ())))
            for item in profile.get("contexts", ())
        )
        explicit = {str(key).casefold() for key in profile.get("keywords", ())}
        self.keywords = frozenset(explicit | set(self.boolean_operators))
    
    @staticmethod
    def _normal_path(path: str) -> str:
        return path.replace("\\", "/").strip("/").casefold()
    
    @classmethod
    def load(cls, path: Path) -> "SemanticRegistry":
        return cls(json.loads(path.read_text(encoding="utf-8")))
    
    def semantic_key(self, raw: str, context: SemanticContext) -> SemanticKey:
        folded = raw.casefold()
        if folded in self.keywords or folded in self.boolean_operators:
            return SemanticKey(KeyKind.KEYWORD, folded)
        return SemanticKey(KeyKind.IDENTIFIER, raw)
    
    def role_for(self, path: str | PurePosixPath,
                 ancestors: tuple[str, ...] = ()) -> SemanticRole:
        normalized = self._normal_path(str(path))
        folded_ancestors = tuple(part.casefold() for part in ancestors)
        matches = [rule for rule in self.context_rules
                   if normalized.startswith(rule.path_prefix)
                   and (not rule.ancestors
                        or folded_ancestors[-len(rule.ancestors):] == rule.ancestors)]
        return max(matches, key=lambda rule: (len(rule.path_prefix),
                                              len(rule.ancestors))).role \
            if matches else SemanticRole.UNKNOWN
    
    def definitions_for(self, path: str | PurePosixPath) -> tuple[DefinitionRule, ...]:
        normalized = self._normal_path(str(path))
        return tuple(rule for rule in self.definition_rules
                     if any(normalized.startswith(prefix) for prefix in rule.paths))
