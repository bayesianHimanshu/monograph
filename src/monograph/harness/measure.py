"""Turn reports from the gates into result lines. Every metric the report shows is written here."""

from __future__ import annotations

from collections.abc import Sequence

from monograph.harness.results import ResultsWriter
from monograph.ingest.conformance import ConformanceReport
from monograph.mutate.runner import MutationResult


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
                "ingest_notes": c.notes.most_common(8),
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
