"""The Pydantic verdict on an unvalidated instance."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ValidationError

from monograph.core.rules import PydanticOnlyViolation, RuleViolation
from monograph.validate.verdict import Verdict, Violation


class PydanticValidator:
    """Re-validates a `model_construct` instance. `today` is the clock for date-relative validators."""

    def __init__(self, today: date) -> None:
        self._context = {"today": today}

    @property
    def context(self) -> dict[str, date]:
        return dict(self._context)

    def check(self, instance: BaseModel) -> Verdict:
        try:
            type(instance).model_validate(instance, context=self._context)
        except ValidationError as exc:
            return Verdict(False, tuple(_violation(e) for e in exc.errors(include_url=False)))
        return Verdict(True)


def _violation(error: object) -> Violation:
    assert isinstance(error, dict)
    path = ".".join(str(p) for p in error.get("loc", ())) or "<record>"
    cause = (error.get("ctx") or {}).get("error")
    if isinstance(cause, PydanticOnlyViolation):
        return Violation(path, "pydantic_only", str(cause), pydantic_only=cause.constraint_id)
    if isinstance(cause, RuleViolation):
        return Violation(path, f"rule:{cause.rule_id}", str(cause))
    return Violation(path, str(error.get("type")), str(error.get("msg")))
