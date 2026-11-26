from __future__ import annotations

import types
from typing import Annotated, ClassVar, Self

from pydantic import AfterValidator, model_validator

from monograph.core import models
from monograph.core.meta import OntClass, OntProp
from monograph.core.rules import pydantic_only
from monograph.validate.lint import ValidatorLinter


def test_source_models_pass_lint() -> None:
    report = ValidatorLinter(models.MODELS, models).run()
    assert report.ok, report.problems
    assert report.rule_engines == ("ModelBase.generated_rules",)
    assert {d.declaration.id for d in report.declared} == {
        "product-ingredient-substances-unique",
        "label-effective-date-not-future",
        "label-section-codes-unique",
    }
    assert all(d.declaration.reason for d in report.declared)


def _module_with(cls: type) -> types.ModuleType:
    module = types.ModuleType("fake_models")
    cls.__module__ = module.__name__
    module.Thing = cls  # type: ignore[attr-defined]
    return module


def test_undeclared_model_validator_fails() -> None:
    class Thing(models.Entity):
        """T."""

        __ont__: ClassVar[OntClass] = OntClass(name="Thing", label="t", path="t", key=("code",))
        code: Annotated[str, OntProp("code", "code")]

        @model_validator(mode="after")
        def sneaky(self) -> Self:
            return self

    report = ValidatorLinter((Thing,), _module_with(Thing)).run()
    assert [p.validator for p in report.problems] == ["sneaky"]


def test_annotated_validator_needs_declaration() -> None:
    def upper(v: str) -> str:
        return v.upper()

    declared = pydantic_only(id="x", reason="r")(lambda v: v)

    class Thing(models.Entity):
        """T."""

        __ont__: ClassVar[OntClass] = OntClass(name="Thing", label="t", path="t", key=("code",))
        code: Annotated[str, OntProp("code", "code"), AfterValidator(upper)]
        other: Annotated[str, OntProp("other", "other"), AfterValidator(declared)]

    report = ValidatorLinter((Thing,), None).run()
    assert [p.validator for p in report.problems] == ["code:AfterValidator"]
    assert [d.declaration.id for d in report.declared] == ["x"]


def test_unlisted_model_class_fails() -> None:
    class Thing(models.Entity):
        """T."""

        __ont__: ClassVar[OntClass] = OntClass(name="Thing", label="t", path="t", key=("code",))
        code: Annotated[str, OntProp("code", "code")]

    report = ValidatorLinter((), _module_with(Thing)).run()
    assert any("not listed" in p.problem for p in report.problems)
