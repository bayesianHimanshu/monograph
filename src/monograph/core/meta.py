"""Ontology annotation types attached to the Pydantic models.

These are plain frozen dataclasses, so mypy checks every argument. `core/spec.py` is the only module
that reads them, and it fails generation when one is missing.
"""

from __future__ import annotations

from dataclasses import dataclass

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
