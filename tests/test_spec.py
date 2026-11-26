"""The analyzer refuses anything it cannot project, instead of guessing."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, ClassVar

import pytest
from pydantic import AwareDatetime, Field

from monograph.core.meta import OntClass, OntProp, SafeIdentifier, SafeText
from monograph.core.models import Entity, ModelBase
from monograph.core.rules import Condition, RequiredWhen, Rule
from monograph.core.spec import Card, Kind, SchemaError, SchemaSpec, analyze


def _thing(**namespace: object) -> type[ModelBase]:
    ns: dict[str, object] = {
        "__doc__": "A test thing.",
        "__ont__": OntClass(name="Thing", label="thing", path="thing", key=("code",)),
        "__annotations__": {"code": Annotated[SafeIdentifier, OntProp("code", "code")], "__ont__": ClassVar[OntClass]},
    }
    annotations = dict(namespace.pop("__annotations__", {}))  # type: ignore[call-overload]
    ns["__annotations__"].update(annotations)  # type: ignore[attr-defined]
    ns.update(namespace)
    return type("Thing", (Entity,), ns)


def _analyze(cls: type[ModelBase]) -> SchemaSpec:
    return analyze((Entity, cls), "0.0.0")


def test_real_model_analyzes(spec: SchemaSpec) -> None:
    aer = spec.by_name("AdverseEventReport")
    assert aer.field("drugs").card is Card.LIST
    assert aer.field("seriousness_criteria").card is Card.SET
    assert aer.field("patient_age_years").card is Card.OPTIONAL
    assert spec.by_name("Ingredient").field("substance").kind is Kind.REF
    assert {r.id for r in aer.rules} == {"serious-requires-criterion", "has-suspect"}


def test_minimal_thing_analyzes() -> None:
    spec = _analyze(_thing())
    assert spec.by_name("Thing").field("code").card is Card.ONE


@pytest.mark.parametrize(
    ("namespace", "message"),
    [
        ({"__annotations__": {"x": SafeText}}, "exactly one OntProp"),
        ({"__annotations__": {"x": Annotated[str, OntProp("x", "x")]}}, "plain str"),
        ({"__annotations__": {"x": Annotated[float, OntProp("x", "x")]}}, "float"),
        ({"__annotations__": {"x": Annotated[datetime, OntProp("x", "x")]}}, "AwareDatetime"),
        ({"__annotations__": {"x": Annotated[SafeText | None, OntProp("x", "x")]}}, "default to None"),
        ({"__annotations__": {"x": Annotated[SafeText, OntProp("x", "x")]}, "x": "d"}, "cannot have a default"),
        ({"__annotations__": {"x": Annotated[list[Entity], OntProp("x", "x")]}, "x": []}, "Ref"),
        ({"__annotations__": {"x": Annotated[list[str], OntProp("x", "x")]}, "x": []}, "plain scalars"),
        (
            {"__annotations__": {"x": Annotated[Decimal, OntProp("x", "x"), Field(multiple_of=2)]}},
            "no projection",
        ),
        (
            {"__annotations__": {"x": Annotated[int, OntProp("x", "x"), Field(pattern="^a$")]}},
            "str fields only",
        ),
        ({"__annotations__": {"x": Annotated[SafeText, OntProp("x", "x"), Field(gt=1)]}}, "numeric bounds"),
        ({"__annotations__": {"x": Annotated[dict[str, str], OntProp("x", "x")]}}, "not supported"),
        (
            {
                "__annotations__": {
                    "x": Annotated[bool, OntProp("x", "x")],
                    "__rules__": ClassVar[tuple[Rule, ...]],
                },
                "__rules__": (RequiredWhen(id="r", when=(Condition("x", (True,)),), then="nope", message="m"),),
            },
            "unknown field",
        ),
        (
            {
                "__annotations__": {
                    "x": Annotated[bool, OntProp("x", "x")],
                    "__rules__": ClassVar[tuple[Rule, ...]],
                },
                "__rules__": (RequiredWhen(id="r", when=(Condition("x", (1,)),), then="code", message="m"),),
            },
            "does not match the type",
        ),
    ],
)
def test_unprojectable_models_fail(namespace: dict[str, object], message: str) -> None:
    with pytest.raises(SchemaError, match=message):
        _analyze(_thing(**namespace))


def test_aware_datetime_is_supported() -> None:
    spec = _analyze(_thing(__annotations__={"at": Annotated[AwareDatetime, OntProp("at", "at")]}))
    assert spec.by_name("Thing").field("at").scalar is datetime


def test_missing_class_annotation_fails() -> None:
    cls = type("Bare", (Entity,), {"__doc__": "x"})
    with pytest.raises(SchemaError, match="unannotated class"):
        analyze((Entity, cls), "0.0.0")


def test_missing_docstring_fails() -> None:
    cls = _thing(__doc__=None)
    with pytest.raises(SchemaError, match="docstring"):
        _analyze(cls)


def test_conflicting_property_ranges_fail() -> None:
    a = _thing()
    b = type(
        "Other",
        (Entity,),
        {
            "__doc__": "Other.",
            "__ont__": OntClass(name="Other", label="other", path="other", key=("code",)),
            "__annotations__": {
                "code": Annotated[int, OntProp("code", "code")],
                "__ont__": ClassVar[OntClass],
            },
        },
    )
    with pytest.raises(SchemaError, match="different ranges"):
        analyze((Entity, a, b), "0.0.0")


def test_constraint_sites_cover_every_kind(spec: SchemaSpec) -> None:
    kinds = {s.kind for s in spec.constraint_sites()}
    assert {
        "pattern",
        "min_length",
        "max_length",
        "min_inclusive",
        "max_inclusive",
        "min_exclusive",
        "max_exclusive",
        "enum",
        "ref",
        "node",
        "closed",
        "required",
        "min_count",
        "max_count",
        "datatype",
    } <= kinds
