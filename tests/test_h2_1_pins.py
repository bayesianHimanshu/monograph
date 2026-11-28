"""H2.1 pins (experiments/h2_1/frozen/): they hash to the PREREG, runs read them, and tampering with a pin or with
executing code refuses a test run. Skipped until the PREREG exists."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from monograph.ablation import pins
from monograph.ablation.cells import CELLS
from monograph.ablation.prereg import _definitions
from monograph.ablation.run import system_prompt
from monograph.agent.arms import ARMS

from .conftest import ROOT

pytestmark = pytest.mark.skipif(not (ROOT / pins.PREREG).exists(), reason="docs/h2_1/PREREG.md is not written yet")


def _copy(tmp_path: Path) -> Path:
    for part in ("docs", "src", "generated", "fixtures", "experiments"):
        shutil.copytree(ROOT / part, tmp_path / part)
    return tmp_path


def test_every_pin_hashes_to_the_prereg() -> None:
    hashes = pins.pinned_hashes(ROOT)
    assert hashes
    for rel, digest in hashes.items():
        assert pins.sha256((ROOT / rel).read_bytes()) == digest, rel


def test_the_pins_are_what_the_live_code_sends() -> None:
    for arm in ARMS:
        assert pins.system_prompt(ROOT, arm) == system_prompt(arm)
        for identity in (True, False):
            pinned = json.loads((ROOT / pins.FROZEN / pins.tools_file(arm, identity)).read_text(encoding="utf-8"))
            assert pinned == json.loads(pins.render_tools(_definitions(arm, identity, ROOT)))


def test_a_test_run_is_allowed_now() -> None:
    # Not a CI invariant: an evolved model correctly refuses an H2.1 rerun. True on this branch.
    drift = pins.frozen_drift(ROOT)
    if drift:
        pytest.skip(f"the live stack has moved on since H2.1: {drift}")
    assert drift == []


def test_runs_read_the_pins(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    (root / pins.QUESTIONS).write_text('{"questions": []}\n')
    (root / "src/monograph/agent/prompts/base_v2.md").write_text("edited\n")
    assert len(pins.questions(root)["questions"]) == 420
    assert "edited" not in pins.system_prompt(root, "S-free")
    for cell in CELLS:
        assert pins.ddl(root, cell) == pins.ddl(ROOT, cell)


@pytest.mark.parametrize(
    "edited",
    [
        "experiments/h2_1/frozen/questions.json",
        "experiments/h2_1/frozen/tools.G-tools.id-off.json",
        "experiments/h2_1/frozen/schema.id-off_desc-off.graph.txt",
        "src/monograph/tools/service.py",
        "generated/h2_1.id-on_desc-off.monograph.ddl.sql",
    ],
)
def test_a_tampered_pin_or_executing_file_refuses_a_test_run(tmp_path: Path, edited: str) -> None:
    root = _copy(tmp_path)
    (root / edited).write_text((root / edited).read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert edited in pins.frozen_drift(root)
