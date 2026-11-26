"""Curated fixtures: `fixtures/valid/<Class>.json`, `fixtures/invalid/<Class>.json`, `fixtures/probes/*.json`."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from monograph.core.spec import SchemaSpec


@dataclass(frozen=True, slots=True)
class Fixture:
    id: str
    cls: type[BaseModel]
    record: dict[str, Any]
    expect: str  # an agreement Outcome value
    violates: str | None = None
    field: str | None = None
    note: str | None = None

    @property
    def name(self) -> str:
        return f"{self.cls.__name__}/{self.id}"


class FixtureLoader:
    def __init__(self, spec: SchemaSpec, directory: Path) -> None:
        self._spec = spec
        self._directory = directory

    def _read(self, path: Path) -> list[dict[str, Any]]:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, list)
        return data

    def valid(self) -> list[Fixture]:
        out = []
        for path in sorted((self._directory / "valid").glob("*.json")):
            cls = self._spec.by_name(path.stem).cls
            out += [Fixture(d["id"], cls, d["record"], "agree_valid") for d in self._read(path)]
        return out

    def invalid(self) -> list[Fixture]:
        out = []
        for path in sorted((self._directory / "invalid").glob("*.json")):
            cls = self._spec.by_name(path.stem).cls
            out += [
                Fixture(d["id"], cls, d["record"], d["expect"], d["violates"], d.get("field"), d.get("note"))
                for d in self._read(path)
            ]
        return out

    def probes(self) -> list[Fixture]:
        out = []
        for path in sorted((self._directory / "probes").glob("*.json")):
            for d in self._read(path):
                cls = self._spec.by_name(d["class"]).cls
                out.append(Fixture(d["id"], cls, d["record"], d["expect"], None, None, d.get("note")))
        return out
