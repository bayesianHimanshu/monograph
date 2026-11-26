from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from monograph.harness.results import ResultsWriter, read_results


def _writer(tmp_path: Path, when: datetime) -> ResultsWriter:
    return ResultsWriter(tmp_path, "run1", "abc123", "0.1.0", lambda: when)


def test_lines_are_appended_and_self_describing(tmp_path: Path) -> None:
    when = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    path = tmp_path / "2026-11-26.jsonl"
    path.write_text('{"existing": true}\n')
    _writer(tmp_path, when).write("m", 1, {"a": 1})
    _writer(tmp_path, when).write("m", 2)
    lines = path.read_text().splitlines()
    assert lines[0] == '{"existing": true}'
    second = json.loads(lines[1])
    assert second == {
        "run_id": "run1",
        "timestamp": "2026-11-26T12:00:00+00:00",
        "git_sha": "abc123",
        "schema_version": "0.1.0",
        "experiment": "H6",
        "metric": "m",
        "value": 1,
        "details": {"a": 1},
    }
    assert [r.get("value") for r in read_results(tmp_path)] == [None, 1, 2]


def test_naive_clock_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone"):
        _writer(tmp_path, datetime(2026, 1, 1)).write("m", 1)


def test_writer_has_no_way_to_delete_or_overwrite() -> None:
    public = {name for name in dir(ResultsWriter) if not name.startswith("_")}
    assert public == {"write", "run_id"}
