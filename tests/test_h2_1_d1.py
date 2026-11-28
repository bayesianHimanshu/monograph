"""H2.1 D1: the rerun selection rule, recomputed from the committed results (docs/h2_1/DEVIATIONS.md).

Rule: rerun every test record written after the crash by a process that was running at the crash, whatever its
score, except the crashing run. These tests check that exactly those records were rerun, that the rule agrees
with the connection-error criterion, and that everything else that ran again was an identical cache replay.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import pytest

from .conftest import ROOT

CRASH = datetime(2026, 11, 28, 6, 50, 42, 356000, tzinfo=UTC)
RERUN_COMMIT = "62de336"
TRIGGER = ("claude-sonnet-5-5", "id-off_desc-off", "G-free", "t10_firms_within_six_steps-10")
LOST = re.compile(
    r"connection is lost|connection is closed|server closed the connection|terminating connection|recovery mode", re.I
)

Key = tuple[str, str, str, str]


def _records() -> list[dict[str, Any]]:
    out = []
    for path in sorted((ROOT / "results" / "h2_1").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r["metric"] in ("h2_1.run", "h2_1.error") and r["details"].get("split") == "test":
                out.append(r)
    return out


def _key(r: dict[str, Any]) -> Key:
    d = r["details"]
    return (d["model"], d["cell"], d["arm"], d["question_id"])


def _ts(r: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(r["timestamp"])


def _conn_error(r: dict[str, Any]) -> bool:
    calls = r["details"].get("calls", []) if r["metric"] == "h2_1.run" else []
    return any(c.get("is_error") and LOST.search(c.get("preview", "")) for c in calls)


@pytest.fixture(scope="module")
def d1() -> dict[str, Any]:
    records = _records()
    if not records:
        pytest.skip("no H2.1 test results in this copy")
    before = [r for r in records if not r["clean_commit"].startswith(RERUN_COMMIT)]
    after = [r for r in records if r["clean_commit"].startswith(RERUN_COMMIT)]
    by_process: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in before:
        by_process[r["run_id"]].append(r)
    alive = {p for p, rs in by_process.items() if min(map(_ts, rs)) < CRASH < max(map(_ts, rs))}
    selected = [r for r in before if r["run_id"] in alive and _ts(r) > CRASH and _key(r) != TRIGGER]
    return {"before": before, "after": after, "alive": alive, "selected": selected}


def test_the_rule_selects_what_the_deviation_states(d1: dict[str, Any]) -> None:
    sel = d1["selected"]
    assert len(sel) == 1219
    assert sum(1 for r in sel if r["metric"] == "h2_1.error") == 1083
    runs = [r for r in sel if r["metric"] == "h2_1.run"]
    assert sum(1 for r in runs if r["details"]["arm"] == "S-tools") == 5
    assert sum(1 for r in runs if r["details"]["arm"] == "G-free") == 131
    assert all(LOST.search(r["details"]["error"]) for r in sel if r["metric"] == "h2_1.error")


def test_every_selected_record_was_rerun(d1: dict[str, Any]) -> None:
    rerun = {_key(r) for r in d1["after"]}
    assert {_key(r) for r in d1["selected"]} <= rerun


def test_only_the_selected_records_got_a_new_sample(d1: dict[str, Any]) -> None:
    unscored = {_key(r) for r in d1["before"] if r["metric"] == "h2_1.error"}
    fresh = {_key(r) for r in d1["after"] if not r["details"].get("from_cache") or _key(r) in unscored}
    selected = {_key(r) for r in d1["selected"]}
    in_flight = {_key(r) for r in d1["selected"] if r["metric"] == "h2_1.run" and r["details"]["arm"] == "S-tools"}
    assert fresh == selected - in_flight


def test_the_rule_agrees_with_the_connection_errors(d1: dict[str, Any]) -> None:
    errored = [r for r in d1["before"] if _conn_error(r)]
    assert errored
    assert all(r["run_id"] in d1["alive"] and _ts(r) > CRASH for r in errored)
    sonnet_after = {
        _key(r)
        for r in d1["before"]
        if r["run_id"] in d1["alive"] and _ts(r) > CRASH and r["details"]["model"] == "claude-sonnet-5-5"
    }
    assert sonnet_after == {_key(r) for r in errored}


def test_nothing_correct_on_a_live_connection_was_resampled_or_changed(d1: dict[str, Any]) -> None:
    selected = {_key(r) for r in d1["selected"]}
    original = {_key(r): r for r in d1["before"] if r["metric"] == "h2_1.run"}
    replays = 0
    for r in d1["after"]:
        k = _key(r)
        if k in selected or k == TRIGGER:
            continue
        o = original[k]
        assert _ts(o) < CRASH  # a run before the crash, on a live connection
        assert r["details"]["from_cache"], k  # no new sample
        for field in ("answer", "correct", "f1"):
            assert r["details"][field] == o["details"][field], (k, field)
        assert len(r["details"]["calls"]) == len(o["details"]["calls"]), k
        replays += 1
    assert replays == 172


def test_the_unscorable_question_is_outside_the_rule(d1: dict[str, Any]) -> None:
    key = ("claude-haiku-5-5", "id-on_desc-on", "G-free", "t10_fewest_steps_substance_to_firm-13")
    errors = [r for r in d1["before"] if r["metric"] == "h2_1.error" and _key(r) == key]
    assert errors
    assert all(r["run_id"] not in d1["alive"] for r in errors)
    assert key not in {_key(r) for r in d1["after"]}
