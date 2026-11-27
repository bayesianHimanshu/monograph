"""Builds every generated artifact in memory and writes them to `generated/`."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

OWL_FILE = "monograph.owl.ttl"
SHACL_FILE = "monograph.shapes.ttl"
JSON_SCHEMA_FILE = "monograph.schema.json"
DDL_FILE = "monograph.ddl.sql"
DDL_TEST_FILE = "monograph.test.ddl.sql"
EXTRACTION_SCHEMA_FILE = "monograph.extraction.schema.json"
CONTEXT_SHACL_FILE = "monograph.context.shapes.ttl"
AGE_FILE = "monograph.age.json"


@dataclass(frozen=True, slots=True)
class Artifacts:
    files: dict[str, str]
    seconds: dict[str, float]


class ArtifactBuilder:
    def __init__(self, renderers: Mapping[str, Callable[[], str]], timer: Callable[[], float]) -> None:
        self._renderers = dict(renderers)
        self._timer = timer

    def build(self) -> Artifacts:
        files: dict[str, str] = {}
        seconds: dict[str, float] = {}
        for name, render in self._renderers.items():
            start = self._timer()
            files[name] = render()
            seconds[name] = self._timer() - start
        return Artifacts(files, seconds)


def write_artifacts(artifacts: Artifacts, directory: Path) -> list[str]:
    """Write files whose content changed. Returns the names written."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in sorted(artifacts.files.items()):
        path = directory / name
        if not path.exists() or path.read_bytes() != text.encode("utf-8"):
            path.write_bytes(text.encode("utf-8"))
            written.append(name)
    return written
