from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from monograph.harness.report import ReportBuilder
from monograph.harness.results import ResultsWriter, read_results

EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)


def _write(tmp_path: Path, run: str, metric: str, value: object, details: dict[str, object], minute: int) -> None:
    when = datetime(2026, 11, 26, 12, minute, tzinfo=UTC)
    ResultsWriter(tmp_path, run, "sha", "0.1.0", lambda: when).write(metric, value, details)


def test_report_uses_latest_values_and_one_consistent_mutation_run(tmp_path: Path) -> None:
    _write(tmp_path, "a", "agreement.fixtures", 0.5, {}, 0)
    _write(tmp_path, "b", "agreement.fixtures", 1.0, {"cases": 3}, 1)
    _write(
        tmp_path,
        "c",
        "mutation.M1",
        True,
        {"title": "t", "expected_gate": "drift", "failed_gates": ["drift"], "seconds": 1.0},
        2,
    )
    _write(
        tmp_path,
        "c",
        "mutation.detection_rate",
        1.0,
        {
            "mutations": 1,
            "caught_by_expected_gate": 1,
            "caught_by_any_gate": 1,
            "false_alarm_probes": ["M0"],
            "false_alarms": [],
        },
        3,
    )
    _write(
        tmp_path,
        "d",
        "mutation.M1",
        False,
        {"title": "stale", "expected_gate": "drift", "failed_gates": [], "seconds": 1.0},
        4,
    )
    text = ReportBuilder(list(read_results(tmp_path))).render()
    assert "| Pydantic and SHACL agreement, curated fixtures | 100.0% |" in text
    assert "stale" not in text  # mutation rows come from the run that wrote the detection rate
    assert "What this means for the PoV" in text
    assert EM_DASH not in text
    assert EN_DASH not in text


def test_report_is_deterministic(tmp_path: Path) -> None:
    _write(tmp_path, "a", "roundtrip", 1.0, {"cases": {"fixture": 1}}, 0)
    records = list(read_results(tmp_path))
    assert ReportBuilder(records).render() == ReportBuilder(list(reversed(records))).render()


def test_no_em_dashes_anywhere_in_the_repository() -> None:
    from .conftest import ROOT

    offenders = []
    for path in ROOT.rglob("*"):
        if (
            any(
                part in {".git", ".venv", "data", ".mypy_cache", ".ruff_cache", ".pytest_cache", ".hypothesis"}
                for part in path.parts
            )
            or not path.is_file()
        ):
            continue
        if path.suffix in {".py", ".md", ".toml", ".yml", ".ttl", ".sql", ".json", ".jsonl", ""}:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if EM_DASH in text and "fixtures/openfda" not in str(path):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
