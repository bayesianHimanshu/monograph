"""`monograph report h1` renders every required section from run records alone (here the dev round 2 records,
relabelled as test in memory), with no em-dashes outside verbatim model output."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from monograph.h1 import pins
from monograph.h1.report import render
from monograph.harness.results import read_results

from .conftest import ROOT

SECTIONS = (
    "## Verdicts",
    "Primary (Q2 to Q5)",
    "Inside coverage only",
    "## Accuracy, per type and model",
    "## Context and check effects",
    "## Inside and outside semantic coverage",
    "## The check itself",
    "False-block rate",
    "## Failure categories",
    "## Cost and latency",
    "USD per correct",
    "## Worked examples",
    "## What this means for the PoV",
)


def _as_test(records: list[dict[str, Any]], sha: str) -> list[dict[str, Any]]:
    out = []
    for r in records:
        if r.get("metric") == "h1.run" and r["details"].get("questions_sha256") == sha:
            relabelled = copy.deepcopy(r)
            relabelled["details"]["split"] = "test"
            out.append(relabelled)
    return out


def test_the_report_has_every_section() -> None:
    results = ROOT / "results" / "h1"
    if not results.exists():
        pytest.skip("no H1 results in this copy")
    sha = pins.questions_sha256(ROOT)
    records = _as_test(list(read_results(results)), sha)
    if not records:
        pytest.skip("no runs on the current question set")
    text = render(records, sha, ["Q1", "Q3", "Q4"])
    for section in SECTIONS:
        assert section in text, section
    for qtype in ("### Q2", "### Q3", "### Q4", "### Q5"):
        assert qtype in text
    assert "Sensitivity, without the types marked non-informative" in text
    assert chr(0x2014) not in text
