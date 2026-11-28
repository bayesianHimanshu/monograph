"""`monograph report mutations`: docs/MUTATIONS.md from results/mutations/ only, the latest run per mutation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from monograph.cli import app
from monograph.harness.report_mutations import latest_per_mutation, outcome, render


def _line(n: int, ts: str, sha: str, expected: str, failed: list[str], title: str = "t") -> dict[str, Any]:
    return {
        "metric": f"mutation.M{n}",
        "timestamp": ts,
        "git_sha": sha,
        "value": bool(failed),
        "details": {"title": title, "expected_gate": expected, "failed_gates": failed, "apply_error": None},
    }


def test_the_latest_run_of_each_mutation_wins_in_mutation_order() -> None:
    records = [
        _line(10, "2026-11-28T09:00:00+00:00", "b" * 40, "drift", ["drift"]),
        _line(2, "2026-11-28T09:00:00+00:00", "b" * 40, "tests", ["tests"]),
        _line(2, "2026-11-27T09:00:00+00:00", "a" * 40, "tests", []),
        {"metric": "mutation.detection_rate", "timestamp": "2026-11-28T10:00:00+00:00", "details": {}},
    ]
    rows = latest_per_mutation(records)
    assert [r["metric"] for r in rows] == ["mutation.M2", "mutation.M10"]
    assert rows[0]["git_sha"] == "b" * 40


def test_outcomes() -> None:
    assert outcome({"expected_gate": "drift|tests", "failed_gates": ["tests"]}) == "caught"
    assert outcome({"expected_gate": "drift", "failed_gates": ["tests"]}) == "caught by another gate"
    assert outcome({"expected_gate": "drift", "failed_gates": []}) == "missed"
    assert outcome({"expected_gate": "none (must pass)", "failed_gates": []}) == "passed every gate"
    assert outcome({"expected_gate": "none (must pass)", "failed_gates": ["lint"]}) == "false alarm"
    assert outcome({"expected_gate": "drift", "failed_gates": [], "apply_error": "boom"}) == "could not apply"


def test_the_page_shows_gate_date_and_commit() -> None:
    text = render(
        [
            _line(0, "2026-11-27T09:00:00+00:00", "c" * 40 + "-dirty", "none (must pass)", [], "control"),
            _line(3, "2026-11-28T09:56:40+00:00", "5964dc0e4dee", "drift", ["drift", "tests"], "drop a class"),
        ]
    )
    assert "| M3 | drop a class | drift | drift, tests | caught | 2026-11-28 | 5964dc0 |" in text
    assert "| M0 | control | none (must pass) | none | passed every gate | 2026-11-27 | ccccccc-dirty |" in text
    assert "every one ended as expected" in text
    assert chr(0x2014) not in text  # no em-dashes


def test_the_command_reads_results_mutations_only(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    for exp, line in (
        ("mutations", _line(1, "2026-11-28T09:00:00+00:00", "d" * 40, "tests", ["tests"])),
        ("h6", _line(1, "2026-11-29T09:00:00+00:00", "e" * 40, "tests", [])),  # an older layout: not read
    ):
        (tmp_path / "results" / exp).mkdir(parents=True)
        (tmp_path / "results" / exp / "x.jsonl").write_text(json.dumps(line) + "\n")
    result = CliRunner().invoke(app, ["report", "mutations", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    text = (tmp_path / "docs" / "MUTATIONS.md").read_text()
    assert "| M1 | t | tests | tests | caught | 2026-11-28 | ddddddd |" in text
    assert "eeeeeee" not in text
