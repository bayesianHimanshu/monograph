"""Append-only JSONL results.

The writer can only append: it opens files with O_APPEND and never truncates, renames or deletes.
Each line is self-describing (run id, UTC timestamp, git SHA, schema version, experiment, metric,
value, details), so the report can be rebuilt from the files alone.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

EXPERIMENT = "H6"


class ResultsWriter:
    def __init__(
        self,
        directory: Path,
        run_id: str,
        git_sha: str,
        schema_version: str,
        clock: Callable[[], datetime],
        experiment: str = EXPERIMENT,
    ) -> None:
        self._directory = directory
        self._experiment = experiment
        self._run_id = run_id
        self._git_sha = git_sha
        self._schema_version = schema_version
        self._clock = clock

    @property
    def run_id(self) -> str:
        return self._run_id

    def write(self, metric: str, value: Any, details: dict[str, Any] | None = None) -> None:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("the results clock must return timezone-aware UTC times")
        line = {
            "run_id": self._run_id,
            "timestamp": now.isoformat(),
            "git_sha": self._git_sha,
            "schema_version": self._schema_version,
            "experiment": self._experiment,
            "metric": metric,
            "value": value,
            "details": details or {},
        }
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / f"{now.date().isoformat()}.jsonl"
        data = (json.dumps(line, sort_keys=True, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)


def read_results(directory: Path) -> Iterator[dict[str, Any]]:
    for path in sorted(directory.glob("*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)
