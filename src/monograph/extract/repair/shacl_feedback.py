"""Graph-context feedback (arm A3): resolve the extraction, merge it into the reference graph, validate
with the generated context shapes and the extraction_only checks, and write the report for the model.

Each issue carries the focus node (as a location in the extraction), the path, the message, the
offending value and, where the shape or the graph knows them, the allowed candidates.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

import pyshacl
from rdflib import RDF, Graph
from rdflib.collection import Collection
from rdflib.term import Node

from monograph.core.models import LabelExtraction
from monograph.extract.resolve import (
    NdcResolver,
    ReferenceGraph,
    ResolvedExtraction,
    SubstanceResolver,
    resolve_extraction,
)
from monograph.gen.shacl import SH
from monograph.rdf.mapping import RdfMapper


@dataclass(frozen=True, slots=True)
class ContextIssue:
    source: str  # shacl | extraction_only:<id>
    location: str
    path: str
    message: str
    value: str | None
    candidates: tuple[str, ...] = field(default=())


class CandidatePolicy(StrEnum):
    """Where the "allowed values" in feedback may come from.

    INDEPENDENT (arm A3): only sources not linked to the label being extracted, such as other labels'
        records from the same labeler and DailyMed's moiety list; never the label's own SPL, its DailyMed
        NDC list or its own NDC directory records. Messages state that a value does not resolve or does not
        belong, without registry facts taken from the label (such as its ingredient count).
    LABEL (arm A3+): candidates from the label's own registry records. They can contain gold, so A3+ is
        reported only as an upper bound, outside the verdict.
    """

    INDEPENDENT = "independent"
    LABEL = "label"


ExtractionOnlyCheck = Callable[[ResolvedExtraction, ReferenceGraph, CandidatePolicy], list[ContextIssue]]


def _location(iri: str) -> str:
    """`.../label-extraction/<id>/<v>/extractedProduct/0/extractedIngredient/1` -> `products[0].ingredients[1]`."""
    names = {"extractedProduct": "products", "extractedIngredient": "ingredients", "extractedPackage": "packages"}
    parts = re.findall(r"/(extractedProduct|extractedIngredient|extractedPackage)/(\d+)", iri)
    return ".".join(f"{names[p]}[{i}]" for p, i in parts) or "label"


def _package_matches_label(
    resolved: ResolvedExtraction, reference: ReferenceGraph, policy: CandidatePolicy
) -> list[ContextIssue]:
    set_id = resolved.extraction.set_id
    label_products = reference.label_products(set_id)
    if not label_products:
        return []  # the registry does not know this label's products, so membership cannot be checked
    issues = []
    for p_index, product in enumerate(resolved.extraction.products):
        for k_index, package in enumerate(product.packages):
            if package.resolved_package is None:
                continue  # reported by the context shapes (listed-package-resolves)
            owner = reference.product_of_package(package.resolved_package.iri)
            if owner not in label_products:
                issues.append(
                    ContextIssue(
                        "extraction_only:package-product-matches-label",
                        f"products[{p_index}].packages[{k_index}]",
                        "ndc",
                        "this NDC belongs to a product that this label does not describe",
                        package.ndc,
                        _ndc_candidates(reference, set_id, package.ndc, policy),
                    )
                )
    return issues


def _ndc_candidates(reference: ReferenceGraph, set_id: str, ndc: str, policy: CandidatePolicy) -> tuple[str, ...]:
    if policy is CandidatePolicy.LABEL:
        return tuple(reference.packages_of(reference.label_products(set_id)))
    return tuple(n for n in reference.independent_ndcs(set_id, ndc) if n != ndc)


def _ingredient_count(
    resolved: ResolvedExtraction, reference: ReferenceGraph, policy: CandidatePolicy
) -> list[ContextIssue]:
    issues = []
    for p_index, product in enumerate(resolved.extraction.products):
        owners = Counter(
            owner
            for package in product.packages
            if package.resolved_package is not None
            and (owner := reference.product_of_package(package.resolved_package.iri)) is not None
        )
        if not owners:
            continue  # no registry product to compare with
        owner = owners.most_common(1)[0][0]
        expected = reference.ingredient_count(owner)
        if expected is not None and expected != len(product.ingredients):
            issues.append(
                ContextIssue(
                    "extraction_only:ingredient-count-matches-product",
                    f"products[{p_index}]",
                    "ingredients",
                    (
                        f"the registry lists {expected} active ingredient(s) for this product's packages,"
                        f" the answer has {len(product.ingredients)}"
                        if policy is CandidatePolicy.LABEL
                        else "the number of active ingredients does not match the registry's record for this"
                        " product's packages"
                    ),
                    str(len(product.ingredients)),
                )
            )
    return issues


# Every extraction_only id declared on the model has exactly one implementation here (checked by a test).
EXTRACTION_ONLY_CHECKS: dict[str, ExtractionOnlyCheck] = {
    "package-product-matches-label": _package_matches_label,
    "ingredient-count-matches-product": _ingredient_count,
}


class GraphContextChecker:
    def __init__(
        self,
        reference: ReferenceGraph,
        substances: SubstanceResolver,
        ndcs: NdcResolver,
        context_shapes: Graph,
        mapper: RdfMapper,
        policy: CandidatePolicy = CandidatePolicy.INDEPENDENT,
        checks: dict[str, ExtractionOnlyCheck] = EXTRACTION_ONLY_CHECKS,
    ) -> None:
        self.policy = policy
        self._reference = reference
        self._substances = substances
        self._ndcs = ndcs
        self._shapes = context_shapes
        self._mapper = mapper
        self._checks = checks

    def check(self, extraction: LabelExtraction) -> tuple[ResolvedExtraction, list[ContextIssue]]:
        listed = bool(self._reference.label_products(extraction.set_id))
        resolved = resolve_extraction(extraction, self._substances, self._ndcs, listed)
        merged = Graph()
        merged += self._reference.graph
        merged += self._mapper.to_graph(resolved.extraction)
        issues = self._shacl(merged, resolved)
        for check in self._checks.values():
            issues += check(resolved, self._reference, self.policy)
        return resolved, issues

    def _shacl(self, merged: Graph, resolved: ResolvedExtraction) -> list[ContextIssue]:
        conforms, report, _ = pyshacl.validate(merged, shacl_graph=self._shapes, inference="none", advanced=False)
        if conforms:
            return []
        assert isinstance(report, Graph)
        names = {name: res for name, res in resolved.substances}
        out: set[ContextIssue] = set()
        for result in report.subjects(RDF.type, SH.ValidationResult):
            if report.value(result, SH.sourceConstraintComponent) == SH.NodeConstraintComponent:
                continue  # a parent's summary of nested results, which are reported on their own
            focus = str(report.value(result, SH.focusNode))
            path = report.value(result, SH.resultPath)
            path_name = str(path).rsplit("#", 1)[-1] if path is not None else ""
            value = report.value(result, SH.value)
            message = str(report.value(result, SH.resultMessage) or "")
            candidates: tuple[str, ...] = ()
            location = _location(focus)
            offending = None if value is None else str(value)
            if path_name == "resolvedSubstance":
                name = self._ingredient_name(resolved, location)
                offending = name
                if name is not None and self.policy is CandidatePolicy.LABEL and name in names:
                    candidates = names[name].candidates
                elif name is not None:
                    candidates = self._substances.candidates(name, exclude_set_id=resolved.extraction.set_id)
                message = "this substance name does not resolve to exactly one substance in the reference graph"
            elif "listed-package-resolves" in str(report.value(result, SH.sourceShape)):
                offending = self._package_ndc(resolved, location)
                candidates = _ndc_candidates(self._reference, resolved.extraction.set_id, offending or "", self.policy)
                path_name = "ndc"
                message = "this NDC is not a package of the products the registry lists for this label"
            else:
                candidates = self._allowed(report.value(result, SH.sourceShape))
            out.add(ContextIssue("shacl", location, path_name, message, offending, candidates))
        return sorted(out, key=lambda i: (i.location, i.path, i.message))

    def _allowed(self, shape: Node | None) -> tuple[str, ...]:
        if shape is None:
            return ()
        members = self._shapes.value(shape, SH["in"])
        if members is None:
            return ()
        return tuple(str(m).rsplit("-", 1)[-1] for m in Collection(self._shapes, members))

    @staticmethod
    def _ingredient_name(resolved: ResolvedExtraction, location: str) -> str | None:
        m = re.fullmatch(r"products\[(\d+)\]\.ingredients\[(\d+)\]", location)
        if not m:
            return None
        return resolved.extraction.products[int(m[1])].ingredients[int(m[2])].substance_name

    @staticmethod
    def _package_ndc(resolved: ResolvedExtraction, location: str) -> str | None:
        m = re.fullmatch(r"products\[(\d+)\]\.packages\[(\d+)\]", location)
        if not m:
            return None
        return resolved.extraction.products[int(m[1])].packages[int(m[2])].ndc


def context_feedback(issues: Sequence[ContextIssue]) -> str:
    lines = [
        "Your answer was checked against the drug product registry. Fix these problems, using only the label"
        " text, and answer again in full. Where allowed values are listed, choose among them only if the"
        " label text supports it; otherwise leave the value out.",
    ]
    for i in issues:
        line = f"- at `{i.location}` ({i.path}): {i.message}"
        if i.value is not None:
            line += f"; your value: {i.value!r}"
        if i.candidates:
            shown = ", ".join(i.candidates[:20])
            more = f" (and {len(i.candidates) - 20} more)" if len(i.candidates) > 20 else ""
            line += f"; allowed: {shown}{more}"
        lines.append(line)
    return "\n".join(lines)
