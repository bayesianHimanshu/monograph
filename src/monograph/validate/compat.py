"""Breaking-change check: compare the generated OWL and SHACL with the released snapshots.

The comparison is semantic, not textual. Both graphs are reduced to a signature (classes and their
parents, properties with kind, range and domain, individuals, every property-shape constraint, closed
shapes and their ignored properties, rules), the signatures are diffed, and each difference is
classified:

* breaking: anything that can make data valid under the release invalid now, or that removes or
  renames vocabulary (class, property, individual, shape property, enum member), tightens a bound,
  raises a minimum, lowers a maximum, changes a datatype, class, node or pattern, adds a negated
  constraint (sh:not), adds a required property or adds a rule;
* additive: new vocabulary, loosened bounds, removed rules, new enum members, wider domains,
  annotation changes.

Fail-closed: every statement in either graph that the signature does not model explicitly is kept as
a residual, and any residual difference is breaking ("unclassified"). A construct the classifier was
not written for can therefore never slip through as "no change".

A pattern change is always breaking, because whether one regex accepts a superset of another cannot
be decided in general.

Release rules (CompatChecker):

1. SCHEMA_VERSION must have a snapshot in generated/releases/, and the snapshot must equal what the
   models generate now.
2. Against the previous release (the highest one below SCHEMA_VERSION), a breaking change needs a
   major bump.
3. Every consecutive pair of releases obeys rule 2 as well.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable, Iterable
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

_OWL_TYPES = {OWL.Class, OWL.NamedIndividual, OWL.DatatypeProperty, OWL.ObjectProperty, OWL.FunctionalProperty}
_ONTOLOGY_HEADER = {OWL.versionInfo, OWL.versionIRI}  # change with every version by design
_PROPERTY_SHAPE_HANDLED = {RDF.type, SH.path, SH.name, SH["in"], SH["not"]} | {
    SH[n] for n in (*_LOWER_BOUNDS, *_UPPER_BOUNDS, *_EXACT)
}
_NODE_SHAPE_HANDLED = {RDF.type, SH.targetClass, RDFS.label, SH.closed, SH.ignoredProperties, SH.property}

Add = Callable[[str, str, str, bool], None]


@dataclass(frozen=True, slots=True)
class Change:
    kind: str
    subject: str
    detail: str
    breaking: bool


@dataclass
class SchemaSignature:
    classes: set[str] = field(default_factory=set)
    parents: dict[str, frozenset[str]] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    properties: dict[str, tuple[str, str, bool]] = field(default_factory=dict)
    domains: dict[str, frozenset[str]] = field(default_factory=dict)
    individuals: dict[str, frozenset[str]] = field(default_factory=dict)
    shape_props: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    closed: dict[str, bool] = field(default_factory=dict)
    ignored: dict[str, frozenset[str]] = field(default_factory=dict)
    rules: dict[str, str] = field(default_factory=dict)
    rule_targets: dict[str, str] = field(default_factory=dict)
    residual: set[str] = field(default_factory=set)

    @classmethod
    def from_graphs(cls, owl: Graph, shacl: Graph) -> SchemaSignature:
        sig = cls()
        sig._read_owl(owl)
        sig._read_shacl(shacl)
        return sig

    def _read_owl(self, owl: Graph) -> None:
        individuals = set(owl.subjects(RDF.type, OWL.NamedIndividual))
        enum_classes = {o for s, o in owl.subject_objects(RDF.type) if s in individuals}
        for c in owl.subjects(RDF.type, OWL.Class):
            if isinstance(c, URIRef):
                self.classes.add(str(c))
                self.parents[str(c)] = frozenset(str(p) for p in owl.objects(c, RDFS.subClassOf))
        for kind in (OWL.DatatypeProperty, OWL.ObjectProperty):
            for prop in owl.subjects(RDF.type, kind):
                functional = (prop, RDF.type, OWL.FunctionalProperty) in owl
                self.properties[str(prop)] = (str(kind), str(owl.value(prop, RDFS.range)), functional)
                self.domains[str(prop)] = frozenset(_domain(owl, d) for d in owl.objects(prop, RDFS.domain))
        for ind in individuals:
            types = frozenset(str(t) for t in owl.objects(ind, RDF.type) if t != OWL.NamedIndividual)
            self.individuals[str(ind)] = types
        ontologies = set(owl.subjects(RDF.type, OWL.Ontology))
        for s, p, o in owl:
            if isinstance(s, BNode):
                continue  # reached through its parent (owl:unionOf in a domain), compared there
            if p in (RDFS.label, RDFS.comment):
                self.annotations[f"{s} {p}"] = str(o)
            elif not _owl_modelled(s, p, o, ontologies, enum_classes):
                self.residual.add(f"owl: {s} {p} {_canonical(owl, o)}")

    def _read_shacl(self, shacl: Graph) -> None:
        property_shapes: set[Node] = set()
        rule_shapes: set[Node] = set()
        node_shapes = set(shacl.subjects(RDF.type, SH.NodeShape))
        for shape in node_shapes:
            target = shacl.value(shape, SH.targetClass)
            if target is None:
                continue
            if "#rule-" in str(shape):
                rule_shapes.add(shape)
                self.rules[str(shape)] = _canonical(shacl, shape)
                self.rule_targets[str(shape)] = str(target)
                continue
            if (shape, SH.closed, None) in shacl:
                self.closed[str(target)] = shacl.value(shape, SH.closed) == Literal(True)
                ignored = shacl.value(shape, SH.ignoredProperties)
                if ignored is not None:
                    self.ignored[str(target)] = frozenset(str(m) for m in Collection(shacl, ignored))
            for ps in shacl.objects(shape, SH.property):
                property_shapes.add(ps)
                path = str(shacl.value(ps, SH.path))
                self.shape_props[(str(target), path)] = _constraints(shacl, ps)
        for s, p, o in shacl:
            if isinstance(s, BNode) or s in rule_shapes:
                continue  # blank nodes are compared through their parents; rule shapes as a whole
            if s in property_shapes and p in _PROPERTY_SHAPE_HANDLED:
                continue
            if s in node_shapes and p in _NODE_SHAPE_HANDLED:
                continue
            self.residual.add(f"shacl: {s} {p} {_canonical(shacl, o)}")


def _owl_modelled(s: Node, p: Node, o: Node, ontologies: set[Node], enum_classes: set[Node]) -> bool:
    """True if the signature models this OWL statement explicitly (so it is not residual)."""
    if p == RDF.type:
        return o in _OWL_TYPES or o == OWL.Ontology or o in enum_classes
    if p in (RDFS.range, RDFS.domain, RDFS.subClassOf):
        return True
    return s in ontologies and p in _ONTOLOGY_HEADER


def _domain(g: Graph, node: Node) -> str:
    union = g.value(node, OWL.unionOf)
    if union is not None:
        return "|".join(sorted(str(m) for m in Collection(g, union)))
    return str(node)


def _constraints(g: Graph, ps: Node) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in (*_LOWER_BOUNDS, *_UPPER_BOUNDS, *_EXACT):
        values = sorted((_plain(v) for v in g.objects(ps, SH[name])), key=str)
        if len(values) == 1:
            out[name] = values[0]
        elif values:
            out[name] = tuple(values)  # several values: compared exactly, so any change is breaking
    members = g.value(ps, SH["in"])
    if members is not None:
        out["in"] = frozenset(str(m) for m in Collection(g, members))
    negated = frozenset(_canonical(g, n) for n in g.objects(ps, SH["not"]))
    if negated:
        out["not"] = negated
    return out


def _plain(value: Node) -> Any:
    return value.toPython() if isinstance(value, Literal) else str(value)


def _canonical(g: Graph, node: Node) -> str:
    if isinstance(node, BNode):
        if (node, RDF.first, None) in g:
            return "(" + " ".join(_canonical(g, m) for m in Collection(g, node)) + ")"
        parts = sorted(f"{p} {_canonical(g, o)}" for p, o in g.predicate_objects(node) if p != RDFS.label)
        return "[" + "; ".join(parts) + "]"
    if isinstance(node, URIRef) and "#rule-" in str(node) and (node, None, None) in g:
        parts = sorted(
            f"{p} {_canonical(g, o)}" for p, o in g.predicate_objects(node) if p not in (RDFS.label, SH.message)
        )
        return "{" + "; ".join(parts) + "}"
    return node.n3()


def _num(value: Any, default: Decimal) -> Decimal | None:
    if value is None:
        return default
    if isinstance(value, tuple):
        return None
    return Decimal(str(value))


def diff(old: SchemaSignature, new: SchemaSignature) -> list[Change]:
    changes: list[Change] = []

    def add(kind: str, subject: str, detail: str, breaking: bool) -> None:
        changes.append(Change(kind, subject, detail, breaking))

    def add_unless_new_class(kind: str, subject: str, detail: str, breaking: bool) -> None:
        # A constraint on a class the previous release did not have cannot invalidate existing data.
        if breaking and subject.split(" ", maxsplit=1)[0] in new.classes - old.classes:
            add(kind, subject, f"{detail} (on a new class)", False)
        else:
            add(kind, subject, detail, breaking)

    _diff_vocabulary(old, new, add)
    _diff_shapes(old, new, add_unless_new_class)
    for rule_iri in sorted(set(old.rules) | set(new.rules)):
        o_r, n_r = old.rules.get(rule_iri), new.rules.get(rule_iri)
        if n_r is None:
            add("rule", rule_iri, "rule removed", False)
        elif o_r is None:
            target = new.rule_targets.get(rule_iri, "")
            if target in new.classes - old.classes:
                add("rule", rule_iri, "rule added (on a new class)", False)
            else:
                add("rule", rule_iri, "rule added", True)
        elif o_r != n_r:
            add("rule", rule_iri, "rule changed", True)
    for statement in sorted(old.residual - new.residual):
        add("unclassified", statement, "a statement the classifier does not model was removed", True)
    for statement in sorted(new.residual - old.residual):
        add("unclassified", statement, "a statement the classifier does not model was added", True)
    return changes


def _diff_vocabulary(old: SchemaSignature, new: SchemaSignature, add: Add) -> None:
    for c in sorted(old.classes - new.classes):
        add("class", c, "class removed (or its IRI changed)", True)
    for c in sorted(new.classes - old.classes):
        add("class", c, "class added", False)
    for c in sorted(old.classes & new.classes):
        if old.parents.get(c) != new.parents.get(c):
            before, after = sorted(old.parents.get(c, ())), sorted(new.parents.get(c, ()))
            add("class", c, f"parents changed from {before} to {after}", True)
    for key in sorted(set(old.annotations) | set(new.annotations)):
        if old.annotations.get(key) != new.annotations.get(key):
            add("annotation", key, "label or comment changed", False)
    for p in sorted(set(old.properties) | set(new.properties)):
        o, n = old.properties.get(p), new.properties.get(p)
        if n is None:
            add("property", p, "property removed (or renamed)", True)
            continue
        if o is None:
            add("property", p, "property added", False)
            continue
        if o[:2] != n[:2]:
            add("property", p, f"kind or range changed from {o[:2]} to {n[:2]}", True)
        if o[2] != n[2]:
            add("property", p, "became functional" if n[2] else "no longer functional", n[2])
        o_dom, n_dom = _classes(old.domains.get(p, ())), _classes(new.domains.get(p, ()))
        if o_dom - n_dom:
            add("property", p, f"domain lost {sorted(o_dom - n_dom)}", True)
        if n_dom - o_dom:
            add("property", p, f"domain gained {sorted(n_dom - o_dom)}", False)
    for i in sorted(set(old.individuals) | set(new.individuals)):
        o_types, n_types = old.individuals.get(i), new.individuals.get(i)
        if n_types is None:
            add("individual", i, "enum member removed", True)
        elif o_types is None:
            add("individual", i, "enum member added", False)
        elif o_types != n_types:
            add("individual", i, "enum member moved to another enumeration", True)


def _diff_shapes(old: SchemaSignature, new: SchemaSignature, add: Add) -> None:
    for target in sorted(set(old.closed) | set(new.closed)):
        was, now = old.closed.get(target, False), new.closed.get(target, False)
        if was != now:
            add("shape", target, "shape became closed" if now else "shape opened", now)
        o_ign, n_ign = old.ignored.get(target, frozenset()), new.ignored.get(target, frozenset())
        for prop in sorted(o_ign - n_ign):
            add("shape", target, f"closed shape no longer ignores {prop}", True)
        for prop in sorted(n_ign - o_ign):
            add("shape", target, f"closed shape now ignores {prop}", False)
    for site in sorted(set(old.shape_props) | set(new.shape_props)):
        subject = f"{site[0]} {site[1]}"
        o_c, n_c = old.shape_props.get(site), new.shape_props.get(site)
        if n_c is None:
            if site[0] in new.closed:
                add("shape", subject, "property removed from a closed shape", True)
            continue
        if o_c is None:
            minimum = n_c.get("minCount", 0)
            required = isinstance(minimum, tuple) or int(minimum) >= 1
            add("shape", subject, "required property added" if required else "optional property added", required)
            continue
        _compare_constraints(subject, o_c, n_c, add)


def _classes(domains: Iterable[str]) -> frozenset[str]:
    return frozenset(c for d in domains for c in d.split("|"))


def _compare_constraints(subject: str, o_c: dict[str, Any], n_c: dict[str, Any], add: Add) -> None:
    def higher(before: Decimal, after: Decimal) -> bool:
        return after > before

    def lower(before: Decimal, after: Decimal) -> bool:
        return after < before

    bound_kinds: tuple[tuple[tuple[str, ...], Decimal, Callable[[Decimal, Decimal], bool]], ...] = (
        (_LOWER_BOUNDS, Decimal("-Infinity"), higher),
        (_UPPER_BOUNDS, Decimal("Infinity"), lower),
    )
    for names, default, tighter in bound_kinds:
        for name in names:
            if o_c.get(name) == n_c.get(name):
                continue
            before, after = _num(o_c.get(name), default), _num(n_c.get(name), default)
            breaking = True if before is None or after is None else tighter(before, after)
            add("constraint", subject, f"{name} {o_c.get(name)} -> {n_c.get(name)}", breaking)
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


_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def parse_version(text: str) -> tuple[int, int, int]:
    match = _SEMVER.match(text)
    if not match:
        raise ValueError(f"{text!r} is not a semantic version")
    major, minor, patch = (int(g) for g in match.groups())
    return major, minor, patch


@dataclass(frozen=True, slots=True)
class CompatReport:
    release: str | None  # the previous release (highest below SCHEMA_VERSION)
    current: str
    changes: tuple[Change, ...]  # previous release -> current generation
    problem: str | None
    snapshot_changes: tuple[Change, ...] = ()  # current snapshot -> current generation (must be empty)

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

    def releases(self) -> list[str]:
        if not self._releases.exists():
            return []
        found = [p.name for p in self._releases.iterdir() if p.is_dir() and _SEMVER.match(p.name)]
        return sorted(found, key=parse_version)

    def last_release(self) -> str | None:
        found = self.releases()
        return found[-1] if found else None

    def _snapshot(self, version: str) -> SchemaSignature:
        directory = self._releases / version
        return SchemaSignature.from_graphs(
            Graph().parse(directory / self._owl_file, format="turtle"),
            Graph().parse(directory / self._shacl_file, format="turtle"),
        )

    def check(self, current: str, owl_text: str, shacl_text: str) -> CompatReport:
        cur = parse_version(current)
        releases = self.releases()
        fresh = SchemaSignature.from_graphs(
            Graph().parse(data=owl_text, format="turtle"), Graph().parse(data=shacl_text, format="turtle")
        )
        problems: list[str] = []

        newer = [r for r in releases if parse_version(r) > cur]
        if newer:
            problems.append(f"SCHEMA_VERSION {current} is older than the last release {newer[-1]}")

        snapshot_changes: tuple[Change, ...] = ()
        if current not in releases:
            problems.append(
                f"SCHEMA_VERSION {current} has no snapshot in generated/releases/; run `monograph release`"
                " in the same change as the version bump"
            )
        else:
            snapshot_changes = tuple(diff(self._snapshot(current), fresh))
            if snapshot_changes:
                breaking = sum(c.breaking for c in snapshot_changes)
                need = "a major" if breaking else "a minor or patch"
                problems.append(
                    f"the models no longer match the {current} snapshot ({len(snapshot_changes)} changes,"
                    f" {breaking} breaking); releases are immutable, so this needs {need} version bump and a"
                    " new release"
                )

        older = [r for r in releases if parse_version(r) < cur]
        previous = older[-1] if older else None
        changes: tuple[Change, ...] = ()
        if previous is not None:
            changes = tuple(diff(self._snapshot(previous), fresh))
            if any(c.breaking for c in changes) and parse_version(previous)[0] == cur[0]:
                problems.append(
                    f"breaking changes since {previous} need a major version bump (SCHEMA_VERSION is {current})"
                )

        for a, b in itertools.pairwise(releases):
            if parse_version(b) > cur:
                continue
            pair = diff(self._snapshot(a), self._snapshot(b))
            if any(c.breaking for c in pair) and parse_version(a)[0] == parse_version(b)[0]:
                problems.append(f"release {b} contains breaking changes from {a} without a major bump")

        return CompatReport(previous, current, changes, "; ".join(problems) or None, snapshot_changes)
