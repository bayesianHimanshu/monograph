"""Turn the gates' reports into result lines. Every metric in RESULTS.md is written here.

`Measurement` runs the measurements that `monograph measure` performs; the `record_*` functions are
also used by `monograph ingest map` and `monograph mutate`.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from rdflib import Graph

from monograph.core.instances import Instantiator
from monograph.core.regex import RegexSubsetError
from monograph.core.spec import SchemaSpec
from monograph.gen.artifacts import ArtifactBuilder
from monograph.gen.ddl import DdlResult
from monograph.harness.property_run import PropertyAgreementRun
from monograph.harness.results import ResultsWriter
from monograph.ingest.conformance import ConformanceReport
from monograph.mutate.runner import MutationResult
from monograph.rdf.mapping import RdfMapper
from monograph.validate.agreement import AgreementChecker, Outcome
from monograph.validate.fixtures import FixtureLoader
from monograph.validate.lint import LintReport
from monograph.validate.postgres_matrix import PgMatrix
from monograph.validate.pyshacl_probes import ProbeResult
from monograph.validate.shacl_side import ShaclValidator

# Idiomatic patterns a developer would naturally write for this model, next to the subset form we use.
# Measures how often the subset forces a rewrite, and why.
IDIOMATIC_PATTERNS: tuple[tuple[str, str], ...] = (
    ("UNII", r"^\w{10}$"),
    ("UNII", r"^[[:alnum:]]{10}$"),
    ("NDC 4-4-2", r"^\d{4}-\d{4}-\d{2}$"),
    ("SPL set ID", r"^(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
    ("SPL set ID", r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$"),
    ("recall number", r"^D-\d{3,4}-\d{4}$"),
    ("slug", r"^[a-z0-9]+(?:-[a-z0-9]+)*$"),
    ("LOINC", r"^\d{1,7}-\d$"),
    ("safety report ID", r"^\d+(-\d+)?$"),
    ("IRI", r"^https?://\S+$"),
    ("trimmed text", r"^\S(.*\S)?$"),
    ("RxCUI", r"^[0-9]{1,8}$"),
)


def record_conformance(writer: ResultsWriter, report: ConformanceReport, source: str) -> None:
    for name, c in report.classes.items():
        writer.write(
            f"conformance.{name}",
            round(c.rate, 4),
            {
                "source": source,
                "candidates": c.candidates,
                "valid": c.valid,
                "duplicates_skipped": c.duplicates,
                "top_reasons": c.reasons.most_common(8),
                "all_reasons": sorted(c.reasons.items(), key=lambda kv: (-kv[1], kv[0])),
                "ingest_notes": c.notes.most_common(8),
            },
        )
    suffixed = sum(n for s, n in report.report_id_suffixes.items() if s != "(none)")
    total_reports = sum(report.report_id_suffixes.values())
    writer.write(
        "ingest.report_id_suffix",
        round(suffixed / total_reports, 4) if total_reports else None,
        {
            "source": source,
            "reports": total_reports,
            "with_suffix": suffixed,
            "suffixes": report.report_id_suffixes.most_common(),
        },
    )
    agreed, total = report.agreement()
    writer.write(
        "agreement.real",
        round(agreed / total, 4) if total else None,
        {
            "source": source,
            "agreed": agreed,
            "cases": total,
            "raw_records": report.raw_records,
            "outcomes": dict(sum((c.outcomes for c in report.classes.values()), Counter())),
            "disagreements": report.disagreements[:20],
        },
    )


def record_mutations(writer: ResultsWriter, results: Sequence[MutationResult]) -> None:
    for r in results:
        writer.write(
            f"mutation.{r.id}",
            r.as_expected,
            {
                "title": r.title,
                "expected_gate": r.expected_gate or "none (must pass)",
                "failed_gates": r.failed_gates,
                "caught": r.caught,
                "seconds": r.seconds,
                "gate_seconds": r.gate_seconds,
                "apply_error": r.apply_error,
                "evidence": r.evidence,
            },
        )
    scored = [r for r in results if r.expected_gate is not None]
    writer.write(
        "mutation.detection_rate",
        round(sum(r.as_expected for r in scored) / len(scored), 4) if scored else None,
        {
            "mutations": len(scored),
            "caught_by_expected_gate": sum(r.as_expected for r in scored),
            "caught_by_any_gate": sum(r.caught for r in scored),
            "false_alarm_probes": [r.id for r in results if r.expected_gate is None],
            "false_alarms": [r.id for r in results if r.expected_gate is None and r.caught],
        },
    )


def record_postgres(writer: ResultsWriter, ddl: DdlResult, matrix: PgMatrix | None, skipped: str | None) -> None:
    unenforced = [{"class": u.cls, "field": u.field, "kind": u.kind, "reason": u.reason} for u in ddl.unenforced]
    if matrix is None:
        writer.write("postgres.matrix", None, {"skipped": skipped, "unenforced_by_ddl": unenforced})
        return
    rows: list[dict[str, Any]] = [
        {
            "fixture": r.fixture,
            "violates": r.violates,
            "ddl_claims_enforced": r.expected_by_ddl,
            "rejected": not r.verdict.accepted,
            "by": r.verdict.constraint or r.verdict.error,
        }
        for r in matrix.rows
    ]
    claimed = [r for r in rows if r["ddl_claims_enforced"]]
    writer.write(
        "postgres.matrix",
        round(sum(bool(r["rejected"]) for r in claimed) / len(claimed), 4) if claimed else None,
        {
            "valid_loaded": matrix.valid_loaded,
            "valid_failed": list(matrix.valid_failed),
            "rows": rows,
            "probes": [
                {"probe": name, "rejected": not v.accepted, "by": v.constraint or v.error} for name, v in matrix.probes
            ],
            "unenforced_by_ddl": unenforced,
        },
    )


class Measurement:
    def __init__(
        self,
        writer: ResultsWriter,
        spec: SchemaSpec,
        checker: AgreementChecker,
        instantiator: Instantiator,
        mapper: RdfMapper,
        fixtures: FixtureLoader,
        context: dict[str, Any],
    ) -> None:
        self._writer = writer
        self._spec = spec
        self._checker = checker
        self._instantiator = instantiator
        self._mapper = mapper
        self._fixtures = fixtures
        self._context = context

    def fixtures_agreement(self) -> None:
        groups: dict[str, Counter[str]] = {}
        mismatched, declared, dialect = [], [], []
        for group, items in (
            ("valid", self._fixtures.valid()),
            ("invalid", self._fixtures.invalid()),
            ("probe", self._fixtures.probes()),
        ):
            counter: Counter[str] = Counter()
            for f in items:
                result = self._checker.check(self._instantiator.instantiate(f.cls, f.record))
                counter[result.outcome.value] += 1
                if result.outcome.value != f.expect:
                    mismatched.append(f.name)
                if result.outcome is Outcome.DECLARED_LEAK:
                    declared.append({"fixture": f.name, "constraint": f.violates})
                if group == "probe":
                    dialect.append({"probe": f.name, "outcome": result.outcome.value, "note": f.note})
            groups[group] = counter
        schema_cases = sum(sum(groups[g].values()) for g in ("valid", "invalid"))
        schema_disagree = sum(groups[g][Outcome.DISAGREE] for g in ("valid", "invalid"))
        self._writer.write(
            "agreement.fixtures",
            round((schema_cases - schema_disagree) / schema_cases, 4),
            {
                "cases": schema_cases,
                "outcomes": {g: dict(c) for g, c in groups.items()},
                "unexpected": mismatched,
                "declared_leaks": declared,
                "dialect_probes": dialect,
            },
        )

    def property_agreement(self, run: PropertyAgreementRun, examples: int, per_site: int) -> None:
        stats = run.run()
        self._writer.write(
            "agreement.property",
            round(stats.agreed / stats.cases, 4) if stats.cases else None,
            {
                "cases": stats.cases,
                "valid_instances": dict(stats.valid_outcomes),
                "mutated_instances": dict(stats.mutation_outcomes),
                "by_perturbation": {k: dict(v) for k, v in sorted(stats.by_kind.items())},
                "disagreements": stats.disagreements[:20],
                "examples_per_class": examples,
                "examples_per_site": per_site,
            },
        )

    def round_trip(self, extra: Sequence[BaseModel] = ()) -> None:
        models: list[tuple[str, BaseModel]] = []
        for f in self._fixtures.valid():
            models.append(
                (
                    "fixture",
                    f.cls.model_validate(self._instantiator.instantiate(f.cls, f.record), context=self._context),
                )
            )
        models += [("real", m) for m in extra]
        ok: Counter[str] = Counter()
        total: Counter[str] = Counter()
        failures: list[dict[str, str]] = []
        for source, model in models:
            total[source] += 1
            cls = type(model)
            iri = self._mapper.iri_of(model)
            try:
                in_memory = self._mapper.from_graph(self._mapper.to_graph(model), cls, iri, self._context)
                text = self._mapper.to_graph(model).serialize(format="turtle")
                via_text = self._mapper.from_graph(Graph().parse(data=text, format="turtle"), cls, iri, self._context)
                error = None if in_memory == model and via_text == model else "read-back differs"
            except Exception as exc:  # failing to read back is a round-trip failure; keep the reason
                error = str(exc)[:200]
            if error is None:
                ok[source] += 1
            elif len(failures) < 20:
                failures.append({"iri": str(iri), "error": error})
        n = sum(total.values())
        self._writer.write(
            "roundtrip",
            round(sum(ok.values()) / n, 4) if n else None,
            {"cases": dict(total), "passed": dict(ok), "failures": failures, "modes": ["in-memory", "turtle text"]},
        )

    def dataset_integrity(self, shapes: Graph) -> None:
        graph = Graph()
        for f in self._fixtures.valid():
            graph += self._mapper.to_graph(self._instantiator.instantiate(f.cls, f.record))
        verdict = ShaclValidator(shapes, self._mapper).validate(graph)
        self._writer.write(
            "agreement.dataset_integrity",
            verdict.valid,
            {
                "note": "All valid fixtures merged, no reference stubs: sh:class now checks that referents exist.",
                "triples": len(graph),
                "violations": [v.describe() for v in verdict.violations][:10],
            },
        )

    def leaks(self, lint: LintReport, ddl: DdlResult, check_pattern: Callable[[str], None]) -> None:
        self._writer.write(
            "leaks.pydantic_only",
            len(lint.declared),
            {
                "declared": [
                    {"class": d.cls, "validator": d.validator, "id": d.declaration.id, "reason": d.declaration.reason}
                    for d in lint.declared
                ]
            },
        )
        self._writer.write(
            "leaks.postgres_static",
            len(ddl.unenforced),
            {
                "unenforced": [
                    {"class": u.cls, "field": u.field, "kind": u.kind, "reason": u.reason} for u in ddl.unenforced
                ]
            },
        )
        catalog = []
        for purpose, pattern in IDIOMATIC_PATTERNS:
            try:
                check_pattern(pattern)
                catalog.append({"purpose": purpose, "pattern": pattern, "accepted": True, "reason": None})
            except RegexSubsetError as exc:
                catalog.append({"purpose": purpose, "pattern": pattern, "accepted": False, "reason": exc.reason})
        self._writer.write(
            "leaks.regex_subset",
            sum(not c["accepted"] for c in catalog),
            {"idiomatic_patterns": catalog, "model_patterns_in_subset": True},
        )

    def validator_findings(self, probes: Sequence[ProbeResult]) -> None:
        self._writer.write(
            "findings.pyshacl",
            sum(p.observed for p in probes),
            {"probes": [{"id": p.id, "observed": p.observed, "description": p.description} for p in probes]},
        )

    def sizes(self, models_file: Path, files: dict[str, str]) -> None:
        source = models_file.read_text(encoding="utf-8")
        code_lines = [line for line in source.splitlines() if line.strip() and not line.strip().startswith("#")]
        fields = sum(len(c.fields) for c in self._spec.classes)
        generated: dict[str, dict[str, int]] = {}
        for name, text in sorted(files.items()):
            entry = {"bytes": len(text.encode("utf-8")), "lines": text.count("\n")}
            if name.endswith(".ttl"):
                entry["triples"] = len(Graph().parse(data=text, format="turtle"))
            generated[name] = entry
        self._writer.write(
            "size.source",
            len(code_lines),
            {
                "file": str(models_file),
                "lines_total": source.count("\n"),
                "lines_non_blank_non_comment": len(code_lines),
                "classes": len(self._spec.classes),
                "concrete_classes": len(self._spec.concrete()),
                "enums": len(self._spec.enums),
                "fields": fields,
                "rules": sum(len(c.rules) for c in self._spec.classes),
                "pydantic_only": sum(len(c.pydantic_only) for c in self._spec.classes),
                "constraint_sites": sum(1 for _ in self._spec.constraint_sites()),
            },
        )
        self._writer.write(
            "size.generated",
            sum(g["lines"] for g in generated.values()),
            {"artifacts": generated},
        )

    def generation_time(self, builder: ArtifactBuilder, analyze: Callable[[], SchemaSpec], repeats: int) -> None:
        import time

        analyze_runs: list[float] = []
        samples: dict[str, list[float]] = {}
        for _ in range(repeats):
            start = time.perf_counter()
            analyze()
            analyze_runs.append(time.perf_counter() - start)
            for name, seconds in builder.build().seconds.items():
                samples.setdefault(name, []).append(seconds)
        medians = {name: round(statistics.median(v), 4) for name, v in samples.items()}
        self._writer.write(
            "generation.seconds",
            round(statistics.median(analyze_runs) + sum(medians.values()), 4),
            {"analyze": round(statistics.median(analyze_runs), 4), "per_artifact": medians, "repeats": repeats},
        )
