"""Hypothesis strategies derived from the SchemaSpec, and single-constraint perturbations.

`RecordStrategies.record(cls)` draws JSON-shaped records that satisfy every field constraint.
`valid_record(cls)` additionally filters by the Pydantic verdict (rules and pydantic_only validators).
`Perturber` lists every constraint site of a class (recursing into embedded values) and breaks exactly
one of them. The property tests assert that Pydantic and SHACL agree on the result.

Strings never contain a newline. The trailing-newline divergence between dialects is measured by the
dialect probes instead, so that it shows up as one named finding rather than as random noise.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from hypothesis import strategies as st
from pydantic import BaseModel

from monograph.core.refs import IRI_PATTERN
from monograph.core.rules import AtLeast, RequiredWhen
from monograph.core.spec import Card, ClassSpec, FieldSpec, Kind, SchemaSpec

Record = dict[str, Any]
Path = tuple[str | int, ...]

_TEXT = st.characters(codec="utf-8", exclude_categories=["Cs", "Cc"])
_DECIMAL_LIMIT = Decimal("1e9")
_INT_LIMIT = 10**9


def _json_value(value: Any) -> Any:
    if isinstance(value, date | Decimal):
        return value.isoformat() if isinstance(value, date) else str(value)
    return value


class RecordStrategies:
    def __init__(self, spec: SchemaSpec, base: str, today: date, is_valid: Callable[[type[BaseModel], Record], bool]):
        self._spec = spec
        self._base = base
        self._today = today
        self._is_valid = is_valid

    def valid_record(self, cls: type[BaseModel]) -> st.SearchStrategy[Record]:
        return self.record(cls).filter(lambda r: self._is_valid(cls, r))

    def record(self, cls: type[BaseModel]) -> st.SearchStrategy[Record]:
        c = self._spec.of(cls)
        required = {f.name: self.field(f) for f in c.fields if f.min_count >= 1}
        optional = {f.name: self.field(f) for f in c.fields if f.min_count == 0}
        return st.fixed_dictionaries(required, optional=optional)

    def field(self, f: FieldSpec) -> st.SearchStrategy[Any]:
        item = self.item(f)
        if f.card is Card.OPTIONAL:
            return st.one_of(st.none(), item)
        if f.card in (Card.LIST, Card.SET):
            max_size = f.max_count if f.max_count is not None else f.min_count + 3
            unique_by: Callable[[Any], Any] | None = None
            if f.card is Card.SET:
                unique_by = self._set_key(f)
            return st.lists(item, min_size=f.min_count, max_size=max_size, unique_by=unique_by)
        return item

    def _set_key(self, f: FieldSpec) -> Callable[[Any], Any]:
        if f.kind is Kind.EMBEDDED:
            assert f.target is not None
            keys = self._spec.of(f.target).meta.key
            return lambda d: tuple(d[k] for k in keys)
        return lambda v: v

    def item(self, f: FieldSpec) -> st.SearchStrategy[Any]:
        if f.kind is Kind.EMBEDDED:
            assert f.target is not None
            return self.record(f.target)
        if f.kind is Kind.ENUM:
            assert f.enum is not None
            return st.sampled_from([m.value for m in f.enum])
        if f.kind is Kind.REF:
            assert f.target is not None
            return self.iri(self._spec.of(f.target))
        return self.scalar(f).map(_json_value)

    def iri(self, target: ClassSpec) -> st.SearchStrategy[str]:
        parts = [self.scalar(target.field(k)) for k in target.meta.key]
        return st.tuples(*parts).map(lambda ps: f"{self._base}{target.meta.path}/" + "/".join(str(p) for p in ps))

    def scalar(self, f: FieldSpec) -> st.SearchStrategy[Any]:
        k = f.constraints
        if f.scalar is str:
            low, high = k.min_length or 0, k.max_length
            if k.pattern is not None:
                s = st.from_regex(k.pattern, fullmatch=True).filter(lambda v: "\n" not in v)
                return s.filter(lambda v: low <= len(v) and (high is None or len(v) <= high))
            return st.text(_TEXT, min_size=low, max_size=min(high or 60, 60))
        if f.scalar is bool:
            return st.booleans()
        if f.scalar is int:
            low = int(k.ge) if k.ge is not None else (int(k.gt) + 1 if k.gt is not None else -_INT_LIMIT)
            high = int(k.le) if k.le is not None else (int(k.lt) - 1 if k.lt is not None else _INT_LIMIT)
            return st.integers(low, high)
        if f.scalar is Decimal:
            dlow = Decimal(k.ge if k.ge is not None else (k.gt if k.gt is not None else -_DECIMAL_LIMIT))
            dhigh = Decimal(k.le if k.le is not None else (k.lt if k.lt is not None else _DECIMAL_LIMIT))
            decimals = st.decimals(dlow, dhigh, allow_nan=False, allow_infinity=False, places=3)
            return decimals.filter(lambda d: (k.gt is None or d > k.gt) and (k.lt is None or d < k.lt))
        if f.scalar is date:
            return st.dates(min_value=date(1900, 1, 1), max_value=self._today)
        raise NotImplementedError(f"no strategy for {f.scalar}")


# ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Site:
    """One constraint that a perturbation can break."""

    kind: str
    path: Path
    field: FieldSpec | None = None
    rule: RequiredWhen | AtLeast | None = None

    def label(self) -> str:
        return f"{self.kind}@{'.'.join(str(p) for p in self.path)}"


class Perturber:
    def __init__(self, spec: SchemaSpec) -> None:
        self._spec = spec

    def sites(self, cls: type[BaseModel], prefix: Path = ()) -> list[Site]:
        c = self._spec.of(cls)
        out = [Site("closed", prefix)]
        for f in c.fields:
            p = (*prefix, f.name)
            if f.card is Card.ONE:
                out.append(Site("required", p, f))
            out.append(Site("wrong_type", p, f))
            if f.is_collection:
                if f.min_count > 0:
                    out.append(Site("min_count", p, f))
                if f.max_count is not None:
                    out.append(Site("max_count", p, f))
                out.append(Site("item_wrong_type", p, f))
            k = f.constraints
            if k.pattern is not None:
                out.append(Site("pattern", p, f))
            if k.charset is not None:
                out.append(Site("charset", p, f))
            if k.min_length:
                out.append(Site("min_length", p, f))
            if k.max_length is not None:
                out.append(Site("max_length", p, f))
            for name in ("ge", "le", "gt", "lt"):
                if getattr(k, name) is not None:
                    out.append(Site(name, p, f))
            if f.kind is Kind.ENUM:
                out.append(Site("enum", p, f))
            if f.kind is Kind.REF:
                out.append(Site("ref", p, f))
            if f.kind is Kind.EMBEDDED:
                assert f.target is not None
                child = (*p, 0) if f.is_collection else p
                out += self.sites(f.target, child)
        for rule in c.rules:
            out.append(Site(f"rule:{rule.id}", prefix, rule=rule))
        return out

    def apply(self, draw: Callable[[st.SearchStrategy[Any]], Any], record: Record, site: Site) -> Record | None:
        """Return a copy of `record` with `site` broken, or None if the record has no such location."""
        out = copy.deepcopy(record)
        if site.rule is not None:
            return self._break_rule(out, site)
        if site.kind == "closed":
            target = out if not site.path else _get(out, site.path)
            if not isinstance(target, dict):
                return None
            target[draw(st.sampled_from(["zz_extra", "comment", "synonym"]))] = draw(st.sampled_from(["x", 1, True]))
            return out
        parent, key = _locate(out, site.path)
        if parent is None:
            return None
        assert site.field is not None
        assert isinstance(parent, dict)
        assert isinstance(key, str)
        f = site.field
        value = draw(self._value_for(site, f, parent.get(key)))
        if value is _REMOVE:
            parent.pop(key, None)
        else:
            parent[key] = value
        return out

    def _value_for(self, site: Site, f: FieldSpec, current: Any) -> st.SearchStrategy[Any]:
        k = f.constraints
        kind = site.kind
        if kind == "required":
            return st.just(_REMOVE)
        if kind == "wrong_type":
            return st.sampled_from(_wrong_types(f))
        if kind == "item_wrong_type":
            return st.sampled_from(_wrong_types(f, item=True)).map(lambda v: [v])
        if kind == "min_count":
            return st.lists(st.just(current[0]) if current else st.nothing(), max_size=f.min_count - 1)
        if kind == "max_count":
            assert f.max_count is not None
            extra = st.text(_TEXT, min_size=1, max_size=8)
            return st.lists(extra, min_size=f.max_count + 1, max_size=f.max_count + 2, unique=True)
        if kind == "charset":
            assert k.charset is not None
            forbidden = [chr(c) for lo, hi in k.charset.forbidden_ranges() for c in (lo, hi)]
            base = current if isinstance(current, str) and current else "x"
            return st.tuples(st.sampled_from(forbidden), st.integers(0, len(base))).map(
                lambda t: base[: t[1]] + t[0] + base[t[1] :]
            )
        if kind == "pattern":
            assert k.pattern is not None
            pattern = re.compile(k.pattern)
            return st.text(_TEXT, max_size=20).filter(lambda v: not pattern.search(v))
        if kind == "min_length":
            assert k.min_length is not None
            return st.text(_TEXT, max_size=k.min_length - 1)
        if kind == "max_length":
            assert k.max_length is not None
            return st.text(_TEXT, min_size=k.max_length + 1, max_size=k.max_length + 3)
        if kind in ("ge", "gt", "le", "lt"):
            bound = getattr(k, kind)
            step = 1 if f.scalar is int else Decimal("0.001")
            if kind == "ge":
                bad = bound - step
            elif kind == "le":
                bad = bound + step
            else:
                bad = bound
            return st.just(bad if f.scalar is int else str(Decimal(bad)))
        if kind == "enum":
            values = {m.value for m in f.enum} if f.enum else set()
            bad = st.text(_TEXT, max_size=12).filter(lambda v: v not in values)
            return bad.map(lambda v: [v]) if f.is_collection else bad
        if kind == "ref":
            iri = re.compile(IRI_PATTERN)
            bad = st.text(_TEXT, max_size=20).filter(lambda v: not iri.search(v))
            return bad.map(lambda v: [v]) if f.is_collection else bad
        raise ValueError(kind)

    def _break_rule(self, record: Record, site: Site) -> Record | None:
        rule = site.rule
        if isinstance(rule, RequiredWhen):
            for condition in rule.when:
                value = condition.values[0]
                record[condition.field] = value if not isinstance(value, str) else str(value)
            record.pop(rule.then, None)
            return record
        assert isinstance(rule, AtLeast)
        items = record.get(rule.field)
        if not isinstance(items, list) or not items:
            return None
        c = self._spec.of(self._owner(site))
        child_field = self._spec.of(_target(c.field(rule.field))).field(rule.where)
        allowed = {str(v) for v in rule.values}
        others = [m.value for m in child_field.enum or () if m.value not in allowed]
        for item in items:
            if isinstance(item, dict):
                item[rule.where] = others[0]
        return record

    def _owner(self, site: Site) -> type[BaseModel]:
        for c in self._spec.classes:
            if site.rule in c.rules:
                return c.cls
        raise KeyError(site.rule)


def _target(f: FieldSpec) -> type[BaseModel]:
    assert f.target is not None
    return f.target


class _Remove:
    def __repr__(self) -> str:
        return "<remove>"


_REMOVE = _Remove()


def _wrong_types(f: FieldSpec, item: bool = False) -> list[object]:
    if f.is_collection and not item:
        return ["not a list", 7]
    if f.kind is Kind.EMBEDDED:
        return ["text", 7, ["nested"]]
    if f.kind in (Kind.ENUM, Kind.REF):
        return [7, True, {"iri": "x"}]
    table: dict[type, list[object]] = {
        str: [42, True, 1.5],
        int: ["3", 3.5, True],
        Decimal: [1.5, True, "1,5"],
        bool: ["true", 1, 0],
        date: ["20240101", 20240101, "2024-02-30"],
    }
    return table.get(f.scalar or str, [None])


def _get(record: Any, path: Path) -> Any:
    node = record
    for p in path:
        if isinstance(p, int):
            if not isinstance(node, list) or len(node) <= p:
                return None
            node = node[p]
        else:
            if not isinstance(node, dict) or p not in node:
                return None
            node = node[p]
    return node


def _locate(record: Record, path: Path) -> tuple[Any, str | int | None]:
    if not path:
        return record, None
    parent = _get(record, path[:-1]) if len(path) > 1 else record
    key = path[-1]
    if not isinstance(parent, dict) or not isinstance(key, str):
        return None, None
    return parent, key
