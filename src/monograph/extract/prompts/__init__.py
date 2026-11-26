"""Versioned prompts. A prompt's identity is the SHA-256 of the exact text sent, schema included."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROMPT_DIR = Path(__file__).parent


@dataclass(frozen=True, slots=True)
class PromptSet:
    version: str
    system: str  # instructions followed by the schema: the cached prefix
    schema: dict[str, Any]  # the transformed schema sent as output_config.format

    @property
    def system_hash(self) -> str:
        return hashlib.sha256(self.system.encode("utf-8")).hexdigest()

    @property
    def schema_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.schema, sort_keys=True).encode("utf-8")).hexdigest()


def load_prompts(version: str, extraction_schema: dict[str, Any]) -> PromptSet:
    """`extraction_schema` is generated/monograph.extraction.schema.json; it is passed through the SDK's
    documented `anthropic.transform_schema`, never edited by hand."""
    import anthropic

    instructions = (PROMPT_DIR / f"system_{version}.md").read_text(encoding="utf-8")
    schema = anthropic.transform_schema(extraction_schema)
    system = instructions + "\n" + json.dumps(schema, indent=1, sort_keys=True) + "\n"
    return PromptSet(version, system, schema)


def user_message(narrative: str) -> str:
    return f"Label text:\n\n{narrative}"
