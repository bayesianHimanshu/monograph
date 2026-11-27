from __future__ import annotations

import json
from collections import Counter

from monograph.extract.sample import FrameEntry, Stratum, split, stratified_sample

from .conftest import ROOT


def _frame() -> list[FrameEntry]:
    out = []
    for n in range(300):
        cell = Stratum(
            ("single", "multi")[n % 2], ("solid_oral", "liquid", "injectable")[n % 3], ("rx", "otc")[n % 4 // 2]
        )
        out.append(FrameEntry(f"s{n:04d}", 1, cell, h6_gap=n < 10))
    out.append(FrameEntry("excluded", 1, Stratum("?", "?", "?"), True, "no narrative"))
    return out


def test_gap_labels_first_then_quotas_and_determinism() -> None:
    a = stratified_sample(_frame(), 120, seed=7)
    b = stratified_sample(_frame(), 120, seed=7)
    assert a == b
    ids = {e.set_id for e in a.entries}
    assert len(ids) == 120
    assert {f"s{n:04d}" for n in range(10)} <= ids
    assert "excluded" not in ids


def test_split_sizes_and_stratification() -> None:
    sample = stratified_sample(_frame(), 120, seed=7)
    dev, test = split(sample.entries, 24, seed=7)
    assert (len(dev), len(test)) == (24, 96)
    assert not {e.set_id for e in dev} & {e.set_id for e in test}
    cells = Counter(e.stratum.key() for e in dev)
    assert max(cells.values()) - min(cells.values()) <= 2


def test_committed_split_is_complete() -> None:
    data = json.loads((ROOT / "fixtures" / "h4" / "split.json").read_text())
    assert (len(data["dev"]), len(data["test"])) == (40, 160)
    assert sum(r["h6_gap"] for r in data["dev"] + data["test"]) == 48
    assert len({r["set_id"] for r in data["dev"] + data["test"]}) == 200
