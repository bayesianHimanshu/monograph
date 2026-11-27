"""PREREG.md freezes the prompt, schema, feedback, resolution and scoring code by sha256, and docs/h4/
DEVIATIONS.md pins each of those files under experiments/h4/frozen/. A pin that changes, or a live code file
that changes, fails here and blocks `monograph h4 run --split test`. H4 loads its data from the pins, so the
live model is free to evolve."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from monograph.extract.cli import frozen_drift
from monograph.extract.setup import (
    CONTEXT_SHAPES_REF,
    EXTRACTION_SCHEMA_REF,
    SPLIT_REF,
    h4_context_shapes,
    h4_extraction_schema,
    h4_split,
)
from monograph.validate.prereg import frozen_table, pins, sha256_file

from .conftest import ROOT

PINNED_SCHEMA = "experiments/h4/frozen/monograph.extraction.schema.json"
PINNED_SCHEMA_SHA256 = "600131d475ec05e4c0a6f430efd8d607c3d068e77dd63bfcf7c5cac6a4e70661"


def _copy(tmp_path: Path) -> Path:
    for part in ("docs", "src", "generated", "fixtures", "experiments"):
        shutil.copytree(ROOT / part, tmp_path / part)
    return tmp_path


def test_frozen_artifacts_match_prereg() -> None:
    assert frozen_drift(ROOT) == []


def test_the_extraction_schema_pin_is_the_2_1_1_file_h4_ran_with() -> None:
    pin = pins(ROOT, "h4")[EXTRACTION_SCHEMA_REF]
    assert pin.pinned == PINNED_SCHEMA
    assert pin.sha256 == PINNED_SCHEMA_SHA256 == frozen_table(ROOT, "h4")[EXTRACTION_SCHEMA_REF]
    assert sha256_file(ROOT / PINNED_SCHEMA) == PINNED_SCHEMA_SHA256
    assert json.loads((ROOT / PINNED_SCHEMA).read_text())["x-schema-version"] == "2.1.1"
    assert (ROOT / PINNED_SCHEMA).read_bytes() == (
        ROOT / "generated" / "releases" / "2.1.1" / "monograph.extraction.schema.json"
    ).read_bytes()


def test_a_rerun_loads_the_pins_never_the_live_paths(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    for ref in (EXTRACTION_SCHEMA_REF, CONTEXT_SHAPES_REF, SPLIT_REF):
        (root / ref).write_text("not what H4 ran with\n")
    assert h4_extraction_schema(root) == json.loads((ROOT / PINNED_SCHEMA).read_text())
    assert h4_context_shapes(root) == root / "experiments/h4/frozen/monograph.context.shapes.ttl"
    assert h4_split(root) == json.loads((ROOT / "experiments/h4/frozen/split.json").read_text())
    assert frozen_drift(root) == []  # the live generated files are free to change


def test_a_tampered_pin_is_reported(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    (root / PINNED_SCHEMA).write_text((root / PINNED_SCHEMA).read_text().replace('"2.1.1"', '"2.1.2"'))
    drift = frozen_drift(root)
    assert PINNED_SCHEMA in drift


@pytest.mark.parametrize("edited", ["src/monograph/extract/score.py", "experiments/h4/frozen/split.json"])
def test_a_changed_frozen_file_is_reported(tmp_path: Path, edited: str) -> None:
    root = _copy(tmp_path)
    (root / edited).write_text((root / edited).read_text() + "\n# edited\n")
    assert edited in frozen_drift(root)
