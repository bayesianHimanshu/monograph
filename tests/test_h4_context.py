"""Stage 2: extraction schema, graph-context shapes, reference graph and deterministic resolution."""

from __future__ import annotations

import json
from decimal import Decimal

import anthropic
import pytest
from rdflib import RDF, Graph, URIRef

from monograph import wiring
from monograph.core.models import (
    DosageForm,
    ExtractedIngredient,
    ExtractedPackage,
    ExtractedProduct,
    LabelExtraction,
    Route,
    UcumUnit,
)
from monograph.core.spec import SchemaSpec
from monograph.core.vocab import Vocabulary
from monograph.extract.registry import label_links, nested_packages
from monograph.extract.resolve import (
    NdcResolver,
    ReferenceGraph,
    ResolutionStatus,
    SubstanceResolver,
    SubstanceSource,
    normalize_name,
    resolve_extraction,
)
from monograph.gen.artifacts import CONTEXT_SHACL_FILE, EXTRACTION_SCHEMA_FILE
from monograph.gen.shacl import SH
from monograph.ingest.openfda import OpenFdaMapper
from monograph.validate.shacl_side import ShaclValidator

V = Vocabulary()
FILES = wiring.artifact_builder(wiring.build_spec(), V).build().files

NDC_RECORD = {
    "product_ndc": "0280-2000",
    "product_id": "0280-2000_x",
    "generic_name": "ASPIRIN",
    "brand_name": "Bayer Low Dose",
    "labeler_name": "Bayer HealthCare LLC",
    "dosage_form": "TABLET, CHEWABLE",
    "product_type": "HUMAN OTC DRUG",
    "finished": True,
    "route": ["ORAL"],
    "active_ingredients": [{"name": "ASPIRIN", "strength": "81 mg/1"}],
    "packaging": [
        {
            "package_ndc": "0280-2000-10",
            "description": "1 BOTTLE in 1 CARTON (0280-2000-10) > 36 TABLET in 1 BOTTLE (0280-2000-11)",
        }
    ],
    "openfda": {"unii": ["R16CO5Y76E"], "spl_set_id": ["0c3d2f4e-1a2b-4c5d-8e9f-0a1b2c3d4e5f"]},
}
SET_ID = "0c3d2f4e-1a2b-4c5d-8e9f-0a1b2c3d4e5f"


@pytest.fixture(scope="module")
def reference(spec: SchemaSpec) -> ReferenceGraph:
    ref = ReferenceGraph(spec, V, wiring.mapper(spec, V))
    ref.add_substances(
        [
            SubstanceSource("R16CO5Y76E", "ASPIRIN", "spl"),
            SubstanceSource("R16CO5Y76E", "ACETYLSALICYLIC ACID", "dailymed-uniis"),
            SubstanceSource("AAAAAAAAAA", "SAMENAME", "spl"),
            SubstanceSource("BBBBBBBBBB", "SAMENAME", "spl"),
        ]
    )
    mapped = OpenFdaMapper().ndc(NDC_RECORD)
    ref.add_candidates([*mapped, *nested_packages(mapped, NDC_RECORD), *label_links(NDC_RECORD)])
    return ref


def test_extraction_schema_leaves_out_derived_fields_and_survives_the_sdk_transform() -> None:
    doc = json.loads(FILES[EXTRACTION_SCHEMA_FILE])
    defs = doc["$defs"]
    assert set(defs["LabelExtraction"]["properties"]) == {"products"}
    assert "resolved_substance" not in defs["ExtractedIngredient"]["properties"]
    assert set(defs["ExtractedPackage"]["properties"]) == {"ndc"}
    assert len(defs["DosageForm"]["enum"]) == len(DosageForm)
    assert len(defs["UcumUnit"]["enum"]) == len(UcumUnit)
    transformed = anthropic.transform_schema(doc)
    assert transformed["$defs"]["ExtractedPackage"]["additionalProperties"] is False


def test_context_shapes_target_only_extraction_classes_and_require_resolution() -> None:
    g = Graph().parse(data=FILES[CONTEXT_SHACL_FILE], format="turtle")
    targets = {str(t).rsplit("#", 1)[-1] for t in g.objects(None, SH.targetClass)}
    assert targets == {"LabelExtraction", "ExtractedProduct", "ExtractedIngredient", "ExtractedPackage"}
    ctx = wiring.context_vocab()
    resolved = URIRef(ctx.shapes_ns + "ExtractedIngredient-resolvedSubstance")
    assert str(g.value(resolved, SH.minCount)) == "1"
    assert str(g.value(resolved, SH["class"])).endswith("#Substance")
    package = URIRef(ctx.shapes_ns + "ExtractedPackage-resolvedPackage")
    assert g.value(package, SH.minCount) is None  # conditional: rule listed-package-resolves
    assert (URIRef(ctx.shapes_ns + "rule-listed-package-resolves"), RDF.type, SH.NodeShape) in g


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("Aspirin", "ASPIRIN"),
        ("  acetaminophen  USP ", "ACETAMINOPHEN"),
        ("Examplium® hydrochloride", "EXAMPLIUM HYDROCHLORIDE"),
        ("ceftizoxime" + chr(0x2010) + "sodium", "CEFTIZOXIME-SODIUM"),
    ],
)
def test_normalize_name(raw: str, normalized: str) -> None:
    assert normalize_name(raw) == normalized


def test_substance_resolution(reference: ReferenceGraph) -> None:
    resolver = SubstanceResolver(reference, V)
    assert resolver.resolve("aspirin").iri == "https://example.org/monograph/substance/R16CO5Y76E"
    assert resolver.resolve("Acetylsalicylic acid").status is ResolutionStatus.RESOLVED  # an alternate name
    assert resolver.resolve("samename").status is ResolutionStatus.AMBIGUOUS
    unknown = resolver.resolve("asprin")
    assert unknown.status is ResolutionStatus.UNKNOWN
    assert "ASPIRIN" in unknown.candidates


def test_registry_knows_nested_packages_and_label_links(reference: ReferenceGraph) -> None:
    ndcs = NdcResolver(reference, V)
    assert ndcs.resolve("0280-2000-10") is not None
    assert ndcs.resolve("0280-2000-11") is not None  # inner package, from the description text
    products = reference.label_products(SET_ID)
    assert products == {"https://example.org/monograph/product/0280-2000"}
    assert set(reference.packages_of(products)) == {"0280-2000-10", "0280-2000-11"}
    assert reference.ingredient_count("https://example.org/monograph/product/0280-2000") == 1


def _extraction(name: str, ndc: str) -> LabelExtraction:
    ingredient = ExtractedIngredient(
        substance_name=name,
        strength_value=Decimal(81),
        strength_unit=UcumUnit.MILLIGRAM,
        basis_value=Decimal(1),
        basis_unit=UcumUnit.UNIT_OF_PRESENTATION,
    )
    product = ExtractedProduct(
        name="Bayer Low Dose",
        dosage_form=DosageForm.TABLET_CHEWABLE,
        routes=frozenset({Route.ORAL}),
        ingredients=[ingredient],
        packages=[ExtractedPackage(ndc=ndc)],
    )
    return LabelExtraction(set_id=SET_ID, version=1, products=[product])


def _context_verdict(reference: ReferenceGraph, extraction: LabelExtraction) -> set[str]:
    resolved = resolve_extraction(
        extraction, SubstanceResolver(reference, V), NdcResolver(reference, V), registry_lists_label=True
    )
    mapper = wiring.mapper(wiring.build_spec(), V)
    merged = Graph() + reference.graph + mapper.to_graph(resolved.extraction)  # no stubs: refs must exist
    shapes = Graph().parse(data=FILES[CONTEXT_SHACL_FILE], format="turtle")
    verdict = ShaclValidator(shapes, mapper).validate(merged)
    return {v.path.split(" ")[-1].rsplit("#", 1)[-1] for v in verdict.violations}


def test_graph_context_validation(reference: ReferenceGraph) -> None:
    assert _context_verdict(reference, _extraction("aspirin", "0280-2000-10")) == set()
    assert "resolvedSubstance" in "".join(_context_verdict(reference, _extraction("asprin", "0280-2000-10")))
    package = _context_verdict(reference, _extraction("aspirin", "0280-2000-99"))
    assert package  # the registry lists this label's products, and 0280-2000-99 is not among them
