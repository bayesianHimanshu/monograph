"""The mutation suite records its results in results/mutations/, never in an experiment's results, and the H6
report still finds the latest suite run there."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from monograph import wiring
from monograph.cli import app
from monograph.mutate import runner as runner_module
from monograph.mutate.runner import MutationResult


def _fake_run(self: object, mutation: object) -> MutationResult:
    return MutationResult(
        id=mutation.id,  # type: ignore[attr-defined]
        title=mutation.title,  # type: ignore[attr-defined]
        expected_gate=mutation.expected_gate,  # type: ignore[attr-defined]
        failed_gates=[] if mutation.expected_gate is None else [mutation.expected_gate.split("|")[0]],  # type: ignore[attr-defined]
    )


def test_mutate_writes_to_results_mutations_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_module.MutationRunner, "run", _fake_run)
    out = CliRunner().invoke(app, ["mutate", "--root", str(tmp_path), "--only", "M0", "--only", "M16"])
    assert out.exit_code == 0, out.output
    written = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.jsonl"))
    assert written
    assert all(p.startswith("results/mutations/") for p in written), written
    assert not (tmp_path / "results" / "h6").exists()
    lines = [line for p in (tmp_path / "results" / "mutations").glob("*.jsonl") for line in p.read_text().splitlines()]
    assert any('"mutation.detection_rate"' in line for line in lines)
    assert all('"experiment": "MUTATIONS"' in line for line in lines)


def test_the_mutation_writer_belongs_to_no_experiment(tmp_path: Path) -> None:
    assert wiring.mutations_dir(tmp_path) == tmp_path / "results" / "mutations"
    assert wiring.mutations_dir(tmp_path) != wiring.results_dir(tmp_path)


def test_the_h6_report_reads_the_latest_suite_run_from_results_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monograph.harness.results import read_results

    monkeypatch.setattr(runner_module.MutationRunner, "run", _fake_run)
    CliRunner().invoke(app, ["mutate", "--root", str(tmp_path), "--only", "M1"])
    records = [*read_results(wiring.results_dir(tmp_path)), *read_results(wiring.mutations_dir(tmp_path))]
    from monograph.harness.report import ReportBuilder

    text = ReportBuilder(records).render()
    assert "M1" in text
