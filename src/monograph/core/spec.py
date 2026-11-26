"""Analyze the Pydantic models into a small intermediate representation (SchemaSpec).

This is the only module that reads Pydantic internals (`model_fields`, `FieldInfo.metadata`). Every
generator, the instance mapping and the test strategies consume `SchemaSpec`. Anything this module
does not understand raises `SchemaError`; there are no silent defaults.
"""

from __future__ import annotations

import types
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Union, get_args, get_origin

import annotated_types as at
from pydantic import AwareDatetime, BaseModel
from pydantic.fields import FieldInfo

from monograph.core.meta import OntClass, OntEnum, OntProp
from monograph.core.refs import Ref
from monograph.core.rules import PYDANTIC_ONLY_MARK, AtLeast, PydanticOnly, RequiredWhen, Rule


class SchemaError(Exception):
    """The source models cannot be projected. The message says which class and field, and why."""


class Card(StrEnum):
    ONE = "one"  # T: exactly one
    OPTIONAL = "optional"  # T | None = None: at most one
    LIST = "list"  # list[T]: ordered, position-identified
    SET = "set"  # frozenset[T]: unordered, key-identified


class Kind(StrEnum):
    SCALAR = "scalar"
    ENUM = "enum"
    REF = "ref"
    EMBEDDED = "embedded"


SCALAR_TYPES: tuple[type, ...] = (str, int, Decimal, bool, date, datetime)
Bound = int | Decimal


@dataclass(frozen=True, slots=True)
class Constraints:
    pattern: str | None = None
    min_length: int | None = None
    max_length: int | None = None
    ge: Bound | None = None
    le: Bound | None = None
    gt: Bound | None = None
    lt: Bound | None = None


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    prop: OntProp
    kind: Kind
    card: Card
    min_count: int
    max_count: int | None
    scalar: type | None
    enum: type[StrEnum] | None
    target: type[BaseModel] | None
    constraints: Constraints
    description: str | None

    @property
    def is_collection(self) -> bool:
        return self.card in (Card.LIST, Card.SET)


@dataclass(frozen=True, slots=True)
class ClassSpec:
    cls: type[BaseModel]
    meta: OntClass
    doc: str
    parent: type[BaseModel] | None
    fields: tuple[FieldSpec, ...]
    rules: tuple[Rule, ...]
    pydantic_only: tuple[PydanticOnly, ...]

    @property
    def is_entity(self) -> bool:
        return self.meta.path is not None

    @property
    def is_embedded(self) -> bool:
        return self.meta.path is None and not self.meta.abstract

    def field(self, name: str) -> FieldSpec:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(f"{self.cls.__name__} has no field {name!r}")

    def find(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)


@dataclass(frozen=True, slots=True)
class EnumSpec:
    cls: type[StrEnum]
    meta: OntEnum
    doc: str
    members: tuple[tuple[str, str], ...]  # (python name, value)


@dataclass(frozen=True, slots=True)
class ConstraintSite:
    """One constraint on one field (or one rule), used for fixture coverage and leak inventories."""

    cls: str
    field: str | None
    kind: str
    detail: str = ""


@dataclass(frozen=True)
class SchemaSpec:
    version: str
    classes: tuple[ClassSpec, ...]
    enums: tuple[EnumSpec, ...]
    _by_cls: dict[type[BaseModel], ClassSpec] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._by_cls.update({c.cls: c for c in self.classes})

    def of(self, cls: type[BaseModel]) -> ClassSpec:
        try:
            return self._by_cls[cls]
        except KeyError:
            raise SchemaError(f"{cls.__name__} is not part of the schema") from None

    def by_name(self, name: str) -> ClassSpec:
        for c in self.classes:
            if name in (c.cls.__name__, c.meta.name):
                return c
        raise KeyError(name)

    def entities(self) -> tuple[ClassSpec, ...]:
        return tuple(c for c in self.classes if c.is_entity)

    def concrete(self) -> tuple[ClassSpec, ...]:
        return tuple(c for c in self.classes if not c.meta.abstract)

    def constraint_sites(self) -> Iterator[ConstraintSite]:
        """Every constraint in the schema, by kind. Kinds match the invalid-fixture vocabulary."""
        for c in self.concrete():
            name = c.cls.__name__
            yield ConstraintSite(name, None, "closed")
            for f in c.fields:
                if f.min_count >= 1:
                    yield ConstraintSite(name, f.name, "required" if not f.is_collection else "min_count")
                if f.is_collection and f.max_count is not None:
                    yield ConstraintSite(name, f.name, "max_count")
                yield ConstraintSite(name, f.name, _type_kind(f))
                k = f.constraints
                for kind, value in (
                    ("pattern", k.pattern),
                    ("min_length", k.min_length),
                    ("max_length", k.max_length),
                    ("min_inclusive", k.ge),
                    ("max_inclusive", k.le),
                    ("min_exclusive", k.gt),
                    ("max_exclusive", k.lt),
                ):
                    if value is not None:
                        yield ConstraintSite(name, f.name, kind, str(value))
            for r in c.rules:
                yield ConstraintSite(name, None, f"rule:{r.id}")
            for p in c.pydantic_only:
                yield ConstraintSite(name, None, f"pydantic_only:{p.id}")


def _type_kind(f: FieldSpec) -> str:
    return {Kind.SCALAR: "datatype", Kind.ENUM: "enum", Kind.REF: "ref", Kind.EMBEDDED: "node"}[f.kind]


# ---------------------------------------------------------------------------------------------------


def analyze(models: Sequence[type[BaseModel]], version: str) -> SchemaSpec:
    classes = tuple(_analyze_class(cls, models) for cls in models)
    names = [c.meta.name for c in classes]
    if len(names) != len(set(names)):
        raise SchemaError(f"duplicate class names in ontology: {sorted(names)}")
    known = {c.cls for c in classes}
    enums: dict[type[StrEnum], EnumSpec] = {}
    for c in classes:
        for f in c.fields:
            if f.target is not None and f.target not in known:
                raise SchemaError(f"{c.cls.__name__}.{f.name}: {f.target.__name__} is not listed in MODELS")
            if f.kind is Kind.REF and f.target is not None and not _meta(f.target).path:
                raise SchemaError(f"{c.cls.__name__}.{f.name}: Ref target must be an entity with a path")
            if f.kind is Kind.EMBEDDED and f.target is not None and _meta(f.target).abstract:
                raise SchemaError(f"{c.cls.__name__}.{f.name}: abstract class as a field type; use Ref[T]")
            if f.kind is Kind.EMBEDDED and f.target is not None and _meta(f.target).path:
                raise SchemaError(f"{c.cls.__name__}.{f.name}: entities must be referenced with Ref[T]")
            if f.enum is not None and f.enum not in enums:
                enums[f.enum] = _analyze_enum(f.enum)
        _check_rules(c)
    _check_property_consistency(classes)
    return SchemaSpec(version=version, classes=classes, enums=tuple(enums.values()))


def _meta(cls: type[BaseModel]) -> OntClass:
    meta = vars(cls).get("__ont__")
    if not isinstance(meta, OntClass):
        raise SchemaError(f"{cls.__name__}: missing `__ont__: ClassVar[OntClass]` (unannotated class)")
    return meta


def _analyze_class(cls: type[BaseModel], models: Sequence[type[BaseModel]]) -> ClassSpec:
    meta = _meta(cls)
    doc = (cls.__doc__ or "").strip()
    if not doc:
        raise SchemaError(f"{cls.__name__}: a docstring is required (it becomes rdfs:comment)")
    parent = next((b for b in cls.__mro__[1:] if isinstance(vars(b).get("__ont__"), OntClass)), None)
    if parent is not None and parent not in models:
        raise SchemaError(f"{cls.__name__}: parent {parent.__name__} is not listed in MODELS")
    fields = tuple(_analyze_field(cls, name, info) for name, info in cls.model_fields.items())
    if meta.abstract and fields:
        raise SchemaError(f"{cls.__name__}: abstract classes cannot declare fields")
    spec_fields = {f.name: f for f in fields}
    for key in meta.key:
        kf = spec_fields.get(key)
        if kf is None or kf.card is not Card.ONE or kf.kind is not Kind.SCALAR or kf.scalar not in (str, int):
            raise SchemaError(f"{cls.__name__}: key field {key!r} must be an exactly-one str or int field")
    if meta.path is not None and not meta.key:
        raise SchemaError(f"{cls.__name__}: an entity needs at least one key field")
    rules = tuple(vars(cls).get("__rules__", ()))
    declared = tuple(
        getattr(d.func, PYDANTIC_ONLY_MARK)
        for d in cls.__pydantic_decorators__.model_validators.values()
        if hasattr(d.func, PYDANTIC_ONLY_MARK) and d.cls_var_name in vars(cls)
    )
    return ClassSpec(cls, meta, doc, parent, fields, rules, declared)


def _analyze_field(cls: type[BaseModel], name: str, info: FieldInfo) -> FieldSpec:
    where = f"{cls.__name__}.{name}"
    props = [m for m in info.metadata if isinstance(m, OntProp)]
    if len(props) != 1:
        raise SchemaError(f"{where}: needs exactly one OntProp annotation, found {len(props)}")
    constraints, min_len, max_len = _constraints(where, info.metadata)

    annotation: Any = info.annotation
    card = Card.ONE
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) != 1 or len(get_args(annotation)) != 2:
            raise SchemaError(f"{where}: only `T | None` unions are supported")
        card, annotation = Card.OPTIONAL, args[0]
    elif origin is list:
        card, annotation = Card.LIST, get_args(annotation)[0]
    elif origin is frozenset:
        card, annotation = Card.SET, get_args(annotation)[0]
    elif origin in (set, tuple, dict) or annotation in (list, set, frozenset, tuple, dict):
        raise SchemaError(f"{where}: use list[T] (ordered) or frozenset[T] (keyed); {annotation} is not supported")

    kind, scalar, enum, target = _inner_kind(where, annotation)
    _check_default(where, info, card, min_len)

    if card in (Card.LIST, Card.SET):
        if kind is Kind.SCALAR:
            raise SchemaError(f"{where}: collections of plain scalars are not supported in v0.1.0")
        if kind is Kind.EMBEDDED and target is not None:
            keyed = bool(_meta(target).key)
            if card is Card.SET and not keyed:
                raise SchemaError(f"{where}: embedded values in a frozenset need an OntClass key")
            if card is Card.LIST and keyed:
                raise SchemaError(f"{where}: keyed embedded values belong in a frozenset, not a list")
        min_count, max_count = min_len or 0, max_len
    else:
        if min_len is not None or max_len is not None:
            if scalar is not str:
                raise SchemaError(f"{where}: min_length/max_length apply to str or collections only")
            constraints = _replace(constraints, min_length=min_len, max_length=max_len)
        min_count, max_count = (1 if card is Card.ONE else 0), 1

    if constraints.pattern is not None and scalar is not str:
        raise SchemaError(f"{where}: pattern applies to str fields only")
    bounds = (constraints.ge, constraints.le, constraints.gt, constraints.lt)
    if any(b is not None for b in bounds) and (scalar not in (int, Decimal) or card in (Card.LIST, Card.SET)):
        raise SchemaError(f"{where}: numeric bounds apply to single int or Decimal fields only")

    return FieldSpec(
        name=name,
        prop=props[0],
        kind=kind,
        card=card,
        min_count=min_count,
        max_count=max_count,
        scalar=scalar,
        enum=enum,
        target=target,
        constraints=constraints,
        description=info.description,
    )


def _replace(c: Constraints, **changes: Any) -> Constraints:
    values = {name: getattr(c, name) for name in Constraints.__slots__}
    values.update(changes)
    return Constraints(**values)


def _constraints(where: str, metadata: list[Any]) -> tuple[Constraints, int | None, int | None]:
    pattern: str | None = None
    min_len: int | None = None
    max_len: int | None = None
    bounds: dict[str, Bound] = {}
    for m in metadata:
        if isinstance(m, OntProp):
            continue
        if isinstance(m, at.Ge):
            bounds["ge"] = _bound(where, m.ge)
        elif isinstance(m, at.Le):
            bounds["le"] = _bound(where, m.le)
        elif isinstance(m, at.Gt):
            bounds["gt"] = _bound(where, m.gt)
        elif isinstance(m, at.Lt):
            bounds["lt"] = _bound(where, m.lt)
        elif isinstance(m, at.MinLen):
            min_len = m.min_length
        elif isinstance(m, at.MaxLen):
            max_len = m.max_length
        elif type(m).__name__ == "_PydanticGeneralMetadata" and set(vars(m)) == {"pattern"}:
            pattern = str(vars(m)["pattern"])
        elif hasattr(m, "func") and hasattr(m.func, PYDANTIC_ONLY_MARK):
            continue  # a declared pydantic_only functional validator; the lint step inventories it
        else:
            raise SchemaError(f"{where}: constraint {m!r} has no projection (supported: pattern, lengths, bounds)")
    constraints = Constraints(pattern=pattern, **bounds)  # type: ignore[arg-type]
    return constraints, min_len, max_len


def _bound(where: str, value: object) -> Bound:
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise SchemaError(f"{where}: bounds must be int or Decimal, got {value!r}")
    return value


def _inner_kind(where: str, annotation: Any) -> tuple[Kind, type | None, type[StrEnum] | None, type[BaseModel] | None]:
    if get_origin(annotation) is Ref:
        target = get_args(annotation)[0]
        if not (isinstance(target, type) and issubclass(target, BaseModel)):
            raise SchemaError(f"{where}: Ref target must be a model class")
        return Kind.REF, None, None, target
    if annotation is AwareDatetime:
        return Kind.SCALAR, datetime, None, None
    if not isinstance(annotation, type):
        raise SchemaError(f"{where}: unsupported annotation {annotation!r}")
    if issubclass(annotation, StrEnum):
        return Kind.ENUM, None, annotation, None
    if issubclass(annotation, BaseModel):
        return Kind.EMBEDDED, None, None, annotation
    if annotation is datetime:
        raise SchemaError(f"{where}: use AwareDatetime; naive datetimes have no xsd:dateTime projection")
    if annotation is float:
        raise SchemaError(f"{where}: float is not allowed; use Decimal")
    if annotation in SCALAR_TYPES:
        return Kind.SCALAR, annotation, None, None
    raise SchemaError(f"{where}: unsupported type {annotation.__name__}")


def _check_default(where: str, info: FieldInfo, card: Card, min_len: int | None) -> None:
    required = info.is_required()
    if card is Card.ONE and not required:
        raise SchemaError(f"{where}: an exactly-one field cannot have a default (RDF cannot tell it apart)")
    if card is Card.OPTIONAL and (required or info.default is not None):
        raise SchemaError(f"{where}: `T | None` fields must default to None")
    if card in (Card.LIST, Card.SET):
        if (min_len or 0) >= 1:
            if not required:
                raise SchemaError(f"{where}: a collection with min_length >= 1 cannot have a default")
        else:
            if required:
                raise SchemaError(f"{where}: a collection with min_length 0 must default to empty")
            factory: Any = info.default_factory
            default = factory() if factory is not None else info.default
            if default not in ([], frozenset()):
                raise SchemaError(f"{where}: an optional collection must default to empty, not {default!r}")


def _analyze_enum(enum: type[StrEnum]) -> EnumSpec:
    meta = vars(enum).get("__ont__")
    if not isinstance(meta, OntEnum):
        raise SchemaError(f"{enum.__name__}: missing `__ont__ = OntEnum(...)` (unannotated enum)")
    doc = (enum.__doc__ or "").strip()
    if not doc:
        raise SchemaError(f"{enum.__name__}: a docstring is required")
    return EnumSpec(enum, meta, doc, tuple((m.name, m.value) for m in enum))


def _check_rules(c: ClassSpec) -> None:
    where = c.cls.__name__
    for rule in c.rules:
        if isinstance(rule, RequiredWhen):
            when, then = c.find(rule.when), c.find(rule.then)
            if when is None or then is None:
                raise SchemaError(f"{where}: rule {rule.id} names an unknown field")
            if when.is_collection or when.kind not in (Kind.SCALAR, Kind.ENUM):
                raise SchemaError(f"{where}: rule {rule.id}: `when` must be a single scalar or enum field")
            _check_value(where, rule.id, when, rule.equals)
            if then.card is Card.ONE:
                raise SchemaError(f"{where}: rule {rule.id}: `then` is already required")
        elif isinstance(rule, AtLeast):
            parent = c.find(rule.field)
            if parent is None or parent.card is not Card.LIST or parent.kind is not Kind.EMBEDDED:
                raise SchemaError(f"{where}: rule {rule.id}: `field` must be a list of embedded values")
            assert parent.target is not None
            child = ClassSpecLookup.field(parent.target, rule.where)
            if child is None or child.is_collection or child.kind not in (Kind.SCALAR, Kind.ENUM):
                raise SchemaError(f"{where}: rule {rule.id}: `where` must be a single scalar or enum field")
            _check_value(where, rule.id, child, rule.equals)
            if rule.min_count < 1:
                raise SchemaError(f"{where}: rule {rule.id}: min_count must be at least 1")
        ids = [r.id for r in c.rules]
        if len(ids) != len(set(ids)):
            raise SchemaError(f"{where}: duplicate rule ids")


class ClassSpecLookup:
    """Field lookup on a model class without a SchemaSpec (used while the spec is being built)."""

    @staticmethod
    def field(model: type[BaseModel], name: str) -> FieldSpec | None:
        info = model.model_fields.get(name)
        return None if info is None else _analyze_field(model, name, info)


def _check_value(where: str, rule_id: str, f: FieldSpec, value: object) -> None:
    if f.kind is Kind.ENUM:
        assert f.enum is not None
        ok = isinstance(value, f.enum)
    else:
        ok = type(value) is f.scalar
    if not ok:
        raise SchemaError(f"{where}: rule {rule_id}: value {value!r} does not match the type of {f.name}")


def _check_property_consistency(classes: Sequence[ClassSpec]) -> None:
    seen: dict[str, tuple[str, FieldSpec]] = {}
    for c in classes:
        for f in c.fields:
            signature = (f.kind, f.scalar, f.enum, f.target)
            if f.prop.name in seen:
                other_cls, other = seen[f.prop.name]
                if (other.kind, other.scalar, other.enum, other.target) != signature:
                    raise SchemaError(
                        f"property {f.prop.name!r} is used by {other_cls}.{other.name} and "
                        f"{c.cls.__name__}.{f.name} with different ranges"
                    )
                if other.prop.label != f.prop.label:
                    raise SchemaError(f"property {f.prop.name!r} has two different labels")
            else:
                seen[f.prop.name] = (c.cls.__name__, f)
