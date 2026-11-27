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

Cli = Callable[..., None]  # runs `monograph <args>` inside the mutant copy


class MutationError(RuntimeError):
    """The mutation could not be applied (its target text is gone)."""


def replace_once(root: Path, relative: Path, old: str, new: str) -> None:
    path = root / relative
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise MutationError(f"{relative}: expected the target text once, found it {count} times: {old[:60]!r}")
    path.write_text(text.replace(old, new), encoding="utf-8")


def bump_version(root: Path, minor: bool) -> None:
    """Bump SCHEMA_VERSION in the copy: the next minor (x.y+1.0) or the next major (x+1.0.0)."""
    text = (root / VERSION).read_text(encoding="utf-8")
    match = re.search(r'SCHEMA_VERSION = "(\d+)\.(\d+)\.(\d+)"', text)
    if match is None:
        raise MutationError("SCHEMA_VERSION not found")
    major, minor_n = int(match.group(1)), int(match.group(2))
    new = f"{major}.{minor_n + 1}.0" if minor else f"{major + 1}.0.0"
    replace_once(root, VERSION, match.group(0), f'SCHEMA_VERSION = "{new}"')


@dataclass(frozen=True, slots=True)
class Mutation:
    id: str
    title: str
    expected_gate: str | None  # None means every gate must pass (a false-alarm probe)
    apply: Callable[[Path, Cli], None]
    commit: bool = True  # the runner commits the mutant copy, so only uncommitted changes look dirty


def _m1(root: Path, cli: Cli) -> None:
    replace_once(root, SHAPES, 'sh:pattern "^[0-9A-Z]{10}$"', 'sh:pattern "^[0-9A-Z]+$"')


_UNII_FIELD = (
    "    unii: Annotated[\n"
    '        SafeIdentifier, OntProp("unii", "UNII"), Field(pattern=safe(r"^[0-9A-Z]{10}$"), description="FDA UNII code.")\n'  # noqa: E501
    "    ]\n"
)


def _m2(root: Path, cli: Cli) -> None:
    synonym = '    synonym: Annotated[SafeText | None, OntProp("synonym", "synonym"), Field(min_length=1)] = None\n'
    replace_once(root, MODELS, _UNII_FIELD, _UNII_FIELD + synonym)


def _m3(root: Path, cli: Cli) -> None:
    path = root / OWL
    text = path.read_text(encoding="utf-8")
    block = re.compile(r"\nmono:Recall\n(    .*\n)+?(?=\n)", re.MULTILINE)
    new, count = block.subn("", text)
    if count != 1:
        raise MutationError(f"expected one mono:Recall block, found {count}")
    path.write_text(new, encoding="utf-8")


def _m4(root: Path, cli: Cli) -> None:
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
    cli("gen")


def _m5(root: Path, cli: Cli) -> None:
    replace_once(root, MODELS, 'OntProp("unii", "UNII")', 'OntProp("uniiCode", "UNII")')
    cli("gen")


def _m6(root: Path, cli: Cli) -> None:
    replace_once(
        root,
        MODELS,
        'OntClass(name="Substance", label="Substance"',
        'OntClass(name="ChemicalSubstance", label="Substance"',
    )
    cli("gen")


def _m7(root: Path, cli: Cli) -> None:
    # INTRAVENOUS BOLUS (C38274) is in FDA's Data Standards Manual list but not in the SPL list we ship. It
    # also changes the live extraction schema, which H4 no longer reads (it loads its pinned copy).
    replace_once(
        root,
        MODELS,
        '    INTRAVENOUS = "INTRAVENOUS"  # C38276\n',
        '    INTRAVENOUS = "INTRAVENOUS"  # C38276\n    INTRAVENOUS_BOLUS = "INTRAVENOUS BOLUS"  # C38274\n',
    )
    bump_version(root, minor=True)
    cli("gen")
    cli("release")  # an additive change is complete only with its snapshot


def _m8(root: Path, cli: Cli) -> None:
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


def _m9(root: Path, cli: Cli) -> None:
    replace_once(root, MODELS, 'pattern=safe(r"^[0-9A-Z]{10}$")', 'pattern=safe(r"^\\w{10}$")')


def _m10(root: Path, cli: Cli) -> None:
    replace_once(
        root,
        MAPPING,
        "            value = instance.__dict__[f.name]\n            if value is None:\n                continue\n",
        "            value = instance.__dict__[f.name]\n            if value is None:\n                continue\n"
        '            if f.prop.name == "strengthValue":\n                continue\n',
    )


def _m11(root: Path, cli: Cli) -> None:
    replace_once(
        root,
        TEST_DDL,
        "recalling_firm_iri text NOT NULL REFERENCES organization (iri) DEFERRABLE INITIALLY IMMEDIATE",
        "recalling_firm_iri text NOT NULL REFERENCES organization (iri)",
    )


def _m12(root: Path, cli: Cli) -> None:
    bump_version(root, minor=False)
    cli("gen")


JSONSCHEMA_GEN = Path("src/monograph/gen/jsonschema.py")


def _m13(root: Path, cli: Cli) -> None:
    # Reproduces the H6.1 regression: the generator itself stops writing `pattern`, and the artifacts are
    # regenerated, so the drift check stays green.
    replace_once(root, JSONSCHEMA_GEN, '        ("pattern", k.pattern),\n', "")
    cli("gen")


AGE_JSON = Path("generated/monograph.age.json")
AGE_GEN = Path("src/monograph/gen/age.py")


def _m14(root: Path, cli: Cli) -> None:
    # Hand-edit the generated projection: drop the RECALLING_FIRM edge label.
    import json

    path = root / AGE_JSON
    data = json.loads(path.read_text(encoding="utf-8"))
    before = len(data["edges"])
    data["edges"] = [e for e in data["edges"] if e["label"] != "RECALLING_FIRM"]
    if len(data["edges"]) != before - 1:
        raise MutationError("RECALLING_FIRM edge label not found")
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _m15(root: Path, cli: Cli) -> None:
    # The projection generator stops emitting edges for single (non-collection) references, regenerated, so
    # the drift check stays green.
    replace_once(
        root,
        AGE_GEN,
        "                b.edge(edge_label(f.prop.name), source)\n",
        "                if f.is_collection:\n                    b.edge(edge_label(f.prop.name), source)\n",
    )
    cli("gen")


H4_PREREG = Path("docs/h4/PREREG.md")


def _m16(root: Path, cli: Cli) -> None:
    # Edit a committed pre-registration after the fact: loosen H4's decision threshold.
    path = root / H4_PREREG
    text = path.read_text(encoding="utf-8")
    path.write_text(text + "\nAmendment: the threshold is relaxed to 0.02.\n", encoding="utf-8")


H4_PINNED_SCHEMA = Path("experiments/h4/frozen/monograph.extraction.schema.json")


def _m17(root: Path, cli: Cli) -> None:
    # Tamper with H4's pinned extraction schema: drop one dosage form from the enum H4 extracted against.
    replace_once(root, H4_PINNED_SCHEMA, '        "AEROSOL, FOAM",\n', "")


def _m18(root: Path, cli: Cli) -> None:
    # A stray untracked file in the tree (left uncommitted): a test run must refuse to start.
    (root / "notes-from-a-debugging-session.txt").write_text("try a lower threshold\n", encoding="utf-8")


MUTATIONS: tuple[Mutation, ...] = (
    Mutation("M1", "Hand-edit generated SHACL to loosen a pattern", "drift", _m1),
    Mutation("M2", "Add a field to a model without regenerating", "drift", _m2),
    Mutation("M3", "Hand-edit generated OWL to drop a class", "drift", _m3),
    Mutation("M4", "Make an optional field required without a major version bump", "breaking", _m4),
    Mutation("M5", "Rename a property without a major bump", "breaking", _m5),
    Mutation("M6", "Change a class IRI", "breaking", _m6),
    Mutation("M7", "Add an enum member with a minor bump and its release snapshot", None, _m7),
    Mutation("M8", "Add a custom validator with no rule or pydantic_only declaration", "lint", _m8),
    Mutation("M9", "Introduce a regex outside the safe subset", "generation", _m9),
    Mutation("M10", "Break the instance mapping for one property", "agreement|tests", _m10),
    Mutation("M11", "Hand-edit the test-profile DDL to make a foreign key non-deferrable", "drift", _m11),
    Mutation("M12", "Bump the major version without a release snapshot", "breaking", _m12),
    Mutation("M13", "JSON Schema generator drops every pattern (regenerated)", "agreement|tests", _m13),
    Mutation("M14", "Hand-edit the generated graph projection to drop an edge label", "drift", _m14),
    Mutation("M15", "Graph projection generator drops single-reference edges (regenerated)", "tests", _m15),
    Mutation("M16", "Edit a committed PREREG.md after its first commit", "prereg", _m16),
    Mutation("M17", "Tamper with H4's pinned extraction schema", "tests", _m17),
    Mutation("M18", "Leave a stray untracked file: a test run must refuse to start", "clean", _m18, commit=False),
)

BASELINE = Mutation("M0", "No mutation (control: every gate must pass)", None, lambda root, cli: None)
