"""Turn reports from the gates into result lines. Every metric the report shows is written here."""

from __future__ import annotations

from monograph.harness.results import ResultsWriter
from monograph.ingest.conformance import ConformanceReport


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
