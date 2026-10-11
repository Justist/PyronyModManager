from pathlib import Path

from pmm.core.models import Mod
from pmm.core.semantics.definitions import DefinitionIndexer
from pmm.core.semantics.stellaris import stellaris_registry


def mod(identifier: str, name: str, root: Path) -> Mod:
    return Mod(name=name, path=root, descriptor_path=root / f"{identifier}.mod",
               remote_id=identifier)


def test_collision_across_different_filenames(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    (first / "common/traits").mkdir(parents=True)
    (second / "common/traits").mkdir(parents=True)
    (first / "common/traits/a.txt").write_text("same_trait = { cost = 1 }")
    (second / "common/traits/b.txt").write_text("same_trait = { cost = 2 }")
    index = DefinitionIndexer(stellaris_registry()).build([
        mod("a", "First", first), mod("b", "Second", second)])
    assert len(index.collisions) == 1


def test_identifier_case_is_preserved(tmp_path):
    root = tmp_path / "mod"
    (root / "common/traits").mkdir(parents=True)
    (root / "common/traits/traits.txt").write_text(
        "MyTrait = { cost = 1 }\nmytrait = { cost = 2 }")
    index = DefinitionIndexer(stellaris_registry()).build([mod("a", "A", root)])
    assert {key.identifier for key in index.by_key} == {"MyTrait", "mytrait"}


def test_replace_path_removes_earlier_definitions(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    (first / "common/traits").mkdir(parents=True)
    (second / "common/traits").mkdir(parents=True)
    (first / "common/traits/a.txt").write_text("old_trait = { cost = 1 }")
    (second / "common/traits/b.txt").write_text("new_trait = { cost = 2 }")
    (second / "descriptor.mod").write_text('replace_path = "common/traits"')
    index = DefinitionIndexer(stellaris_registry()).build([
        mod("a", "First", first), mod("b", "Second", second)])
    assert {key.identifier for key in index.by_key} == {"new_trait"}
