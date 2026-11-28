"""H1 pins: every file the PREREG names matches its sha256, runs read the pins (not the live files), and the
test-run gate is open on this branch (code and generated artifacts still equal their pins)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from monograph.h1 import arms, build, pins

from .conftest import ROOT


def test_every_pin_matches_the_prereg() -> None:
    hashes = pins.pinned_hashes(ROOT)
    assert len(hashes) >= 30
    for rel, digest in hashes.items():
        assert pins.sha256((ROOT / rel).read_bytes()) == digest, rel


def test_the_pins_are_what_the_live_code_sends() -> None:
    for arm in arms.ARMS:
        assert pins.system_prompt(ROOT, arm) == arms.system_prompt(arm)
    assert pins.render_tools(pins.tools(ROOT)) == pins.render_tools(arms.TOOLS)


def test_a_test_run_is_allowed_now() -> None:
    # Not a CI invariant: an evolved stack (code, or the generated artifacts after a model change) correctly
    # refuses an H1 rerun. True on this branch.
    drift = pins.frozen_drift(ROOT)
    if drift:
        pytest.skip(f"the live stack has moved on since H1: {drift}")
    assert drift == []
    assert pins.pinned_stamp(ROOT) == build.live_stamp(ROOT)


def _copy(tmp_path: Path) -> Path:
    for part in ("docs", "src", "generated", "fixtures", "experiments"):
        shutil.copytree(ROOT / part, tmp_path / part)
    return tmp_path


def test_runs_read_the_pins(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    (root / "src/monograph/agent/prompts/h1_base_v1.md").write_text("edited\n")
    (root / "fixtures/h1/questions.json").write_text('{"questions": []}\n')
    assert "edited" not in pins.system_prompt(root, "ONT+OC")
    assert len(pins.questions(root)) == 360
    assert pins.describe_text(root, "ONT") == pins.describe_text(ROOT, "ONT")


def test_an_edited_pin_or_code_is_drift(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    (root / pins.FROZEN / "context.ONT.txt").write_text("edited\n")
    (root / "src/monograph/sqlcheck/check.py").write_text("# edited\n")
    drift = pins.frozen_drift(root)
    assert f"{pins.FROZEN}/context.ONT.txt" in drift
    assert "src/monograph/sqlcheck/check.py" in drift
