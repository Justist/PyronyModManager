"""Small Boolean algebra for Stellaris trigger equivalence.

It intentionally proves only structural propositional equivalence.  It does not
expand scripted triggers, reason about scopes, or infer implication.
"""

from dataclasses import dataclass
from typing import Tuple, TypeAlias

from pmm.core.clausewitz import (
    CWBlock, CWComment, CWPair, CWRaw, CWScalar, unparse,
)
from .model import SemanticContext
from .registry import SemanticRegistry


@dataclass(frozen=True, slots=True)
class Atom:
    fingerprint: Tuple


@dataclass(frozen=True, slots=True)
class Not:
    child: "TriggerExpr"


@dataclass(frozen=True, slots=True)
class All:
    children: Tuple["TriggerExpr", ...]


@dataclass(frozen=True, slots=True)
class Any:
    children: Tuple["TriggerExpr", ...]


TriggerExpr: TypeAlias = Atom | Not | All | Any


class TriggerSemanticError(ValueError):
    pass


def _value_fingerprint(value, registry: SemanticRegistry,
                       context: SemanticContext) -> Tuple:
    if isinstance(value, CWScalar):
        return "scalar", value.text, value.kind.name, value.quoted
    if isinstance(value, CWBlock):
        return ("block", tuple(_item_fingerprint(item, registry, context)
                               for item in value.items
                               if not isinstance(item, CWComment)))
    return "value", str(value)


def _item_fingerprint(item, registry: SemanticRegistry,
                      context: SemanticContext) -> Tuple:
    if isinstance(item, CWPair):
        key = registry.semantic_key(item.key_text, context)
        return ("pair", key.kind.name, key.value, item.op,
                _value_fingerprint(item.value, registry,
                                   context.child(item.key_text)))
    if isinstance(item, CWRaw):
        # Bare names are predicates/variables, not known syntax keywords.
        return ("raw", item.value.text, item.value.kind.name,
                item.value.quoted)
    if isinstance(item, CWBlock):
        return _value_fingerprint(item, registry, context)
    return "other", unparse(item)


def item_to_expr(item, registry: SemanticRegistry,
                 context: SemanticContext) -> TriggerExpr:
    if not isinstance(item, CWPair):
        return Atom(_item_fingerprint(item, registry, context))
    operator = registry.boolean_operators.get(item.key_text.casefold())
    if operator is None:
        return Atom(_item_fingerprint(item, registry, context))
    if not isinstance(item.value, CWBlock):
        raise TriggerSemanticError(f"{item.key_text} requires a block")
    children = tuple(item_to_expr(child, registry,
                                  context.child(item.key_text))
                     for child in item.value.items
                     if not isinstance(child, CWComment))
    if operator.maximum is not None and len(children) > operator.maximum:
        raise TriggerSemanticError(
            f"{item.key_text} accepts at most {operator.maximum} child(ren)")
    if len(children) < operator.minimum:
        raise TriggerSemanticError(
            f"{item.key_text} requires at least {operator.minimum} child(ren)")
    if operator.kind == "all":
        return All(children)
    if operator.kind == "any":
        return Any(children)
    if operator.kind == "not":
        return Not(children[0])
    if operator.kind == "not-all":
        return Not(All(children))
    if operator.kind == "not-any":
        return Not(Any(children))
    raise TriggerSemanticError(f"unsupported Boolean operator {operator.kind!r}")


def block_to_expr(block: CWBlock, registry: SemanticRegistry,
                  context: SemanticContext) -> TriggerExpr:
    return All(tuple(item_to_expr(item, registry, context)
                     for item in block.items
                     if not isinstance(item, CWComment)))


def to_nnf(expression: TriggerExpr, negate: bool = False) -> TriggerExpr:
    if isinstance(expression, Atom):
        return Not(expression) if negate else expression
    if isinstance(expression, Not):
        return to_nnf(expression.child, not negate)
    children = tuple(to_nnf(child, negate) for child in expression.children)
    if isinstance(expression, All):
        return Any(children) if negate else All(children)
    return All(children) if negate else Any(children)


def fingerprint(expression: TriggerExpr) -> tuple:
    expression = to_nnf(expression)
    if isinstance(expression, Atom):
        return "atom", expression.fingerprint
    if isinstance(expression, Not):
        return "not", fingerprint(expression.child)
    expected = All if isinstance(expression, All) else Any
    name = "all" if expected is All else "any"
    children: list[TriggerExpr] = []
    for child in expression.children:
        child = normalize(child)
        if isinstance(child, expected):
            children.extend(child.children)
        else:
            children.append(child)
    # AND/OR are associative, commutative, and idempotent for predicates.
    unique = {fingerprint(child) for child in children}
    ordered = tuple(sorted(unique, key=repr))
    return ordered[0] if len(ordered) == 1 else (name, ordered)


def normalize(expression: TriggerExpr) -> TriggerExpr:
    value = fingerprint(expression)
    return _from_fingerprint(value)


def _from_fingerprint(value: tuple) -> TriggerExpr:
    kind = value[0]
    if kind == "atom":
        return Atom(value[1])
    if kind == "not":
        return Not(_from_fingerprint(value[1]))
    children = tuple(_from_fingerprint(child) for child in value[1])
    return All(children) if kind == "all" else Any(children)


def equivalent(left: TriggerExpr, right: TriggerExpr) -> bool:
    return fingerprint(left) == fingerprint(right)
