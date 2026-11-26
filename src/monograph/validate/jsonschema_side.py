"""A small JSON Schema validator for the keywords MONOGRAPH's generator emits (draft 2020-12 subset).

No JSON Schema library is on the allowed dependency list, so this validates the generated schema against
real records. It is fail-closed: a keyword it does not implement raises `UnsupportedKeyword` instead of
being skipped, so a schema can never pass because a constraint was ignored.

Regexes are evaluated with Python `re.search`. JSON Schema specifies ECMA-262 regexes; for the patterns in
the safe subset (and Pydantic's own Decimal pattern) the two agree on the strings tested here.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date
from operator import ge, gt, le, lt
from typing import Any

# Keywords that carry no validation meaning.
_ANNOTATIONS = {"$schema", "$id", "$comment", "title", "description", "default", "examples", "$defs", "deprecated"}
_SUPPORTED = _ANNOTATIONS | {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "enum",
    "const",
    "pattern",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "items",
    "uniqueItems",
    "minItems",
    "maxItems",
    "$ref",
    "anyOf",
    "allOf",
    "if",
    "then",
    "contains",
    "minContains",
    "format",
    "not",
}
_FORMATS = {
    "date": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "date-time": re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"),
    "uri": re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:\S+$"),
}


# Invalid curated fixtures the generated JSON Schema cannot reject, each with its reason. Every other invalid
# fixture must be rejected (checked by `monograph check agreement` and tests/test_jsonschema_side.py).
DECLARED_LEAKS: dict[str, str] = {
    "AdverseEventReport/age-151": "Decimal bound on a string-encoded number",
    "AdverseEventReport/age-negative": "Decimal bound on a string-encoded number",
    "Product/strength-zero": "Decimal bound on a string-encoded number",
    "LabelExtraction/basis-zero": "Decimal bound on a string-encoded number",
    "Product/strength-float": "a JSON float is a number to JSON Schema; strict Pydantic rejects it for a Decimal",
    "Label/future-effective-date": "pydantic_only",
    "Label/duplicate-section-code": "pydantic_only",
    "Product/duplicate-substance": "pydantic_only",
}


class UnsupportedKeyword(ValueError):
    """The schema uses a keyword this validator does not implement."""


class JsonSchemaValidator:
    def __init__(self, document: dict[str, Any]) -> None:
        self._root = document

    def errors(self, instance: Any, schema: dict[str, Any] | None = None, path: str = "$") -> list[str]:
        return list(self._errors(instance, self._root if schema is None else schema, path))

    def errors_for(self, definition: str, instance: Any) -> list[str]:
        return self.errors(instance, self._root["$defs"][definition])

    def _resolve(self, ref: str) -> dict[str, Any]:
        if not ref.startswith("#/$defs/"):
            raise UnsupportedKeyword(f"only local $defs references are supported, got {ref!r}")
        target: dict[str, Any] = self._root["$defs"][ref.removeprefix("#/$defs/")]
        return target

    def _errors(self, x: Any, s: dict[str, Any], path: str) -> Iterator[str]:
        for key in s:
            if key not in _SUPPORTED and not key.startswith("x-"):
                raise UnsupportedKeyword(f"{path}: keyword {key!r} is not implemented")
        if "$ref" in s:
            yield from self._errors(x, self._resolve(s["$ref"]), path)
        if "type" in s and not _type_ok(x, s["type"]):
            yield f"{path}: expected type {s['type']}, got {type(x).__name__}"
            return
        if "enum" in s and x not in s["enum"]:
            yield f"{path}: {x!r} is not one of the enum values"
        if "const" in s and x != s["const"]:
            yield f"{path}: {x!r} is not {s['const']!r}"
        if isinstance(x, str):
            yield from self._string(x, s, path)
        if isinstance(x, int | float) and not isinstance(x, bool):
            yield from self._number(x, s, path)
        if isinstance(x, list):
            yield from self._array(x, s, path)
        if isinstance(x, dict):
            yield from self._object(x, s, path)
        if "anyOf" in s and not any(not self.errors(x, sub, path) for sub in s["anyOf"]):
            yield f"{path}: matches none of anyOf"
        for sub in s.get("allOf", []):
            yield from self._errors(x, sub, path)
        if "not" in s and not self.errors(x, s["not"], path):
            yield f"{path}: matches a schema it must not match"
        if "if" in s and not self.errors(x, s["if"], path) and "then" in s:
            yield from self._errors(x, s["then"], path)

    def _string(self, x: str, s: dict[str, Any], path: str) -> Iterator[str]:
        if "pattern" in s and re.search(s["pattern"], x) is None:
            yield f"{path}: {x!r} does not match {s['pattern']!r}"
        if "minLength" in s and len(x) < s["minLength"]:
            yield f"{path}: shorter than {s['minLength']}"
        if "maxLength" in s and len(x) > s["maxLength"]:
            yield f"{path}: longer than {s['maxLength']}"
        fmt = s.get("format")
        if fmt is not None:
            if fmt not in _FORMATS:
                raise UnsupportedKeyword(f"{path}: format {fmt!r} is not implemented")
            if _FORMATS[fmt].match(x) is None or (fmt == "date" and not _real_date(x)):
                yield f"{path}: {x!r} is not a {fmt}"

    def _number(self, x: float, s: dict[str, Any], path: str) -> Iterator[str]:
        for key, holds in (("minimum", ge), ("maximum", le), ("exclusiveMinimum", gt), ("exclusiveMaximum", lt)):
            if key in s and not holds(x, s[key]):
                yield f"{path}: {x} violates {key} {s[key]}"

    def _array(self, x: list[Any], s: dict[str, Any], path: str) -> Iterator[str]:
        if "minItems" in s and len(x) < s["minItems"]:
            yield f"{path}: fewer than {s['minItems']} items"
        if "maxItems" in s and len(x) > s["maxItems"]:
            yield f"{path}: more than {s['maxItems']} items"
        if s.get("uniqueItems") and len({repr(v) for v in x}) != len(x):
            yield f"{path}: items are not unique"
        if "items" in s:
            for i, v in enumerate(x):
                yield from self._errors(v, s["items"], f"{path}[{i}]")
        if "contains" in s:
            matches = sum(not self.errors(v, s["contains"], path) for v in x)
            if matches < s.get("minContains", 1):
                yield f"{path}: fewer than {s.get('minContains', 1)} items match contains"

    def _object(self, x: dict[str, Any], s: dict[str, Any], path: str) -> Iterator[str]:
        properties = s.get("properties", {})
        for name in s.get("required", []):
            if name not in x:
                yield f"{path}: missing required property {name!r}"
        for name, value in x.items():
            if name in properties:
                yield from self._errors(value, properties[name], f"{path}.{name}")
            elif s.get("additionalProperties") is False:
                yield f"{path}: property {name!r} is not allowed"
            elif isinstance(s.get("additionalProperties"), dict):
                yield from self._errors(value, s["additionalProperties"], f"{path}.{name}")


def _real_date(text: str) -> bool:
    """RFC 3339 full-date: the shape and a real calendar day."""
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _type_ok(x: Any, expected: str | list[str]) -> bool:
    kinds = [expected] if isinstance(expected, str) else expected
    checks = {
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
        "null": lambda v: v is None,
    }
    for kind in kinds:
        if kind not in checks:
            raise UnsupportedKeyword(f"type {kind!r} is not implemented")
    return any(checks[kind](x) for kind in kinds)
