"""Append-only quarantine for records that fail SafeIdentifier or SafeText.

A quarantined record is stored exactly as the source delivered it, next to the reason. Nothing is
stripped, replaced or re-encoded: the point is to keep the evidence, not to make the record pass.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any


class QuarantineStore:
    def __init__(self, directory: Path, clock: Callable[[], datetime]) -> None:
        self._directory = directory
        self._clock = clock

    def add(self, entries: Sequence[dict[str, Any]], run_id: str) -> Path | None:
        if not entries:
            return None
        now = self._clock()
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / f"{now.date().isoformat()}.jsonl"
        data = "".join(
            json.dumps({"run_id": run_id, "timestamp": now.isoformat(), **e}, sort_keys=True, ensure_ascii=False) + "\n"
            for e in entries
        ).encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return path
