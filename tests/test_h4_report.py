"""`monograph report h4` renders from result lines alone."""

from __future__ import annotations

from typing import Any

from monograph.harness.report_h4 import H4Report


def _agg(model: str, arm: str, group: str = "headline", **extra: Any) -> dict[str, Any]:
    details = {
        "model": model,
        "arm": arm,
        "split": "test",
        "group": group,
        "usable": arm == "A3",
        "checks": {"mean_f1": arm == "A3"},
        "labels": 150,
        "mean_f1": 0.9 + 0.01 * int(arm[1]),
        "pooled_f1": 0.9,
        "dosage_form_accuracy": 0.98,
        "route_accuracy": 0.99,
        "unresolvable_rate": 0.0,
        "hallucination_rate": 0.0,
        "exact_match_rate": 0.8,
        "violation_free_rate": 0.9,
        "refusal_rate": 0.0,
        "truncation_rate": 0.0,
        "mean_repair_rounds": 0.5,
        "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 1, "cache_write_tokens": 1},
        "cost_usd": "1.0",
        "cost_per_record_usd": "0.01",
        "cost_per_usable_record_usd": "0.02",
        "latency_p50_s": 5.0,
        "latency_p95_s": 9.0,
        **extra,
    }
    return {
        "metric": "h4.aggregate",
        "value": details["mean_f1"],
        "details": details,
        "run_id": "r1",
        "timestamp": "2026-11-27T00:00:00",
    }


def test_report_renders_verdict_and_pov() -> None:
    models = ("claude-sonnet-5-5", "claude-haiku-5-5")
    records = [_agg(m, a) for m in models for a in ("A0", "A1", "A2", "A3", "A3+")]
    records.append(
        {
            "metric": "h4.verdict",
            "value": "holds",
            "run_id": "r1",
            "timestamp": "2026-11-27T00:00:01",
            "details": {"split": "test", "usable": {"claude-haiku-5-5 A3": True}},
        }
    )
    text = H4Report(records).render()
    assert "**Verdict: H4 holds.**" in text
    assert "A3+ (upper bound)" in text
    assert text.rstrip().splitlines()[-1].startswith("* Does graph-context repair")
    assert "## What this means for the PoV" in text
    assert "—" not in text


def test_report_without_scoring_run() -> None:
    assert "No scoring run recorded yet" in H4Report([]).render()
