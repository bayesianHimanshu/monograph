"""The reference graph and deterministic resolution against it. No LLM is involved here.

The reference graph is an in-memory rdflib graph of H6 instances from registry sources:

* Substance nodes: DailyMed `/uniis` (active moieties), the H6 openFDA substances, and every ingredient
  declared in the structured part of the sampled SPLs, pooled over all labels. Alternate names are
  `rdfs:label` values on the Substance node.
* Product, PackagedProduct, ClinicalDrug, Organization nodes: the openFDA NDC directory records for the
  sampled labels (and the H6 sample), mapped by the H6 ingest code.
* Label nodes: openFDA label records, which link a label to its products (`labelsProduct`).

Resolution reads the graph:

* substance name -> Substance: normalized exact match on any name of the node; several matches is
  "ambiguous" and resolves nothing;
* package NDC -> PackagedProduct: exact match on the NDC (A2's deterministic repair has already fixed
  hyphenation).
"""

from __future__ import annotations

import copy
import difflib
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from rdflib import RDF, RDFS, Graph, Literal, URIRef

from monograph.core.instances import Instantiator
from monograph.core.models import LabelExtraction, PackagedProduct, Product, Substance
from monograph.core.refs import Ref
from monograph.core.spec import SchemaSpec
from monograph.core.vocab import Vocabulary
from monograph.ingest.openfda import Candidate
from monograph.rdf.mapping import RdfMapper

_SUFFIXES = re.compile(r"(,?\s+(USP|NF|BP|EP|JP|PH\.? ?EUR\.?))+$")
_MARKS = re.compile("[®™©]")  # (R), (TM), (C)


# Unicode hyphen, non-breaking hyphen, figure dash, en dash, em dash, horizontal bar, minus sign.
_DASHES = re.compile("[" + chr(0x2010) + "-" + chr(0x2015) + chr(0x2212) + "]")


def normalize_name(name: str) -> str:
    """Case, width, whitespace, trademark marks, dash variants and pharmacopoeia suffixes. Nothing else:
    salt forms are distinct substances with distinct UNIIs and are never folded together."""
    text = unicodedata.normalize("NFKC", name)
    text = _MARKS.sub("", text).upper()
    text = _DASHES.sub("-", text)
    text = re.sub(r"\s+", " ", text).strip(" .,;")
    return _SUFFIXES.sub("", text).strip()


@dataclass(frozen=True, slots=True)
class SubstanceSource:
    unii: str
    name: str
    origin: str  # dailymed-uniis | openfda | spl
    set_id: str | None = None  # the label this source is linked to; None for label-independent sources


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Resolution:
    status: ResolutionStatus
    iri: str | None
    candidates: tuple[str, ...]  # nearest names in the graph, for feedback


class ReferenceGraph:
    """Builds the graph from registry data, then answers questions about it."""

    def __init__(self, spec: SchemaSpec, vocab: Vocabulary, mapper: RdfMapper) -> None:
        self._spec = spec
        self._vocab = vocab
        self._mapper = mapper
        self._instantiator = Instantiator(spec)
        self.graph = Graph()
        self.skipped: list[tuple[str, str]] = []  # (candidate class, reason) for records that could not map
        # UNIIs the registry attaches to each label's products (openFDA `openfda.unii` on the label's own NDC
        # and label records). Used only to break ties between substances that share a name.
        self.label_uniis: dict[str, frozenset[str]] = {}
        # Provenance, for feedback candidates: which labels' sources contributed each normalized substance name
        # and each NDC. None stands for a source linked to no label (for example DailyMed's moiety list).
        self.name_links: dict[str, set[str | None]] = defaultdict(set)
        self.ndc_links: dict[str, set[str | None]] = defaultdict(set)

    def add_substances(self, sources: Iterable[SubstanceSource]) -> None:
        names: dict[str, list[str]] = defaultdict(list)
        for s in sources:
            if re.fullmatch(r"[0-9A-Z]{10}", s.unii) and s.name.strip():
                names[s.unii].append(s.name.strip())
                self.name_links[normalize_name(s.name)].add(s.set_id)
        for unii in sorted(names):
            ordered = sorted(set(names[unii]), key=lambda n: (len(n), n))
            iri = URIRef(self._mapper.iri_of(Substance.model_construct(unii=unii, name=ordered[0])))
            self.graph.add((iri, RDF.type, self._vocab.cls(Substance)))
            self.graph.add((iri, URIRef(self._vocab.ns + "unii"), Literal(unii)))
            self.graph.add((iri, URIRef(self._vocab.ns + "name"), Literal(ordered[0])))
            for alt in ordered[1:]:
                self.graph.add((iri, RDFS.label, Literal(alt)))

    def add_candidates(self, candidates: Iterable[Candidate]) -> None:
        """Registry records as they are: unvalidated, because an H6 gap product is still a fact about a
        product (it has an NDC, a name, a number of ingredients) even when one of its fields is missing."""
        for c in candidates:
            if c.cls == "Substance":
                continue  # substances come only through add_substances, with all their names
            cls = self._spec.by_name(c.cls).cls
            instance = self._instantiator.instantiate(cls, c.record)
            try:
                self.graph += self._mapper.to_graph(instance)
            except Exception as exc:  # a record without its key cannot become a node; keep the reason
                self.skipped.append((c.cls, str(exc)[:120]))

    # ------------------------------------------------------------------------------------- queries

    def label_products(self, set_id: str) -> set[str]:
        prop = URIRef(self._vocab.ns + "labelsProduct")
        set_prop = URIRef(self._vocab.ns + "setId")
        out: set[str] = set()
        for label in self.graph.subjects(set_prop, Literal(set_id)):
            out |= {str(p) for p in self.graph.objects(label, prop)}
        return out

    def packages_of(self, product_iris: Iterable[str]) -> dict[str, str]:
        """NDC -> PackagedProduct IRI for the packages of these products."""
        product_prop = URIRef(self._vocab.ns + "product")
        ndc_prop = URIRef(self._vocab.ns + "ndc")
        out: dict[str, str] = {}
        for product in product_iris:
            for package in self.graph.subjects(product_prop, URIRef(product)):
                ndc = self.graph.value(package, ndc_prop)
                if ndc is not None:
                    out[str(ndc)] = str(package)
        return dict(sorted(out.items()))

    def link_ndc(self, ndc: str, set_id: str | None) -> None:
        self.ndc_links[ndc].add(set_id)

    def independent_ndcs(self, set_id: str, like: str) -> tuple[str, ...]:
        """Registry NDCs with the same labeler code as `like`, each known from at least one source that is not
        linked to the label `set_id`. Never anything taken only from that label's own records."""
        labeler = like.split("-", 1)[0]
        return tuple(
            sorted(ndc for ndc, links in self.ndc_links.items() if ndc.split("-", 1)[0] == labeler and links - {set_id})
        )

    def product_of_package(self, package_iri: str) -> str | None:
        value = self.graph.value(URIRef(package_iri), URIRef(self._vocab.ns + "product"))
        return None if value is None else str(value)

    def ingredient_count(self, product_iri: str) -> int | None:
        """Number of ingredients the registry lists for a product, or None if the product is unknown."""
        node = URIRef(product_iri)
        if (node, RDF.type, self._vocab.cls(Product)) not in self.graph:
            return None
        return len(list(self.graph.objects(node, URIRef(self._vocab.ns + "ingredient"))))


class SubstanceResolver:
    def __init__(self, reference: ReferenceGraph, vocab: Vocabulary, max_candidates: int = 5) -> None:
        self._max = max_candidates
        self.label_uniis = reference.label_uniis
        self._name_links = reference.name_links
        self._default_context: frozenset[str] = frozenset()
        index: dict[str, set[str]] = defaultdict(set)
        g = reference.graph
        for node in g.subjects(RDF.type, vocab.cls(Substance)):
            for predicate in (URIRef(vocab.ns + "name"), RDFS.label):
                for name in g.objects(node, predicate):
                    index[normalize_name(str(name))].add(str(node))
        self._index = {k: frozenset(v) for k, v in index.items()}
        self._names = sorted(self._index)

    def for_label(self, set_id: str) -> SubstanceResolver:
        """The same resolver, breaking name ties with this label's registry UNIIs by default."""
        bound = copy.copy(self)
        bound._default_context = self.label_uniis.get(set_id, frozenset())
        return bound

    def candidates(self, name: str, exclude_set_id: str | None = None) -> tuple[str, ...]:
        """Nearest names in the graph. With `exclude_set_id`, only names that some source not linked to that
        label also contributes, so a label's own ingredient list can never be offered back for it."""
        key = normalize_name(name)
        names = (
            self._names
            if exclude_set_id is None
            else [n for n in self._names if self._name_links.get(n, set()) - {exclude_set_id}]
        )
        return tuple(difflib.get_close_matches(key, names, n=self._max, cutoff=0.6))

    def resolve(self, name: str, context_uniis: frozenset[str] = frozenset()) -> Resolution:
        """Exact normalized-name match. When several substances share the name, the one UNII among them
        that the registry attaches to this label's products wins; otherwise the name stays ambiguous.
        Context never resolves a name that matches nothing."""
        key = normalize_name(name)
        hits = self._index.get(key, frozenset())
        context_uniis = context_uniis or self._default_context
        if len(hits) > 1 and context_uniis:
            in_context = frozenset(h for h in hits if h.rsplit("/", 1)[-1] in context_uniis)
            if len(in_context) == 1:
                hits = in_context
        if len(hits) == 1:
            return Resolution(ResolutionStatus.RESOLVED, next(iter(hits)), ())
        candidates = tuple(difflib.get_close_matches(key, self._names, n=self._max, cutoff=0.6))
        status = ResolutionStatus.AMBIGUOUS if hits else ResolutionStatus.UNKNOWN
        return Resolution(status, None, candidates)


class NdcResolver:
    def __init__(self, reference: ReferenceGraph, vocab: Vocabulary) -> None:
        g = reference.graph
        ndc_prop = URIRef(vocab.ns + "ndc")
        self._index = {
            str(ndc): str(node)
            for node in g.subjects(RDF.type, vocab.cls(PackagedProduct))
            for ndc in g.objects(node, ndc_prop)
        }

    def resolve(self, ndc: str) -> str | None:
        return self._index.get(ndc)


@dataclass(frozen=True, slots=True)
class ResolvedExtraction:
    extraction: LabelExtraction
    substances: tuple[tuple[str, Resolution], ...]  # (extracted name, resolution), in extraction order
    packages: tuple[tuple[str, str | None], ...]  # (extracted NDC, PackagedProduct IRI or None)


def resolve_extraction(
    extraction: LabelExtraction, substances: SubstanceResolver, ndcs: NdcResolver, registry_lists_label: bool
) -> ResolvedExtraction:
    """Fill the derived resolver links. The extraction must already be a validated model.

    registry_lists_label: whether the reference graph links this label to any product; only then must
    its package NDCs resolve (rule listed-package-resolves)."""
    substances = substances.for_label(extraction.set_id)
    found_substances: list[tuple[str, Resolution]] = []
    found_packages: list[tuple[str, str | None]] = []
    products = []
    for product in extraction.products:
        ingredients = []
        for ingredient in product.ingredients:
            resolution = substances.resolve(ingredient.substance_name)
            found_substances.append((ingredient.substance_name, resolution))
            substance = Ref[Substance](resolution.iri) if resolution.iri else None
            ingredients.append(ingredient.model_copy(update={"resolved_substance": substance}))
        packages = []
        for package in product.packages:
            iri = ndcs.resolve(package.ndc)
            found_packages.append((package.ndc, iri))
            link = Ref[PackagedProduct](iri) if iri else None
            packages.append(
                package.model_copy(update={"resolved_package": link, "registry_lists_product": registry_lists_label})
            )
        products.append(product.model_copy(update={"ingredients": ingredients, "packages": packages}))
    resolved = extraction.model_copy(update={"products": products})
    return ResolvedExtraction(resolved, tuple(found_substances), tuple(found_packages))


def substance_sources_from_spls(documents: Sequence[Any]) -> list[SubstanceSource]:
    """Every ingredient (active and inactive) declared in the structured part of the given SPLs, each linked
    to its label so it is never offered back as a candidate for that label."""
    out = []
    for doc in documents:
        for product in doc.products:
            out += [SubstanceSource(i.unii, i.name, "spl", doc.set_id) for i in product.active if i.unii]
            out += [SubstanceSource(u, n, "spl", doc.set_id) for i in product.active for u, n in i.strength_basis]
            out += [SubstanceSource(unii, name, "spl", doc.set_id) for unii, name in product.inactive]
    return out
