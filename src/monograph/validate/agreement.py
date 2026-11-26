"""Agreement: the Pydantic verdict equals the SHACL verdict, record by record."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel

from monograph.validate.pydantic_side import PydanticValidator
from monograph.validate.shacl_side import ShaclValidator
from monograph.validate.verdict import Verdict


class Outcome(StrEnum):
    AGREE_VALID = "agree_valid"
    AGREE_INVALID = "agree_invalid"
    DECLARED_LEAK = "declared_leak"  # Pydantic rejects only through pydantic_only validators; SHACL accepts
    DISAGREE = "disagree"


@dataclass(frozen=True, slots=True)
class AgreementResult:
    pydantic: Verdict
    shacl: Verdict

    @property
    def outcome(self) -> Outcome:
        if self.pydantic.valid and self.shacl.valid:
            return Outcome.AGREE_VALID
        if not self.pydantic.valid and not self.shacl.valid:
            return Outcome.AGREE_INVALID
        if not self.pydantic.valid and self.shacl.valid and self.pydantic.only_pydantic_only:
            return Outcome.DECLARED_LEAK
        return Outcome.DISAGREE

    @property
    def agrees(self) -> bool:
        return self.outcome is not Outcome.DISAGREE


class AgreementChecker:
    def __init__(self, pydantic: PydanticValidator, shacl: ShaclValidator) -> None:
        self._pydantic = pydantic
        self._shacl = shacl

    def check(self, instance: BaseModel) -> AgreementResult:
        return AgreementResult(self._pydantic.check(instance), self._shacl.check(instance))
