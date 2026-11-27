"""H2's frozen artifacts are pinned under experiments/h2/frozen/ (docs/h2/DEVIATIONS.md, D1): the pins hash to
the PREREG and to the run records, a rerun uses them and never the live paths, and tampering is refused."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from monograph.agent.arms import ARMS, Arm
from monograph.graph.cli import h2_frozen_drift, load_questions
from monograph.qa import pins

from .conftest import ROOT


def _copy(tmp_path: Path) -> Path:
    for part in ("docs", "src", "generated", "fixtures", "experiments"):
        shutil.copytree(ROOT / part, tmp_path / part)
    return tmp_path


def test_the_pins_are_intact() -> None:
    assert pins.pin_problems(ROOT) == []


def test_a_test_rerun_is_allowed_now() -> None:
    # Not a CI invariant (an evolved model correctly refuses an H2 rerun); true on this branch, where the
    # live code and store are still the ones H2 ran on. Skipped once they move on.
    drift = h2_frozen_drift(ROOT)
    if drift:
        pytest.skip(f"the live stack has moved on since H2: {drift}")
    assert drift == []


def test_pinned_tool_files_hash_as_the_runner_hashes_them() -> None:
    for arm in ARMS:
        defs = pins.tools(ROOT, arm)
        data = (ROOT / "experiments" / "h2" / "frozen" / f"tools.{arm}.json").read_bytes()
        assert json.dumps(list(defs), sort_keys=True).encode("utf-8") == data
        assert Arm(arm, "sql", "tools", defs, lambda t, a: "").tools_hash() == pins.hashes(ROOT)[arm][1]


def test_the_pins_are_what_the_test_runs_used() -> None:
    results = ROOT / "results" / "h2"
    if not results.exists():
        pytest.skip("results/ is not in this copy")
    from monograph.harness.results import read_results

    seen: dict[str, set[tuple[str, str]]] = {}
    for r in read_results(results):
        d = r["details"]
        if r["metric"] == "h2.run" and d["split"] == "test":
            seen.setdefault(d["arm"], set()).add((d["system_prompt_sha256"], d["tools_sha256"]))
    assert seen == {arm: {h} for arm, h in pins.hashes(ROOT).items()}


def test_a_rerun_uses_the_pins_never_the_live_paths(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    (root / "fixtures" / "h2" / "questions.json").write_text('{"questions": []}\n')
    for name in ("base_v1.md", "arm_S-free_v1.md"):
        (root / "src" / "monograph" / "agent" / "prompts" / name).write_text("edited\n")
    assert len(load_questions(root, "test")) == 240
    assert pins.system_prompt(root, "S-free") == pins.system_prompt(ROOT, "S-free")
    assert "edited" not in pins.system_prompt(root, "S-free")
    calls: list[tuple[str, dict[str, Any]]] = []

    def live(tool: str, arguments: dict[str, Any]) -> str:
        calls.append((tool, arguments))
        return "live schema text"

    arm = pins.pin_arm(Arm("S-free", "sql", "free", (), live), root)
    assert arm.tools == pins.tools(ROOT, "S-free")
    assert arm.execute("describe_schema", {}) == pins.schema_text(ROOT, "sql")
    assert arm.execute("run_sql", {"sql": "SELECT 1"}) == "live schema text"
    assert [c[0] for c in calls] == ["describe_schema", "run_sql"]
    assert pins.pin_problems(root) == []  # live prompts and questions are free to change


@pytest.mark.parametrize(
    "edited",
    [
        "experiments/h2/frozen/tools.S-free.json",
        "experiments/h2/frozen/schema.graph.txt",
        "experiments/h2/frozen/questions.json",
        "src/monograph/tools/registry.py",
        "generated/monograph.ddl.sql",
    ],
)
def test_a_tampered_pin_or_executing_file_refuses_a_rerun(tmp_path: Path, edited: str) -> None:
    root = _copy(tmp_path)
    (root / edited).write_text((root / edited).read_text() + "\n")
    assert edited in h2_frozen_drift(root)
    assert (edited in pins.pin_problems(root)) == edited.startswith("experiments/")
