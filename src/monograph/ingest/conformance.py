"""Map raw openFDA records to candidates, then judge each candidate with both validators."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from monograph.core.instances import Instantiator
from monograph.core.spec import SchemaSpec
from monograph.ingest.openfda import Candidate, OpenFdaMapper
from monograph.rdf.mapping import MISSING_KEY, RdfMapper
from monograph.validate.agreement import AgreementChecker, Outcome


@dataclass
class ClassConformance:
    candidates: int = 0
    valid: int = 0
    duplicates: int = 0
    reasons: Counter[str] = field(default_factory=Counter)
    notes: Counter[str] = field(default_factory=Counter)
    outcomes: Counter[str] = field(default_factory=Counter)

    @property
    def rate(self) -> float:
        return self.valid / self.candidates if self.candidates else 0.0


@dataclass
class ConformanceReport:
    raw_records: dict[str, int]
    classes: dict[str, ClassConformance]
    disagreements: list[dict[str, Any]]
    report_id_suffixes: Counter[str] = field(default_factory=Counter)
    quarantine: list[dict[str, Any]] = field(default_factory=list)

    def agreement(self) -> tuple[int, int]:
        total = sum(sum(c.outcomes.values()) for c in self.classes.values())
        bad = sum(c.outcomes[Outcome.DISAGREE] for c in self.classes.values())
        return total - bad, total


def _reason(path: str, kind: str) -> str:
    return f"{re.sub(r'\.[0-9]+', '[]', path)}: {kind}"


def _note(text: str) -> str:
    return re.sub(r"'[^']*'|\"[^\"]*\"|\b[0-9]+\b", "#", text)


class ConformanceRun:
    def __init__(
        self,
        spec: SchemaSpec,
        mapper: OpenFdaMapper,
        instantiator: Instantiator,
        rdf: RdfMapper,
        checker: AgreementChecker,
    ) -> None:
        self._spec = spec
        self._mapper = mapper
        self._instantiator = instantiator
        self._rdf = rdf
        self._checker = checker

    def candidates(self, raw: Mapping[str, Sequence[dict[str, Any]]]) -> list[Candidate]:
        return [c for endpoint, records in raw.items() for r in records for c in self._mapper.map(endpoint, r)]

    def run(self, raw: Mapping[str, Sequence[dict[str, Any]]]) -> ConformanceReport:
        classes: dict[str, ClassConformance] = {c.cls.__name__: ClassConformance() for c in self._spec.entities()}
        seen: set[tuple[str, str]] = set()
        disagreements: list[dict[str, Any]] = []
        suffixes: Counter[str] = Counter()
        quarantine: list[dict[str, Any]] = []
        pairs = [
            (endpoint, r, c)
            for endpoint, records in raw.items()
            for r in records
            for c in self._mapper.map(endpoint, r)
        ]
        for endpoint, raw_record, candidate in pairs:
            cls = self._spec.by_name(candidate.cls).cls
            stats = classes[candidate.cls]
            instance = self._instantiator.instantiate(cls, candidate.record)
            key = (candidate.cls, str(self._rdf.iri_of(instance)))
            if MISSING_KEY not in key[1] and key in seen:
                stats.duplicates += 1
                continue
            seen.add(key)
            stats.candidates += 1
            if candidate.cls == "AdverseEventReport":
                report_id = str(candidate.record.get("report_id", ""))
                match = re.fullmatch(r"[0-9]+(-[0-9]+)", report_id)
                suffixes[match.group(1) if match else "(none)"] += 1
            for note in candidate.notes:
                stats.notes[_note(note)] += 1
            result = self._checker.check(instance)
            stats.outcomes[result.outcome.value] += 1
            if result.pydantic.valid:
                stats.valid += 1
            for reason in sorted({_reason(v.path, v.kind) for v in result.pydantic.violations}):
                stats.reasons[reason] += 1
            unsafe = [v for v in result.pydantic.violations if v.kind == "control_character"]
            if unsafe:
                # Quarantined, never sanitized: the raw record is kept exactly as fetched, with the reason.
                quarantine.append(
                    {
                        "endpoint": endpoint,
                        "source": candidate.source,
                        "class": candidate.cls,
                        "reasons": [f"{v.path}: {v.message}" for v in unsafe],
                        "raw": raw_record,
                    }
                )
            if not result.agrees:
                disagreements.append(
                    {
                        "class": candidate.cls,
                        "source": candidate.source,
                        "pydantic": [v.describe() for v in result.pydantic.violations][:3],
                        "shacl": [v.describe() for v in result.shacl.violations][:3],
                    }
                )
        return ConformanceReport({k: len(v) for k, v in raw.items()}, classes, disagreements, suffixes, quarantine)
