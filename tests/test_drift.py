from __future__ import annotations

from pathlib import Path

from monograph.validate.drift import DriftChecker


def test_drift_detects_change_and_missing(tmp_path: Path) -> None:
    (tmp_path / "a.ttl").write_text("same\n")
    (tmp_path / "b.ttl").write_text("old\n")
    report = DriftChecker(tmp_path).check({"a.ttl": "same\n", "b.ttl": "new\n", "c.ttl": "x"})
    assert report.changed == ("b.ttl",)
    assert report.missing == ("c.ttl",)
    assert not report.ok


def test_committed_generated_files_have_no_drift() -> None:
    from monograph import wiring
    from monograph.core.vocab import Vocabulary

    files = wiring.artifact_builder(wiring.build_spec(), Vocabulary()).build().files
    root = Path(__file__).resolve().parents[1]
    assert DriftChecker(root / "generated").check(files).ok
