"""Shared verdict types for the Pydantic, SHACL and Postgres sides."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Violation:
    path: str
    kind: str
    message: str
    pydantic_only: str | None = None  # the declared constraint id, for pydantic_only failures

    def describe(self) -> str:
        return f"{self.path}: {self.kind}: {self.message}"


@dataclass(frozen=True, slots=True)
class Verdict:
    valid: bool
    violations: tuple[Violation, ...] = ()

    @property
    def only_pydantic_only(self) -> bool:
        return bool(self.violations) and all(v.pydantic_only for v in self.violations)
