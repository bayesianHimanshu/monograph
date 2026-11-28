"""Runs one model x arm over a split and writes every round and every record to results/h4."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from monograph.extract.arms import Extractor, RecordResult, plain
from monograph.extract.config import Settings
from monograph.extract.prompts import PromptSet
from monograph.extract.sources.dailymed import SplDocument
from monograph.harness.results import ResultsWriter


def record_lines(
    result: RecordResult, split: str, prompts: PromptSet, settings: Settings
) -> list[tuple[str, Any, dict[str, Any]]]:
    common = {
        "model": result.model,
        "arm": result.arm,
        "split": split,
        "set_id": result.set_id,
        "label_version": result.version,
        "prompt_version": prompts.version,
        "system_prompt_sha256": prompts.system_hash,
        "schema_sha256": prompts.schema_hash,
        "settings": settings.as_dict(),
    }
    lines: list[tuple[str, Any, dict[str, Any]]] = []
    for r in result.rounds:
        lines.append(
            (
                "h4.round",
                r.index,
                common
                | {
                    "phase": r.phase,
                    "feedback": r.feedback,
                    "request_sha256": r.request_key,
                    "stop_reason": r.stop_reason,
                    "usage": asdict(r.usage),
                    "cost_usd": str(r.usage.cost_usd(result.model)),
                    "latency_s": r.latency_s,
                    "from_cache": r.from_cache,
                    "raw_payload": plain(r.raw_payload),
                    "payload": plain(r.payload),
                    "parse_error": r.parse_error,
                    "violations": r.violations,
                    "fixes": r.fixes,
                    "issues": r.issues,
                },
            )
        )
    usage = result.usage()
    lines.append(
        (
            "h4.record",
            result.status,
            common
            | {
                "repair_rounds": result.repair_rounds,
                "final_payload": plain(result.final_payload),
                "pydantic_valid": result.pydantic_valid,
                "context_issues": result.context_issues,
                "usage": asdict(usage),
                "cost_usd": str(_cost(result)),  # each round priced on its own
                "latency_s": round(sum(r.latency_s for r in result.rounds), 3),
                "network_rounds": sum(not r.from_cache for r in result.rounds),
            },
        )
    )
    return lines


def _cost(result: RecordResult) -> Decimal:
    """A label's cost: the sum of its rounds' costs, each round one request (a long-context rate applies per
    request, not to the label's summed usage)."""
    return sum((r.usage.cost_usd(result.model) for r in result.rounds), Decimal(0))


class SplitRunner:
    def __init__(self, extractor: Extractor, writer: ResultsWriter, prompts: PromptSet, settings: Settings) -> None:
        self._extractor = extractor
        self._writer = writer
        self._prompts = prompts
        self._settings = settings

    def run(self, docs: Sequence[SplDocument], arm: str, split: str, log: Callable[[str], None]) -> Decimal:
        total = Decimal(0)
        for n, doc in enumerate(docs, 1):
            result = self._extractor.run(doc, arm)
            for metric, value, details in record_lines(result, split, self._prompts, self._settings):
                self._writer.write(metric, value, details)
            total += _cost(result)
            if n % 10 == 0 or n == len(docs):
                log(f"{arm} {result.model} {split}: {n}/{len(docs)} labels, cost so far ${total:.4f}")
        return total
