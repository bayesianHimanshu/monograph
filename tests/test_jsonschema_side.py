"""The generated JSON Schema against real records (regression guard for the lost-pattern finding)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from monograph import wiring
from monograph.core.instances import Instantiator
from monograph.core.spec import SchemaSpec
from monograph.gen.artifacts import JSON_SCHEMA_FILE
from monograph.validate.fixtures import FixtureLoader
from monograph.validate.jsonschema_side import DECLARED_LEAKS, JsonSchemaValidator, UnsupportedKeyword
from monograph.validate.pydantic_side import PydanticValidator

from .conftest import ROOT, TODAY

VALIDATOR = JsonSchemaValidator(json.loads((ROOT / "generated" / JSON_SCHEMA_FILE).read_text()))

JSON_SCHEMA_LEAKS = DECLARED_LEAKS


def test_every_valid_fixture_validates(fixtures: FixtureLoader) -> None:
    rejected = {f.name: VALIDATOR.errors_for(f.cls.__name__, f.record) for f in fixtures.valid()}
    assert {k: v for k, v in rejected.items() if v} == {}


def test_real_openfda_records_that_pydantic_accepts_validate(spec: SchemaSpec) -> None:
    instantiator, pydantic = Instantiator(spec), PydanticValidator(TODAY)
    candidates = wiring.conformance_run(spec, ROOT).candidates(wiring.openfda_fixtures(ROOT))
    checked, rejected = 0, []
    for c in candidates:
        if pydantic.check(instantiator.instantiate(spec.by_name(c.cls).cls, c.record)).valid:
            checked += 1
            if errors := VALIDATOR.errors_for(c.cls, c.record):
                rejected.append((c.cls, c.source, errors[:2]))
    assert checked > 100
    assert rejected == []


def test_invalid_fixtures_are_rejected_except_declared_leaks(fixtures: FixtureLoader) -> None:
    accepted = sorted(f.name for f in fixtures.invalid() if not VALIDATOR.errors_for(f.cls.__name__, f.record))
    assert accepted == sorted(JSON_SCHEMA_LEAKS)


def test_unknown_keywords_fail_closed() -> None:
    with pytest.raises(UnsupportedKeyword):
        JsonSchemaValidator({"$defs": {}}).errors("x", {"type": "string", "unevaluatedProperties": False})


@pytest.mark.parametrize(
    ("schema", "value", "ok"),
    [
        ({"type": "string", "pattern": "^[A-Z]{3}$"}, "ABC", True),
        ({"type": "string", "pattern": "^[A-Z]{3}$"}, "ABCD", False),
        ({"type": "string", "format": "date"}, "2025-02-30", False),
        ({"type": "string", "not": {"pattern": "[\\x00]"}}, "a\x00", False),
        ({"type": "array", "contains": {"const": 1}, "minContains": 2}, [1, 2, 1], True),
        (
            {"if": {"properties": {"a": {"const": True}}, "required": ["a"]}, "then": {"required": ["b"]}},
            {"a": True},
            False,
        ),
    ],
)
def test_keywords(schema: dict[str, Any], value: Any, ok: bool) -> None:
    assert (not JsonSchemaValidator({"$defs": {}}).errors(value, schema)) is ok
