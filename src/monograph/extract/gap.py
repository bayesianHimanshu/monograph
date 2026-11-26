"""The "H6 gap": openFDA NDC products that failed H6 on ingredient pairing (substance missing)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from monograph.core.instances import Instantiator
from monograph.core.spec import SchemaSpec
from monograph.ingest.openfda import OpenFdaMapper
from monograph.validate.pydantic_side import PydanticValidator


def h6_gap_products(
    spec: SchemaSpec, ndc_records: Sequence[dict[str, Any]], pydantic: PydanticValidator
) -> list[dict[str, Any]]:
    instantiator, mapper = Instantiator(spec), OpenFdaMapper()
    product_cls = spec.by_name("Product").cls
    out = []
    for raw in ndc_records:
        for candidate in mapper.ndc(raw):
            if candidate.cls != "Product":
                continue
            verdict = pydantic.check(instantiator.instantiate(product_cls, candidate.record))
            if any(v.path.startswith("ingredients") and v.path.endswith("substance") for v in verdict.violations):
                out.append(raw)
    return out


def gap_ndcs(records: Sequence[dict[str, Any]]) -> list[tuple[str, str]]:
    """(product NDC, first package NDC or the product NDC) for each gap product, sorted."""
    pairs = []
    for raw in records:
        packages = raw.get("packaging") or []
        package = packages[0].get("package_ndc") if packages else raw["product_ndc"]
        pairs.append((str(raw["product_ndc"]), str(package)))
    return sorted(set(pairs))
