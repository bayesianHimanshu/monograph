"""PREREG.md freezes the prompt, schema, feedback, resolution and scoring code by sha256. A change to any of
them after pre-registration fails here and blocks `monograph h4 run --split test` (log it as a deviation)."""

from __future__ import annotations

import shutil
from pathlib import Path

from monograph.extract.cli import frozen_drift

from .conftest import ROOT


def test_frozen_artifacts_match_prereg() -> None:
    assert frozen_drift(ROOT) == []


def test_a_changed_frozen_file_is_reported(tmp_path: Path) -> None:
    for part in ("docs", "src", "generated", "fixtures", "data"):
        if (ROOT / part).exists():
            (tmp_path / part).symlink_to(ROOT / part) if part == "data" else shutil.copytree(
                ROOT / part, tmp_path / part
            )
    score = tmp_path / "src" / "monograph" / "extract" / "score.py"
    score.write_text(score.read_text() + "\n# edited\n")
    assert "src/monograph/extract/score.py" in frozen_drift(tmp_path)
