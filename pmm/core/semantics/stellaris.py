"""Stellaris semantic-profile access."""
from functools import lru_cache
from pathlib import Path

from .registry import SemanticRegistry


@lru_cache(maxsize=1)
def stellaris_registry() -> SemanticRegistry:
    profile = Path(__file__).resolve().parents[2] / "data" / "semantics" / \
              "stellaris" / "profile.json"
    return SemanticRegistry.load(profile)
