"""Cross-field rule DSL.

Only two rule patterns exist, both expressible in SHACL Core:

* RequiredWhen: if field `when` equals `equals`, field `then` must be present.
* AtLeast: at least `min_count` items of list field `field` have `where == equals`.

Each rule is projected twice: into Pydantic by the model base class's `@rule_engine` validator, which
calls `Rule.holds()`, and into SHACL by `gen/shacl.py`. Any other validator must be marked
`@pydantic_only(...)`, and `monograph check lint` fails the build if it is not.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

RuleValue = bool | int | str  # a StrEnum member is a str

F = TypeVar("F", bound=Callable[..., Any])

RULE_ENGINE_MARK = "__monograph_rule_engine__"
PYDANTIC_ONLY_MARK = "__monograph_pydantic_only__"


def _present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, Collection) and not isinstance(value, str):
        return len(value) > 0
    return True


@dataclass(frozen=True, slots=True)
class RequiredWhen:
    """If `when == equals` then `then` is present (not None, and non-empty for collections)."""

    id: str
    when: str
    equals: RuleValue
    then: str
    message: str

    def holds(self, model: BaseModel) -> bool:
        if getattr(model, self.when, None) != self.equals:
            return True
        return _present(getattr(model, self.then, None))


@dataclass(frozen=True, slots=True)
class AtLeast:
    """At least `min_count` items of list field `field` have `where == equals`."""

    id: str
    field: str
    where: str
    equals: RuleValue
    min_count: int
    message: str

    def holds(self, model: BaseModel) -> bool:
        items = getattr(model, self.field, ())
        count = sum(1 for item in items if getattr(item, self.where, None) == self.equals)
        return count >= self.min_count


Rule = RequiredWhen | AtLeast


class RuleViolation(ValueError):
    """Raised by the rule engine. Carries the rule id so verdicts can name the rule."""

    def __init__(self, rule_id: str, message: str) -> None:
        super().__init__(f"[{rule_id}] {message}")
        self.rule_id = rule_id


class PydanticOnlyViolation(ValueError):
    """Raised by a `pydantic_only` validator. Verdicts use it to classify declared leaks."""

    def __init__(self, constraint_id: str, message: str) -> None:
        super().__init__(f"[{constraint_id}] {message}")
        self.constraint_id = constraint_id


@dataclass(frozen=True, slots=True)
class PydanticOnly:
    """The declaration attached to a validator that has no SHACL projection."""

    id: str
    reason: str


def rule_engine(func: F) -> F:
    """Mark the single validator that evaluates `__rules__`. Only the model base class uses this."""
    setattr(func, RULE_ENGINE_MARK, True)
    return func


def pydantic_only(id: str, reason: str) -> Callable[[F], F]:
    """Declare that a validator has no SHACL Core projection, and why. Apply it under `@model_validator`."""

    def mark(func: F) -> F:
        setattr(func, PYDANTIC_ONLY_MARK, PydanticOnly(id=id, reason=reason))
        return func

    return mark
