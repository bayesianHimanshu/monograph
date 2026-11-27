"""The H2 report: run selection by prompt hash, rendering from records alone, AGE error grouping."""

from __future__ import annotations

from typing import Any

from monograph.core.meta import DEFAULT_BASE
from monograph.qa.pov import pov_section
from monograph.qa.report import ARMS, age_errors, render, score_lines, select_runs

B = DEFAULT_BASE
MODELS = ("m1", "m2")


def _record(model: str, arm: str, qid: str, qtype: str, correct: bool, **kw: Any) -> dict[str, Any]:
    return {
        "model": model,
        "arm": arm,
        "question_id": qid,
        "type": qtype,
        "split": "test",
        "correct": correct,
        "f1": 1.0 if correct else 0.0,
        "failure": None if correct else "wrong set",
        "outcome": "answered",
        "answer": {"kind": "ids", "ids": [f"{B}organization/a"]},
        "calls": kw.get("calls", []),
        "latency_s": 2.0,
        "cost_usd": "0.01",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "question": f"Question {qid}?",
        "gold": {"kind": "ids", "ids": [f"{B}organization/a"]},
        "system_prompt_sha256": "s",
        "tools_sha256": "t",
    }


def _records() -> list[dict[str, Any]]:
    out = []
    for m in MODELS:
        for a in ARMS:
            for t in ("T1", "T2", "T3", "T4", "T5", "T6"):
                for i in range(4):
                    good = a.startswith("G") or i < 2
                    out.append(_record(m, a, f"{t}-{i}", t, good))
    return out


def test_select_runs_keeps_the_latest_line_with_the_current_prompt() -> None:
    lines = [
        {"metric": "h2.run", "details": {**_record("m1", "S-free", "q", "T1", False), "system_prompt_sha256": "old"}},
        {"metric": "h2.run", "details": _record("m1", "S-free", "q", "T1", False)},
        {"metric": "h2.run", "details": _record("m1", "S-free", "q", "T1", True)},
        {"metric": "h2.error", "details": {}},
    ]
    picked = select_runs(lines, "test", {"S-free": ("s", "t")})
    assert len(picked) == 1
    assert picked[0]["correct"] is True
    assert select_runs(lines, "dev", {"S-free": ("s", "t")}) == []


def test_render_has_every_required_section() -> None:
    records = _records()
    scores = score_lines(records, MODELS)
    assert scores["verdict"] is not None
    questions = {r["question_id"]: {"type": r["type"], "text": r["question"], "gold": r["gold"]} for r in records}
    text = render(scores, records, questions, MODELS, {"test_spend": 1.0, "pov": pov_section(scores, records, MODELS)})
    for heading in (
        "## Verdict against the PREREG",
        "## Accuracy, 2 x 2 per question type",
        "## Store and interface effects",
        "## Failure categories",
        "## Cost and latency",
        "## AGE Cypher limitations hit",
        "## Worked examples (core types)",
        "## What this means for the PoV",
    ):
        assert heading in text
    assert text.rstrip().splitlines()[0].startswith("# H2 results")
    assert "graph arm won" in text
    assert chr(0x2014) not in text


def test_age_errors_are_grouped_by_limitation() -> None:
    calls = [
        {"name": "run_cypher", "is_error": True, "preview": 'error: query error: syntax error at or near "|"'},
        {"name": "run_cypher", "is_error": True, "preview": "error: query error: shortestPath is not supported"},
        {"name": "run_cypher", "is_error": False, "preview": "{}"},
    ]
    got = age_errors([_record("m1", "G-free", "q", "T2", False, calls=calls)])
    assert got["relationship type alternation [:A|B] not supported"] == 1
    assert got["shortestPath() not supported"] == 1
