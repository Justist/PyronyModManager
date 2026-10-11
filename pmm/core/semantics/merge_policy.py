"""Conservative semantic merge decisions.

Type information can prove equivalence or invalidity; it cannot prove that
adding, minimising, maximising, appending, or unioning two user values is the
intended compatibility result.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import auto, Enum

from pmm.core.clausewitz import CWBlock, CWPair
from .model import SemanticContext, SemanticRole
from .registry import SemanticRegistry
from .trigger_logic import block_to_expr, equivalent


class DecisionKind(Enum):
    EQUIVALENT = auto()
    LAST_WINS = auto()
    CONFLICT = auto()


@dataclass(frozen=True, slots=True)
class MergeDecision:
    kind: DecisionKind
    value: CWPair | None
    reason: str
    sources: tuple[CWPair, ...]


def decide(versions: list[CWPair], context: SemanticContext,
           registry: SemanticRegistry) -> MergeDecision:
    sources = tuple(versions)
    if not versions:
        return MergeDecision(DecisionKind.CONFLICT, None, "No source values", ())
    if len(versions) == 1:
        return MergeDecision(DecisionKind.LAST_WINS, versions[0],
                             "Only one source value", sources)
    if context.role is SemanticRole.TRIGGER and all(
            isinstance(version.value, CWBlock) for version in versions):
        first = block_to_expr(versions[0].value, registry, context)
        if all(equivalent(first, block_to_expr(version.value, registry, context))
               for version in versions[1:]):
            return MergeDecision(DecisionKind.EQUIVALENT, versions[-1],
                                 "Trigger expressions are semantically equivalent",
                                 sources)
        return MergeDecision(DecisionKind.CONFLICT, None,
                             "Trigger expressions differ", sources)
    if context.role in {SemanticRole.EFFECT, SemanticRole.ORDERED_SEQUENCE}:
        return MergeDecision(DecisionKind.CONFLICT, None,
                             "Order and repetition are significant", sources)
    if context.role in {SemanticRole.MODIFIER_MAP, SemanticRole.UNORDERED_SET}:
        return MergeDecision(DecisionKind.CONFLICT, None,
                             "A game/profile-specific user policy is required",
                             sources)
    return MergeDecision(DecisionKind.LAST_WINS, versions[-1],
                         "Unknown context preserves game load order", sources)
