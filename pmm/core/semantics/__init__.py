"""Game-aware interpretation for Clausewitz syntax."""
from .model import KeyKind, SemanticContext, SemanticKey, SemanticRole
from .registry import DefinitionRule, SemanticRegistry
from .stellaris import stellaris_registry

__all__ = [
    "DefinitionRule", "KeyKind", "SemanticContext", "SemanticKey",
    "SemanticRegistry", "SemanticRole", "stellaris_registry",
]
