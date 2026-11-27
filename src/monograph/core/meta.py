"""Ontology annotation types attached to the Pydantic models.

These are plain frozen dataclasses, so mypy checks every argument. `core/spec.py` is the only module
that reads them, and it fails generation when one is missing.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any

from pydantic import GetCoreSchemaHandler
from pydantic_core import PydanticCustomError, core_schema

DEFAULT_BASE = "https://example.org/monograph/"
VOCAB_NS = "https://example.org/monograph/ontology#"
SHAPES_NS = "https://example.org/monograph/shapes#"


@dataclass(frozen=True, slots=True)
class OntClass:
    """Class-level metadata, attached as `__ont__: ClassVar[OntClass] = OntClass(...)`.

    name:     local name of the owl:Class under the vocabulary namespace, PascalCase.
    label:    rdfs:label.
    path:     IRI path segment for instances of an entity (for example "substance"). None for
              embedded values, whose IRIs derive from their parent, and for abstract classes.
    key:      field names whose values form the instance IRI, in order. Embedded values that sit in
              a set must have a key; embedded values in a list are keyed by position instead.
    abstract: an abstract base that only exists to become an rdfs:subClassOf target.
    """

    name: str
    label: str
    path: str | None = None
    key: tuple[str, ...] = ()
    abstract: bool = False


@dataclass(frozen=True, slots=True)
class OntProp:
    """Field-level metadata, attached with `Annotated[T, OntProp("localName", "label")]`.

    The same local name may be reused by several classes (for example "name"); `core/spec.py`
    checks that every use agrees on kind and range.
    """

    name: str
    label: str


@dataclass(frozen=True, slots=True)
class OntEnum:
    """Enumeration metadata, attached as a dunder attribute in the StrEnum body: `__ont__ = OntEnum(...)`.

    Enum never turns dunder names into members, so this stays out of the member list.
    """

    name: str
    label: str


# ---------------------------------------------------------------------------------------------------
# Safe string types
# ---------------------------------------------------------------------------------------------------


class Charset(StrEnum):
    """Which control characters a string may contain.

    IDENTIFIER: no control character at all (Unicode category Cc, which includes NUL and newlines).
    TEXT:       no control character except tab, line feed and carriage return (so no NUL).
    """

    IDENTIFIER = "identifier"
    TEXT = "text"

    def allows(self, ch: str) -> bool:
        if unicodedata.category(ch) != "Cc":
            return True
        return self is Charset.TEXT and ch in "\t\n\r"

    def forbidden_ranges(self) -> tuple[tuple[int, int], ...]:
        """Code point ranges of the forbidden characters, for the SHACL and Postgres projections."""
        if self is Charset.IDENTIFIER:
            return ((0x00, 0x1F), (0x7F, 0x9F))
        return ((0x00, 0x08), (0x0B, 0x0C), (0x0E, 0x1F), (0x7F, 0x9F))


@dataclass(frozen=True, slots=True)
class SafeChars:
    """Annotated marker that restricts control characters. Projected by core/spec.py into every layer."""

    charset: Charset

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        charset = self.charset

        def check(value: str) -> str:
            bad = next((ch for ch in value if not charset.allows(ch)), None)
            if bad is not None:
                raise PydanticCustomError(
                    "control_character",
                    "{charset} must not contain control character U+{code}",
                    {"charset": charset.value, "code": f"{ord(bad):04X}"},
                )
            return value

        return core_schema.no_info_after_validator_function(check, handler(source))


SafeIdentifier = Annotated[str, SafeChars(Charset.IDENTIFIER)]
SafeText = Annotated[str, SafeChars(Charset.TEXT)]


@dataclass(frozen=True, slots=True)
class Derived:
    """Annotated marker: the field is filled by deterministic code, never by an extractor.

    by:                  what fills it (for documentation and the leak inventory).
    required_in_context: in the graph-context shapes the field must be present (minCount 1), so a missing
                         value means "could not be resolved against the reference graph".

    The extraction JSON Schema (the one sent to an LLM) leaves derived fields out.
    """

    by: str
    required_in_context: bool = False
