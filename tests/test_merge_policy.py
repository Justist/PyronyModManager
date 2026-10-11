from pathlib import PurePosixPath

from pmm.core.clausewitz import parse_text
from pmm.core.semantics.merge_policy import decide, DecisionKind
from pmm.core.semantics.model import SemanticContext, SemanticRole
from pmm.core.semantics.stellaris import stellaris_registry


def test_numeric_conflicts_are_not_added():
    left = parse_text("cost = 10").top_pairs()[0]
    right = parse_text("cost = 20").top_pairs()[0]
    decision = decide([left, right], SemanticContext(
        "stellaris", PurePosixPath("common/technology/test.txt"),
        SemanticRole.UNKNOWN), stellaris_registry())
    assert decision.kind is DecisionKind.LAST_WINS
    assert decision.value is right


def test_effect_order_requires_explicit_choice():
    left = parse_text("effect = { x = 1 }").top_pairs()[0]
    right = parse_text("effect = { x = 2 }").top_pairs()[0]
    decision = decide([left, right], SemanticContext(
        "stellaris", PurePosixPath("common/scripted_effects/test.txt"),
        SemanticRole.EFFECT), stellaris_registry())
    assert decision.kind is DecisionKind.CONFLICT
    assert decision.value is None
