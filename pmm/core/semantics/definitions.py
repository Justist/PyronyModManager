"""Playset-wide semantic definition index."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from pmm.core.clausewitz import CWBlock, CWPair, parse_file, scalar_text
from pmm.core.models import Mod
from .registry import DefinitionRule, SemanticRegistry


@dataclass(frozen=True, slots=True)
class DefinitionKey:
    game: str
    definition_type: str
    identifier: str


@dataclass(frozen=True, slots=True)
class DefinitionSource:
    key: DefinitionKey
    mod_index: int
    mod: Mod
    path: PurePosixPath
    pair: CWPair


@dataclass(slots=True)
class DefinitionIndex:
    by_key: dict[DefinitionKey, list[DefinitionSource]]
    
    @property
    def collisions(self) -> dict[DefinitionKey, list[DefinitionSource]]:
        return {key: sources for key, sources in self.by_key.items()
                if len({source.mod.id for source in sources}) > 1}


class DefinitionIndexer:
    def __init__(self, registry: SemanticRegistry) -> None:
        self.registry = registry
    
    def build(self, mods: list[Mod]) -> DefinitionIndex:
        result: dict[DefinitionKey, list[DefinitionSource]] = {}
        replaced: set[str] = set()
        for mod_index, mod in enumerate(mods):
            if not mod.path.is_dir():
                continue
            if current_replace := self._replace_paths(mod):
                for key in tuple(result):
                    if any(any(str(source.path).casefold().startswith(prefix)
                               for prefix in current_replace)
                           for source in result[key]):
                        del result[key]
                replaced.update(current_replace)
            for file_path in sorted(mod.path.rglob("*.txt")):
                rel = PurePosixPath(file_path.relative_to(mod.path).as_posix())
                rules = self.registry.definitions_for(rel)
                if not rules:
                    continue
                document = parse_file(file_path)
                for rule in rules:
                    for pair, identifier in self._definitions(document.top_pairs(),
                                                              rule, rel):
                        key = DefinitionKey(self.registry.game, rule.name, identifier)
                        result.setdefault(key, []).append(
                            DefinitionSource(key, mod_index, mod, rel, pair))
        return DefinitionIndex(result)
    
    @staticmethod
    def _definitions(pairs: list[CWPair], rule: DefinitionRule,
                     path: PurePosixPath):
        if rule.type_per_file:
            if pairs:
                yield pairs[-1], path.stem
            return
        for pair in pairs:
            if rule.root_keys and pair.key_text.casefold() not in rule.root_keys:
                continue
            if rule.name_field and isinstance(pair.value, CWBlock):
                identifier = next((scalar_text(item.value)
                                   for item in pair.value.items
                                   if isinstance(item, CWPair)
                                   and item.key_text.casefold()
                                   == rule.name_field.casefold()), None)
            else:
                identifier = pair.key_text
            if identifier is not None:
                yield pair, identifier
    
    @staticmethod
    def _replace_paths(mod: Mod) -> set[str]:
        descriptor = mod.path / "descriptor.mod"
        if not descriptor.is_file():
            descriptor = mod.descriptor_path
        if not descriptor.is_file():
            return set()
        try:
            document = parse_file(descriptor)
        except (OSError, ValueError):
            return set()
        result = set()
        for pair in document.top_pairs():
            if pair.key_text.casefold() == "replace_path":
                if value := scalar_text(pair.value):
                    result.add(value.replace("\\", "/").strip("/").casefold())
        return result
