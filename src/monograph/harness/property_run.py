"""Runs the property-based agreement cases outside pytest and counts outcomes, for the results file."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from monograph.core.instances import Instantiator
from monograph.core.spec import SchemaSpec
from monograph.validate.agreement import AgreementChecker, Outcome
from monograph.validate.strategies import Perturber, RecordStrategies, Site


@dataclass
class PropertyStats:
    valid_outcomes: Counter[str] = field(default_factory=Counter)
    mutation_outcomes: Counter[str] = field(default_factory=Counter)
    by_kind: dict[str, Counter[str]] = field(default_factory=dict)
    disagreements: list[dict[str, Any]] = field(default_factory=list)

    @property
    def cases(self) -> int:
        return sum(self.valid_outcomes.values()) + sum(self.mutation_outcomes.values())

    @property
    def agreed(self) -> int:
        return self.cases - self.valid_outcomes[Outcome.DISAGREE] - self.mutation_outcomes[Outcome.DISAGREE]


class PropertyAgreementRun:
    def __init__(
        self,
        spec: SchemaSpec,
        checker: AgreementChecker,
        instantiator: Instantiator,
        strategies: RecordStrategies,
        perturber: Perturber,
        examples: int,
        examples_per_site: int,
    ) -> None:
        self._spec = spec
        self._checker = checker
        self._instantiator = instantiator
        self._strategies = strategies
        self._perturber = perturber
        self._settings = settings(
            max_examples=examples,
            derandomize=True,
            database=None,
            deadline=None,
            suppress_health_check=list(HealthCheck),
        )

        self._site_settings = settings(self._settings, max_examples=examples_per_site)

    def run(self) -> PropertyStats:
        stats = PropertyStats()
        for c in self._spec.entities():
            self._valid(c.cls, stats)
            self._mutations(c.cls, stats)
        return stats

    def _valid(self, cls: type[BaseModel], stats: PropertyStats) -> None:
        @self._settings
        @given(self._strategies.valid_record(cls))
        def case(record: dict[str, Any]) -> None:
            result = self._checker.check(self._instantiator.instantiate(cls, record))
            stats.valid_outcomes[result.outcome.value] += 1
            if not result.agrees:
                stats.disagreements.append({"class": cls.__name__, "site": "valid", "record": repr(record)[:300]})

        case()

    def _mutations(self, cls: type[BaseModel], stats: PropertyStats) -> None:
        for site in self._perturber.sites(cls):
            self._site(cls, site, stats)

    def _site(self, cls: type[BaseModel], site: Site, stats: PropertyStats) -> None:
        @self._site_settings
        @given(self._strategies.valid_record(cls), st.data())
        def case(record: dict[str, Any], data: st.DataObject) -> None:
            mutated = self._perturber.apply(data.draw, record, site)
            if mutated is None:
                return
            result = self._checker.check(self._instantiator.instantiate(cls, mutated))
            stats.mutation_outcomes[result.outcome.value] += 1
            stats.by_kind.setdefault(site.kind.split(":")[0], Counter())[result.outcome.value] += 1
            if not result.agrees:
                stats.disagreements.append(
                    {
                        "class": cls.__name__,
                        "site": site.label(),
                        "pydantic": [v.describe() for v in result.pydantic.violations][:3],
                        "shacl": [v.describe() for v in result.shacl.violations][:3],
                        "record": repr(mutated)[:300],
                    }
                )

        case()
