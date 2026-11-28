"""Run one arm on one split: every question through the agent loop, scored, one append-only result line each.

Questions run concurrently on a small worker pool; each worker has its own read-only connection and arm,
so nothing is shared but the (thread-safe, content-addressed) response cache and the results writer.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pydantic import BaseModel

from monograph.agent.arms import PROMPT_VERSIONS, Arm, FinalAnswer, sha256
from monograph.agent.budget import BudgetExceeded
from monograph.agent.llm import AGENT_SETTINGS, AgentSettings, ToolClient
from monograph.agent.loop import AgentLoop
from monograph.qa.score import Gold, failure_category, score_answer


class Writer:
    def __init__(self, write: Callable[[str, Any, dict[str, Any]], None]) -> None:
        self._write = write
        self._lock = threading.Lock()

    def __call__(self, metric: str, value: Any, details: dict[str, Any]) -> None:
        with self._lock:
            self._write(metric, value, details)


def score_record(record: dict[str, Any], question: Mapping[str, Any], known_ids: set[str] | None) -> dict[str, Any]:
    gold = Gold.parse(question["gold"])
    correct, f1 = score_answer(record.get("answer"), gold)
    record["correct"], record["f1"] = correct, f1
    record["failure"] = failure_category(record, gold, question.get("anchors", []), known_ids)
    return record


def run_split(
    questions: Sequence[Mapping[str, Any]],
    arm_name: str,
    model: str,
    split: str,
    make_arm: Callable[[], Arm],
    system: str,
    client: ToolClient,
    write: Writer,
    known_ids: set[str] | None,
    workers: int = 4,
    settings: AgentSettings = AGENT_SETTINGS,
    log: Callable[[str], None] = print,
    metric: str = "h2",
    tag: str | None = None,
    extra: Mapping[str, Any] | None = None,
    prompt_versions: Mapping[str, str] | None = None,
    classify: Callable[[dict[str, Any]], str | None] | None = None,
    answer_model: type[BaseModel] = FinalAnswer,
    score: Callable[[dict[str, Any], Mapping[str, Any], set[str] | None], dict[str, Any]] | None = None,
    fresh_arm: bool = False,
    annotate: Callable[[dict[str, Any], Arm], None] | None = None,
) -> list[dict[str, Any]]:
    """H2.1 passes its own metric prefix, cache tag, extra details (the cell), prompt versions, and a
    classifier that may refine a wrong answer's failure category. H1 also passes its answer model and scoring
    function, asks for a fresh arm per question (its arms hold per-question state) and annotates each record
    from the arm."""
    versions = prompt_versions if prompt_versions is not None else PROMPT_VERSIONS
    local = threading.local()
    done: list[dict[str, Any]] = []
    lock = threading.Lock()

    stopped = threading.Event()

    def one(q: Mapping[str, Any]) -> None:
        if stopped.is_set():
            return  # the spend cap was reached: no new question starts
        if fresh_arm or not hasattr(local, "arm"):
            local.arm = make_arm()
        arm: Arm = local.arm
        loop = AgentLoop(client, arm, model, system, settings, tag=tag, answer_model=answer_model)
        try:
            t = loop.run(str(q["id"]), str(q["text"]))
        except BudgetExceeded as exc:  # the approved cap: stop, record why, score nothing for this question
            if not stopped.is_set():
                stopped.set()
                write(
                    f"{metric}.budget_stop",
                    0.0,
                    {
                        "question_id": q["id"],
                        "arm": arm_name,
                        "model": model,
                        "split": split,
                        "reason": str(exc),
                        **(extra or {}),
                    },
                )
                log(f"{arm_name} {model} {split}: stopped at the spend cap: {exc}")
            return
        except Exception as exc:  # an API or connection failure: logged, not scored, rerun later
            write(
                f"{metric}.error",
                0.0,
                {
                    "question_id": q["id"],
                    "arm": arm_name,
                    "model": model,
                    "split": split,
                    "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                    **(extra or {}),
                },
            )
            log(f"{arm_name} {model} {q['id']}: {type(exc).__name__}")
            return
        record = t.as_dict()
        record.update(
            {
                "split": split,
                "type": q["type"],
                "template": q["template"],
                "question": q["text"],
                "gold": q["gold"],
                "anchors": q.get("anchors", []),
                "prompt_versions": {"base": versions["base"], arm_name: versions[arm_name]},
                "system_prompt_sha256": sha256(system),
                "tools_sha256": arm.tools_hash(),
                "settings": settings.as_dict(),
            }
        )
        record.update(extra or {})
        if annotate is not None:
            annotate(record, arm)
        (score or score_record)(record, q, known_ids)
        if classify is not None and not record["correct"]:
            refined = classify(record)
            if refined is not None:
                record["failure"] = refined
        write(f"{metric}.run", 1.0 if record["correct"] else 0.0, record)
        with lock:
            done.append(record)
            if len(done) % 10 == 0:
                acc = sum(r["correct"] for r in done) / len(done)
                log(f"{arm_name} {model} {split}: {len(done)}/{len(questions)}, accuracy so far {acc:.2f}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(one, q) for q in questions]:
            future.result()
    return done
