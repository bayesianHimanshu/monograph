"""Spend corrections: append-only fixes to costs that were logged at the wrong rate.

Results are append-only, so a run record whose cost was logged too low is never edited. Instead one
`<metric>.spend_correction` line is appended per mis-priced API call. It names the original record (file, line,
run, timestamp) and the call (request ID), gives the cost as logged, the corrected cost and the difference, and
carries `correction: true`. `monograph.agent.budget.spent` adds the difference to the phase's cumulative spend.

What is corrected: API calls billed on a long-context rate card (monograph.extract.pricing) inside run records
that were priced at the standard rate before long-context pricing existed. Only records that cumulative spend
counts at their own cost are corrected: run records not replayed from the cache and not covered by per-call
`<metric>.spend` lines (those are written at the right rate). The search is mechanical: every response in the
experiment's response cache, matched to records by request ID.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from monograph.extract.config import PRICES
from monograph.extract.llm import Usage
from monograph.extract.pricing import SOURCE

CORRECTION = "spend_correction"


@dataclass(frozen=True, slots=True)
class Located:
    file: str  # the results file name, e.g. 2026-11-28.jsonl
    line: int  # 1-based line number in that file (stable: results are append-only)
    record: dict[str, Any]


def read_located(results_dir: Path) -> Iterator[Located]:
    for path in sorted(results_dir.glob("*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for n, line in enumerate(handle, 1):
                if line.strip():
                    yield Located(path.name, n, json.loads(line))


def cached_responses(responses_dir: Path) -> dict[str, dict[str, Any]]:
    """request ID -> the cached response (model, usage), from `<dir>/<key[:2]>/<key>.json`."""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(responses_dir.glob("*/*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        response = entry.get("response", entry)
        if response.get("request_id"):
            out[response["request_id"]] = response
    return out


def standard_cost(usage: Usage, model: str) -> Decimal:
    """What the call was logged at before long-context pricing: the standard rate for every token."""
    p = PRICES[model]
    return (
        p.input * usage.input_tokens
        + p.output * usage.output_tokens
        + p.cache_read * usage.cache_read_tokens
        + p.cache_write * usage.cache_write_tokens
    ) / Decimal(1_000_000)


def corrections(
    located: list[Located], responses: dict[str, dict[str, Any]], metric: str
) -> list[tuple[float, dict[str, Any]]]:
    """The correction lines (value, details) still missing from the results; empty when all are present."""
    done = {
        (d["corrects"]["file"], d["corrects"]["line"], d["request_id"])
        for loc in located
        if loc.record.get("metric") == f"{metric}.{CORRECTION}"
        for d in [loc.record["details"]]
    }
    out: list[tuple[float, dict[str, Any]]] = []
    for loc in located:
        r = loc.record
        d = r.get("details", {})
        if r.get("metric") != f"{metric}.run" or d.get("from_cache") or d.get("ledgered"):
            continue
        for request_id in d.get("request_ids") or []:
            response = responses.get(request_id)
            if response is None or (loc.file, loc.line, request_id) in done:
                continue
            model = response["model"]
            usage = Usage(**response["usage"])
            billed = usage.rate(model)
            if billed.tier == "standard":
                continue
            logged = standard_cost(usage, model)
            corrected = usage.cost_usd(model)
            difference = corrected - logged
            record_cost = Decimal(str(d["cost_usd"]))
            out.append(
                (
                    float(difference),
                    {
                        "correction": True,
                        "corrects": {
                            "file": loc.file,
                            "line": loc.line,
                            "run_id": r["run_id"],
                            "timestamp": r["timestamp"],
                            "metric": r["metric"],
                            "question_id": d.get("question_id"),
                            "cell": d.get("cell"),
                            "arm": d.get("arm"),
                        },
                        "split": d.get("split"),
                        "model": model,
                        "request_id": request_id,
                        "usage": dict(response["usage"]),
                        "prompt_tokens": usage.prompt_tokens,
                        "rate": billed.tier,
                        "logged_cost_usd": str(logged),
                        "corrected_cost_usd": str(corrected),
                        "difference_usd": str(difference),
                        "record_cost_usd": str(record_cost),
                        "record_corrected_cost_usd": str(record_cost + difference),
                        "reason": (
                            f"prompt of {usage.prompt_tokens} tokens is billed on the {billed.tier} rate card;"
                            " the record priced it at the standard rate"
                        ),
                        "pricing_source": SOURCE,
                    },
                )
            )
    return out
