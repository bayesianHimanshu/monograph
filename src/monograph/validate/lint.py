"""Validator lint: every validator is generated from a rule or declared `pydantic_only`.

The declared ones are the leak inventory. An undeclared one fails the build.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from dataclasses import dataclass
from types import ModuleType

from pydantic import BaseModel

from monograph.core.rules import PYDANTIC_ONLY_MARK, RULE_ENGINE_MARK, PydanticOnly

_VALIDATOR_KINDS = ("validators", "field_validators", "root_validators", "model_validators")


@dataclass(frozen=True, slots=True)
class Declared:
    cls: str
    validator: str
    declaration: PydanticOnly


@dataclass(frozen=True, slots=True)
class Problem:
    cls: str
    validator: str
    problem: str


@dataclass(frozen=True, slots=True)
class LintReport:
    rule_engines: tuple[str, ...]
    declared: tuple[Declared, ...]
    problems: tuple[Problem, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


class ValidatorLinter:
    """Inspects the validators Pydantic actually registered, not the source text."""

    def __init__(self, models: Sequence[type[BaseModel]], module: ModuleType | None = None) -> None:
        self._models = tuple(models)
        self._module = module

    def run(self) -> LintReport:
        engines: list[str] = []
        declared: list[Declared] = []
        problems: list[Problem] = []
        targets = list(self._models)
        if self._module is not None:
            targets += [
                obj
                for obj in vars(self._module).values()
                if inspect.isclass(obj)
                and issubclass(obj, BaseModel)
                and obj.__module__ == self._module.__name__
                and obj not in targets
            ]
        for cls in targets:
            decorators = cls.__pydantic_decorators__
            for kind in _VALIDATOR_KINDS:
                for name, dec in getattr(decorators, kind).items():
                    if name not in vars(cls):
                        continue  # inherited; reported on the class that defines it
                    func = dec.func
                    where = f"{cls.__name__}.{name}"
                    if getattr(func, RULE_ENGINE_MARK, False):
                        engines.append(where)
                    elif isinstance(getattr(func, PYDANTIC_ONLY_MARK, None), PydanticOnly):
                        declared.append(Declared(cls.__name__, name, getattr(func, PYDANTIC_ONLY_MARK)))
                    else:
                        problems.append(
                            Problem(
                                cls.__name__, name, "validator is neither rule-generated nor pydantic_only"
                            )
                        )
            for field_name, info in cls.model_fields.items():
                if field_name not in getattr(cls, "__annotations__", {}):
                    continue
                for m in info.metadata:
                    func = getattr(m, "func", None)
                    if func is None:
                        continue
                    where = f"{field_name}:{type(m).__name__}"
                    mark = getattr(func, PYDANTIC_ONLY_MARK, None)
                    if isinstance(mark, PydanticOnly):
                        declared.append(Declared(cls.__name__, where, mark))
                    else:
                        problems.append(
                            Problem(
                                cls.__name__,
                                where,
                                "Annotated validator is neither rule-generated nor pydantic_only",
                            )
                        )
        if self._module is not None:
            listed = set(self._models)
            for name, obj in vars(self._module).items():
                if (
                    inspect.isclass(obj)
                    and issubclass(obj, BaseModel)
                    and obj.__module__ == self._module.__name__
                    and obj not in listed
                    and "__ont__" in vars(obj)
                ):
                    problems.append(Problem(name, "-", "model class is not listed in MODELS"))
        return LintReport(tuple(engines), tuple(declared), tuple(problems))
