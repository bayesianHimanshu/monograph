"""Spend corrections: a call logged at the standard rate but billed on the long-context card gets one appended
correction line; the original line is never edited; cumulative spend includes the difference."""

from __future__ import annotations

import json
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from monograph.agent.budget import spent
from monograph.agent.spend_correction import cached_responses, corrections, read_located
from monograph.cli import app
from monograph.harness.results import read_results

from .conftest import ROOT

HAIKU = "claude-haiku-5-5"
M = Decimal(1_000_000)
BIG = {"input_tokens": 2, "cache_read_tokens": 87_199, "cache_write_tokens": 16_310, "output_tokens": 5_259}
SMALL = {"input_tokens": 10, "cache_read_tokens": 1_000, "cache_write_tokens": 500, "output_tokens": 100}


def _std(u: dict[str, int]) -> Decimal:
    return (
        Decimal("0.10") * u["input_tokens"]
        + Decimal("0.01") * u["cache_read_tokens"]
        + Decimal("0.125") * u["cache_write_tokens"]
        + Decimal("0.50") * u["output_tokens"]
    ) / M


def _run(qid: str, request_ids: list[str], cost: Decimal, **extra: Any) -> dict[str, Any]:
    details: dict[str, Any] = {"split": "test", "question_id": qid, "cell": "c", "arm": "S-tools", "model": HAIKU}
    details |= {"request_ids": request_ids, "cost_usd": str(cost), "from_cache": False, **extra}
    return {"metric": "h2_1.run", "run_id": "r1", "timestamp": "2026-11-28T07:45:19+00:00", "details": details}


def _setup(root: Path) -> Decimal:
    results = root / "results" / "h2_1"
    results.mkdir(parents=True)
    logged = _std(BIG) + _std(SMALL)
    lines = [
        _run("q-small", ["req_small"], _std(SMALL)),
        _run("q-big", ["req_big", "req_small2"], logged),
        _run("q-replay", ["req_big"], logged, from_cache=True),  # a free replay: not spent, not corrected
    ]
    (results / "2026-11-28.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines))
    for i, (rid, usage) in enumerate((("req_big", BIG), ("req_small", SMALL), ("req_small2", SMALL))):
        d = root / "data" / "h2_1" / "responses" / f"{i:02d}"
        d.mkdir(parents=True)
        body = {"model": HAIKU, "request_id": rid, "usage": usage, "content": [], "stop_reason": "end_turn"}
        (d / f"{i:02d}key.json").write_text(json.dumps(body))
    return logged


def _git(repo: Path, *args: str) -> None:
    cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args]
    subprocess.run(cmd, cwd=repo, check=True, capture_output=True)


def test_one_correction_for_the_long_context_call(tmp_path: Path) -> None:
    _setup(tmp_path)
    located = list(read_located(tmp_path / "results" / "h2_1"))
    lines = corrections(located, cached_responses(tmp_path / "data" / "h2_1" / "responses"), "h2_1")
    assert len(lines) == 1
    value, d = lines[0]
    long = (Decimal("0.50") * 2 + Decimal("0.05") * 87_199 + Decimal("0.625") * 16_310 + Decimal("2.50") * 5_259) / M
    assert d["correction"] is True
    assert d["corrects"]["file"] == "2026-11-28.jsonl"
    assert d["corrects"]["line"] == 2
    assert d["corrects"]["question_id"] == "q-big"
    assert d["request_id"] == "req_big"
    assert d["prompt_tokens"] == 103_511
    assert d["rate"] == "long_context"
    assert Decimal(d["logged_cost_usd"]) == _std(BIG)
    assert Decimal(d["corrected_cost_usd"]) == long
    assert Decimal(d["difference_usd"]) == long - _std(BIG)
    assert value == float(long - _std(BIG))


def test_the_command_appends_never_edits_and_is_idempotent(tmp_path: Path) -> None:
    logged = _setup(tmp_path)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    original = (tmp_path / "results" / "h2_1" / "2026-11-28.jsonl").read_bytes()
    before = spent(list(read_results(tmp_path / "results" / "h2_1")), "h2_1", "test")
    assert before == _std(SMALL) + logged

    result = CliRunner().invoke(app, ["correct-spend", "h2_1", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    after_files = sorted((tmp_path / "results" / "h2_1").glob("*.jsonl"))
    records = list(read_results(tmp_path / "results" / "h2_1"))
    added = [r for r in records if r["metric"] == "h2_1.spend_correction"]
    assert len(added) == 1
    # the original lines are untouched: the file starts with exactly the bytes it had
    for path in after_files:
        if path.name == "2026-11-28.jsonl":
            assert path.read_bytes()[: len(original)] == original
    difference = Decimal(added[0]["details"]["difference_usd"])
    assert difference > 0
    assert spent(records, "h2_1", "test") == before + difference

    again = CliRunner().invoke(app, ["correct-spend", "h2_1", "--root", str(tmp_path)])
    assert again.exit_code == 0
    assert "no corrections needed" in again.output
    assert len([r for r in read_results(tmp_path / "results" / "h2_1") if r["metric"].endswith("correction")]) == 1


def test_spent_counts_correction_lines_only_for_their_phase() -> None:
    def line(split: str, diff: str, correction: bool = True) -> dict[str, Any]:
        details = {"split": split, "difference_usd": diff, "correction": correction}
        return {"metric": "x.spend_correction", "details": details}

    records = [line("test", "0.02"), line("dev", "5"), line("test", "7", correction=False)]
    assert spent(records, "x", "test") == Decimal("0.02")


def test_the_h2_1_correction_is_recorded() -> None:
    results = ROOT / "results" / "h2_1"
    records = list(read_located(results)) if results.exists() else []
    fixes = [loc.record for loc in records if loc.record["metric"] == "h2_1.spend_correction"]
    if not fixes:
        return  # a copy without H2.1 results
    assert len(fixes) == 1
    d = fixes[0]["details"]
    assert d["correction"] is True
    assert d["prompt_tokens"] == 103_511
    assert d["request_id"] == "req_011ChUxFr8CDkrP9QUHbw5N6"
    original = next(
        loc.record for loc in records if (loc.file, loc.line) == (d["corrects"]["file"], d["corrects"]["line"])
    )
    assert original["metric"] == "h2_1.run"
    assert d["request_id"] in original["details"]["request_ids"]
    assert original["details"]["cost_usd"] == d["record_cost_usd"]  # the original line, as it was logged
