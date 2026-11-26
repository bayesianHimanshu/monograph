"""Deliberate breaks. Each mutation edits files in a throwaway copy of the repository.

Every edit is an exact text replacement that must match exactly once, so a mutation fails loudly
instead of silently doing nothing when the source changes under it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MODELS = Path("src/monograph/core/models.py")
VERSION = Path("src/monograph/core/version.py")
MAPPING = Path("src/monograph/rdf/mapping.py")
SHAPES = Path("generated/monograph.shapes.ttl")
OWL = Path("generated/monograph.owl.ttl")
TEST_DDL = Path("generated/monograph.test.ddl.sql")

Regenerate = Callable[[], None]


class MutationError(RuntimeError):
    """The mutation could not be applied (its target text is gone)."""


def replace_once(root: Path, relative: Path, old: str, new: str) -> None:
    path = root / relative
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise MutationError(f"{relative}: expected the target text once, found it {count} times: {old[:60]!r}")
    path.write_text(text.replace(old, new), encoding="utf-8")


@dataclass(frozen=True, slots=True)
class Mutation:
    id: str
    title: str
    expected_gate: str | None  # None means every gate must pass (a false-alarm probe)
    apply: Callable[[Path, Regenerate], None]


def _m1(root: Path, regen: Regenerate) -> None:
    replace_once(root, SHAPES, 'sh:pattern "^[0-9A-Z]{10}$"', 'sh:pattern "^[0-9A-Z]+$"')


_UNII_FIELD = (
    "    unii: Annotated[\n"
    '        SafeIdentifier, OntProp("unii", "UNII"), Field(pattern=safe(r"^[0-9A-Z]{10}$"), description="FDA UNII code.")\n'  # noqa: E501
    "    ]\n"
)


def _m2(root: Path, regen: Regenerate) -> None:
    synonym = '    synonym: Annotated[SafeText | None, OntProp("synonym", "synonym"), Field(min_length=1)] = None\n'
    replace_once(root, MODELS, _UNII_FIELD, _UNII_FIELD + synonym)


def _m3(root: Path, regen: Regenerate) -> None:
    path = root / OWL
    text = path.read_text(encoding="utf-8")
    block = re.compile(r"\nmono:Recall\n(    .*\n)+?(?=\n)", re.MULTILINE)
    new, count = block.subn("", text)
    if count != 1:
        raise MutationError(f"expected one mono:Recall block, found {count}")
    path.write_text(new, encoding="utf-8")


def _m4(root: Path, regen: Regenerate) -> None:
    replace_once(
        root,
        MODELS,
        '        SafeText | None,\n        OntProp("packageDescription", "package description"),\n'
        '        Field(min_length=1, max_length=2000, description="Package description as written in the source."),\n'
        "    ] = None\n",
        '        SafeText,\n        OntProp("packageDescription", "package description"),\n'
        '        Field(min_length=1, max_length=2000, description="Package description as written in the source."),\n'
        "    ]\n",
    )
    regen()


def _m5(root: Path, regen: Regenerate) -> None:
    replace_once(root, MODELS, 'OntProp("unii", "UNII")', 'OntProp("uniiCode", "UNII")')
    regen()


def _m6(root: Path, regen: Regenerate) -> None:
    replace_once(
        root,
        MODELS,
        'OntClass(name="Substance", label="Substance"',
        'OntClass(name="ChemicalSubstance", label="Substance"',
    )
    regen()


def _m7(root: Path, regen: Regenerate) -> None:
    replace_once(
        root,
        MODELS,
        '    INTRADERMAL = "INTRADERMAL"\n',
        '    INTRACAUDAL = "INTRACAUDAL"\n    INTRADERMAL = "INTRADERMAL"\n',
    )
    replace_once(root, VERSION, 'SCHEMA_VERSION = "1.0.0"', 'SCHEMA_VERSION = "1.1.0"')
    regen()


def _m8(root: Path, regen: Regenerate) -> None:
    anchor = (
        '    @model_validator(mode="after")\n    @pydantic_only(\n        id="product-ingredient-substances-unique",'
    )
    undeclared = (
        '    @model_validator(mode="after")\n    def name_is_trimmed(self) -> Self:\n'
        "        if self.name != self.name.strip():\n"
        '            raise ValueError("name has surrounding whitespace")\n'
        "        return self\n\n"
    )
    replace_once(root, MODELS, anchor, undeclared + anchor)


def _m9(root: Path, regen: Regenerate) -> None:
    replace_once(root, MODELS, 'pattern=safe(r"^[0-9A-Z]{10}$")', 'pattern=safe(r"^\\w{10}$")')


def _m10(root: Path, regen: Regenerate) -> None:
    replace_once(
        root,
        MAPPING,
        "            value = instance.__dict__[f.name]\n            if value is None:\n                continue\n",
        "            value = instance.__dict__[f.name]\n            if value is None:\n                continue\n"
        '            if f.prop.name == "strengthValue":\n                continue\n',
    )


def _m11(root: Path, regen: Regenerate) -> None:
    replace_once(
        root, TEST_DDL, "REFERENCES substance (iri) DEFERRABLE INITIALLY IMMEDIATE", "REFERENCES substance (iri)"
    )


MUTATIONS: tuple[Mutation, ...] = (
    Mutation("M1", "Hand-edit generated SHACL to loosen a pattern", "drift", _m1),
    Mutation("M2", "Add a field to a model without regenerating", "drift", _m2),
    Mutation("M3", "Hand-edit generated OWL to drop a class", "drift", _m3),
    Mutation("M4", "Make an optional field required without a major version bump", "breaking", _m4),
    Mutation("M5", "Rename a property without a major bump", "breaking", _m5),
    Mutation("M6", "Change a class IRI", "breaking", _m6),
    Mutation("M7", "Add an enum member with a minor bump", None, _m7),
    Mutation("M8", "Add a custom validator with no rule or pydantic_only declaration", "lint", _m8),
    Mutation("M9", "Introduce a regex outside the safe subset", "generation", _m9),
    Mutation("M10", "Break the instance mapping for one property", "agreement|tests", _m10),
    Mutation("M11", "Hand-edit the test-profile DDL to make a foreign key non-deferrable", "drift", _m11),
)

BASELINE = Mutation("M0", "No mutation (control: every gate must pass)", None, lambda root, regen: None)
