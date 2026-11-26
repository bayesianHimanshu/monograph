"""Turn a JSON record into an unvalidated model tree with `model_construct`.

Both validators judge this one object: Pydantic re-validates it (strict, `revalidate_instances`
is "always") and the RDF mapping turns whatever it holds into triples for SHACL.

Lifting from JSON to Python types happens only where it is lossless and unambiguous:
ISO `YYYY-MM-DD` strings to `date`, ISO strings to `datetime`, strings and ints to `Decimal`, enum
values to members, IRI strings to `Ref`, objects to embedded models, and arrays to `frozenset` for set
fields. Anything else is left exactly as it was, so a bad value reaches both validators unchanged.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel

from monograph.core.refs import Ref
from monograph.core.spec import Card, FieldSpec, Kind, SchemaSpec

_ISO_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


class Instantiator:
    def __init__(self, spec: SchemaSpec) -> None:
        self._spec = spec

    def instantiate(self, cls: type[BaseModel], data: Mapping[str, Any]) -> BaseModel:
        c = self._spec.of(cls)
        values: dict[str, Any] = {}
        extras: dict[str, Any] = {}
        for key, raw in data.items():
            f = c.find(key)
            if f is None:
                extras[key] = raw
            else:
                values[key] = self._field(f, raw)
        instance = cls.model_construct(**values)
        if extras:
            # model_construct drops unknown keys unless extra="allow". Pydantic keeps extras in this slot,
            # and revalidation with extra="forbid" reports them, so we put them back where it looks.
            object.__setattr__(instance, "__pydantic_extra__", extras)
        return instance

    def _field(self, f: FieldSpec, raw: Any) -> Any:
        if raw is None:
            return None
        if f.card is Card.LIST:
            return [self._item(f, x) for x in raw] if isinstance(raw, list) else raw
        if f.card is Card.SET:
            if not isinstance(raw, list | tuple | set | frozenset):
                return raw
            items = [self._item(f, x) for x in raw]
            try:
                return frozenset(items)
            except TypeError:
                return items  # unhashable items cannot form a set; strict Pydantic rejects the list
        return self._item(f, raw)

    def _item(self, f: FieldSpec, raw: Any) -> Any:
        if f.kind is Kind.EMBEDDED:
            assert f.target is not None
            return self.instantiate(f.target, raw) if isinstance(raw, Mapping) else raw
        if f.kind is Kind.REF:
            return Ref(raw) if isinstance(raw, str) else raw
        if f.kind is Kind.ENUM:
            assert f.enum is not None
            if isinstance(raw, str) and raw in {m.value for m in f.enum}:
                return f.enum(raw)
            return raw
        return _scalar(f.scalar, raw)


def _scalar(scalar: type | None, raw: Any) -> Any:
    if scalar is date and isinstance(raw, str) and _ISO_DATE.match(raw):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return raw
    if scalar is datetime and isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return raw
    if scalar is Decimal and (isinstance(raw, str) or (isinstance(raw, int) and not isinstance(raw, bool))):
        try:
            return Decimal(raw)
        except InvalidOperation:
            return raw
    return raw


def dump_record(spec: SchemaSpec, instance: BaseModel) -> dict[str, Any]:
    """The JSON form of a valid instance, with set fields sorted so files are deterministic."""
    c = spec.of(type(instance))
    out: dict[str, Any] = {}
    data = instance.model_dump(mode="json", exclude_defaults=True)
    for f in c.fields:
        if f.name not in data:
            continue
        value = getattr(instance, f.name)
        if f.kind is Kind.EMBEDDED:
            if f.is_collection:
                items = [dump_record(spec, item) for item in value]
                out[f.name] = sorted(items, key=_json_key) if f.card is Card.SET else items
            else:
                out[f.name] = dump_record(spec, value)
        elif f.card is Card.SET:
            out[f.name] = sorted(data[f.name], key=_json_key)
        else:
            out[f.name] = data[f.name]
    return out


def _json_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True)
