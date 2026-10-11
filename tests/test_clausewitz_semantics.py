from pathlib import PurePosixPath

import pytest

from pmm.core.clausewitz import (
    CWBlock, CWParseError, parse_text, unparse_pair,
)
from pmm.core.semantics.model import SemanticContext, SemanticRole
from pmm.core.semantics.stellaris import stellaris_registry
from pmm.core.semantics.trigger_logic import block_to_expr, equivalent


def expression(source: str):
    block = parse_text(source).root
    return block_to_expr(block, stellaris_registry(), SemanticContext(
        "stellaris", PurePosixPath("common/scripted_triggers/test.txt"),
        SemanticRole.TRIGGER))


def test_keyword_case_is_ignored():
    assert equivalent(expression("AND = { a b }"), expression("and = { a b }"))


def test_variable_case_is_preserved():
    assert not equivalent(expression("AND = { MyVariable }"),
                          expression("AND = { myvariable }"))


def test_correct_de_morgan_equivalence():
    assert equivalent(expression("AND = { NOT = { a } NOT = { b } }"),
                      expression("NOT = { OR = { a b } }"))


def test_incorrect_de_morgan_is_not_equivalent():
    assert not equivalent(expression("AND = { NOT = { a } NOT = { b } }"),
                          expression("NOT = { AND = { a b } }"))


def test_nor_and_nand():
    assert equivalent(expression("NOR = { a b }"),
                      expression("NOT = { OR = { a b } }"))
    assert equivalent(expression("NAND = { a b }"),
                      expression("NOT = { AND = { a b } }"))


def test_not_requires_exactly_one_child():
    with pytest.raises(ValueError):
        expression("NOT = { a b }")


def test_quoted_and_unquoted_boolean_round_trip():
    pairs = parse_text('enabled = yes\nlabel = "yes"').top_pairs()
    assert [unparse_pair(pair) for pair in pairs] == [
        "enabled = yes", 'label = "yes"']


def test_escaped_string_round_trip():
    pair = parse_text(r'label = "say \"hello\""').top_pairs()[0]
    assert unparse_pair(pair) == r'label = "say \"hello\""'


def test_extended_operators_round_trip():
    pairs = parse_text("value ?= foo\nother == bar\nx != y").top_pairs()
    assert [unparse_pair(pair) for pair in pairs] == [
        "value ?= foo", "other == bar", "x != y"]


def test_repeated_effects_and_comments_survive():
    document = parse_text("effect = {\n# keep\nadd_resource = 1\nadd_resource = 1\n}")
    pair = document.top_pairs()[0]
    assert isinstance(pair.value, CWBlock)
    output = unparse_pair(pair)
    assert output.count("add_resource") == 2
    assert "# keep" in output


def test_malformed_blocks_fail():
    with pytest.raises(CWParseError):
        parse_text("x = { a = yes")
    with pytest.raises(CWParseError):
        parse_text("}")
