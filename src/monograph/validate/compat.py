"""Breaking-change check: compare the generated OWL and SHACL with the last released snapshot.

The comparison is semantic, not textual. Both graphs are reduced to a signature (classes, properties,
individuals, and every property-shape constraint), the signatures are diffed, and each difference is
classified:

* breaking: anything that can make data valid under the release invalid now, or that removes or
  renames vocabulary (class, property, individual, shape property, enum member), tightens a bound,
  raises a minimum, lowers a maximum, changes a datatype, class, node or pattern, adds a required
  property or adds a rule;
* every constraint the SHACL generator can emit is in the signature, including sh:not (an added
  sh:not is a tightening);
* additive: new vocabulary, loosened bounds, removed rules, new enum members, annotation changes.

A pattern change is always breaking, because whether one regex accepts a superset of another cannot
be decided in general. A breaking change needs a major SCHEMA_VERSION bump; any change needs a bump.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, URIRef
from rdflib.collection import Collection
from rdflib.term import Node

from monograph.gen.shacl import SH

_LOWER_BOUNDS = ("minCount", "minLength", "minInclusive", "minExclusive")
_UPPER_BOUNDS = ("maxCount", "maxLength", "maxInclusive", "maxExclusive")
_EXACT = ("datatype", "class", "node", "nodeKind", "pattern")


@dataclass(frozen=True, slots=True)
class Change:
    kind: str
    subject: str
    detail: str
    breaking: bool


@dataclass
class SchemaSignature:
    classes: set[str] = field(default_factory=set)
    annotations: dict[str, str] = field(default_factory=dict)
    properties: dict[str, tuple[str, str, bool]] = field(default_factory=dict)
    individuals: dict[str, frozenset[str]] = field(default_factory=dict)
    shape_props: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    closed: dict[str, bool] = field(default_factory=dict)
    rules: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_graphs(cls, owl: Graph, shacl: Graph) -> SchemaSignature:
        sig = cls()
        for c in owl.subjects(RDF.type, OWL.Class):
            if isinstance(c, URIRef):
                sig.classes.add(str(c))
        for s, p, o in owl:
            if isinstance(s, URIRef) and p in (RDFS.label, RDFS.comment):
                sig.annotations[f"{s} {p}"] = str(o)
        for kind in (OWL.DatatypeProperty, OWL.ObjectProperty):
            for prop in owl.subjects(RDF.type, kind):
                functional = (prop, RDF.type, OWL.FunctionalProperty) in owl
                sig.properties[str(prop)] = (str(kind), str(owl.value(prop, RDFS.range)), functional)
        for ind in owl.subjects(RDF.type, OWL.NamedIndividual):
            types = frozenset(str(t) for t in owl.objects(ind, RDF.type) if t != OWL.NamedIndividual)
            sig.individuals[str(ind)] = types
        for shape in shacl.subjects(RDF.type, SH.NodeShape):
            target = shacl.value(shape, SH.targetClass)
            if target is None:
                continue
            if (shape, SH.closed, None) in shacl:
                sig.closed[str(target)] = shacl.value(shape, SH.closed) == Literal(True)
                for ps in shacl.objects(shape, SH.property):
                    path = str(shacl.value(ps, SH.path))
                    sig.shape_props[(str(target), path)] = _constraints(shacl, ps)
            elif "/rule-" in str(shape) or "#rule-" in str(shape):
                sig.rules[str(shape)] = _canonical(shacl, shape)
        return sig


def _constraints(g: Graph, ps: Node) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in (*_LOWER_BOUNDS, *_UPPER_BOUNDS, *_EXACT):
        value = g.value(ps, SH[name])
        if value is not None:
            out[name] = value.toPython() if isinstance(value, Literal) else str(value)
    members = g.value(ps, SH["in"])
    if members is not None:
        out["in"] = frozenset(str(m) for m in Collection(g, members))
    negated = frozenset(_canonical(g, n) for n in g.objects(ps, SH["not"]))
    if negated:
        out["not"] = negated
    return out


def _canonical(g: Graph, node: Node) -> str:
    if isinstance(node, BNode):
        if (node, RDF.first, None) in g:
            return "(" + " ".join(_canonical(g, m) for m in Collection(g, node)) + ")"
        parts = sorted(f"{p} {_canonical(g, o)}" for p, o in g.predicate_objects(node) if p != RDFS.label)
        return "[" + "; ".join(parts) + "]"
    if isinstance(node, URIRef) and (node, None, None) in g and node != SH.IRI:
        parts = sorted(
            f"{p} {_canonical(g, o)}" for p, o in g.predicate_objects(node) if p not in (RDFS.label, SH.message)
        )
        return "{" + "; ".join(parts) + "}"
    return node.n3()


def _num(value: Any, default: Decimal) -> Decimal:
    if value is None:
        return default
    return Decimal(str(value))


def diff(old: SchemaSignature, new: SchemaSignature) -> list[Change]:
    changes: list[Change] = []

    def add(kind: str, subject: str, detail: str, breaking: bool) -> None:
        changes.append(Change(kind, subject, detail, breaking))

    for c in sorted(old.classes - new.classes):
        add("class", c, "class removed (or its IRI changed)", True)
    for c in sorted(new.classes - old.classes):
        add("class", c, "class added", False)
    for key in sorted(set(old.annotations) | set(new.annotations)):
        if old.annotations.get(key) != new.annotations.get(key):
            add("annotation", key, "label or comment changed", False)

    for p in sorted(set(old.properties) | set(new.properties)):
        o, n = old.properties.get(p), new.properties.get(p)
        if n is None:
            add("property", p, "property removed (or renamed)", True)
        elif o is None:
            add("property", p, "property added", False)
        else:
            if o[:2] != n[:2]:
                add("property", p, f"kind or range changed from {o[:2]} to {n[:2]}", True)
            if o[2] != n[2]:
                add("property", p, "became functional" if n[2] else "no longer functional", n[2])

    for i in sorted(set(old.individuals) | set(new.individuals)):
        o_types, n_types = old.individuals.get(i), new.individuals.get(i)
        if n_types is None:
            add("individual", i, "enum member removed", True)
        elif o_types is None:
            add("individual", i, "enum member added", False)
        elif o_types != n_types:
            add("individual", i, "enum member moved to another enumeration", True)

    for target in sorted(set(old.closed) | set(new.closed)):
        o_closed, n_closed = old.closed.get(target), new.closed.get(target)
        if o_closed is not None and n_closed is not None and o_closed != n_closed:
            add("shape", target, "shape became closed" if n_closed else "shape opened", bool(n_closed))

    for site in sorted(set(old.shape_props) | set(new.shape_props)):
        subject = f"{site[0]} {site[1]}"
        o_c, n_c = old.shape_props.get(site), new.shape_props.get(site)
        if n_c is None:
            if site[0] in new.closed:
                add("shape", subject, "property removed from a closed shape", True)
            continue
        if o_c is None:
            required = int(n_c.get("minCount", 0)) >= 1
            add("shape", subject, "required property added" if required else "optional property added", required)
            continue
        for name in _LOWER_BOUNDS:
            before, after = _num(o_c.get(name), Decimal("-Infinity")), _num(n_c.get(name), Decimal("-Infinity"))
            if before != after:
                add("constraint", subject, f"{name} {o_c.get(name)} -> {n_c.get(name)}", after > before)
        for name in _UPPER_BOUNDS:
            before, after = _num(o_c.get(name), Decimal("Infinity")), _num(n_c.get(name), Decimal("Infinity"))
            if before != after:
                add("constraint", subject, f"{name} {o_c.get(name)} -> {n_c.get(name)}", after < before)
        for name in _EXACT:
            if o_c.get(name) != n_c.get(name):
                add("constraint", subject, f"{name} {o_c.get(name)!r} -> {n_c.get(name)!r}", True)
        o_not, n_not = o_c.get("not", frozenset()), n_c.get("not", frozenset())
        for shape in sorted(n_not - o_not):
            add("constraint", subject, f"sh:not added {shape}", True)
        for shape in sorted(o_not - n_not):
            add("constraint", subject, f"sh:not removed {shape}", False)
        o_in, n_in = o_c.get("in", frozenset()), n_c.get("in", frozenset())
        for member in sorted(o_in - n_in):
            add("constraint", subject, f"sh:in lost {member}", True)
        for member in sorted(n_in - o_in):
            add("constraint", subject, f"sh:in gained {member}", False)

    for rule_iri in sorted(set(old.rules) | set(new.rules)):
        o_r, n_r = old.rules.get(rule_iri), new.rules.get(rule_iri)
        if n_r is None:
            add("rule", rule_iri, "rule removed", False)
        elif o_r is None:
            add("rule", rule_iri, "rule added", True)
        elif o_r != n_r:
            add("rule", rule_iri, "rule changed", True)
    return changes


_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def parse_version(text: str) -> tuple[int, int, int]:
    match = _SEMVER.match(text)
    if not match:
        raise ValueError(f"{text!r} is not a semantic version")
    major, minor, patch = (int(g) for g in match.groups())
    return major, minor, patch


@dataclass(frozen=True, slots=True)
class CompatReport:
    release: str | None
    current: str
    changes: tuple[Change, ...]
    problem: str | None

    @property
    def ok(self) -> bool:
        return self.problem is None

    @property
    def breaking(self) -> tuple[Change, ...]:
        return tuple(c for c in self.changes if c.breaking)


class CompatChecker:
    def __init__(self, releases_dir: Path, owl_file: str, shacl_file: str) -> None:
        self._releases = releases_dir
        self._owl_file = owl_file
        self._shacl_file = shacl_file

    def last_release(self) -> str | None:
        versions = [p.name for p in self._releases.iterdir() if p.is_dir() and _SEMVER.match(p.name)]
        return max(versions, key=parse_version) if versions else None

    def check(self, current: str, owl_text: str, shacl_text: str) -> CompatReport:
        release = self.last_release() if self._releases.exists() else None
        if release is None:
            return CompatReport(None, current, (), None)
        directory = self._releases / release
        old = SchemaSignature.from_graphs(
            Graph().parse(directory / self._owl_file, format="turtle"),
            Graph().parse(directory / self._shacl_file, format="turtle"),
        )
        new = SchemaSignature.from_graphs(
            Graph().parse(data=owl_text, format="turtle"), Graph().parse(data=shacl_text, format="turtle")
        )
        changes = tuple(diff(old, new))
        cur, rel = parse_version(current), parse_version(release)
        problem = None
        if cur < rel:
            problem = f"SCHEMA_VERSION {current} is older than the last release {release}"
        elif any(c.breaking for c in changes) and cur[0] == rel[0]:
            problem = f"breaking changes need a major version bump from {release} (SCHEMA_VERSION is {current})"
        elif changes and cur == rel:
            problem = f"the schema changed but SCHEMA_VERSION is still {current}"
        return CompatReport(release, current, changes, problem)
