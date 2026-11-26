"""The oracle extraction: what a perfect extractor would return, built from gold. Used to test the
scorer and to measure the ceiling a perfect extractor could reach under these definitions."""

from __future__ import annotations

import re
from typing import Any

from monograph.extract.sources.dailymed import SplDocument, narrative_text


def oracle_payload(doc: SplDocument) -> dict[str, Any]:
    text = narrative_text(doc)
    digits = re.sub(r"\D", "", text)
    products = []
    for p in doc.products:
        if not p.active:
            continue
        products.append(
            {
                "name": p.name,
                "dosage_form": p.dosage_form,
                "routes": list(p.routes),
                "ingredients": [
                    {
                        "substance_name": i.name,
                        "strength_value": i.numerator_value,
                        "strength_unit": i.numerator_unit,
                        "basis_value": i.denominator_value,
                        "basis_unit": i.denominator_unit,
                    }
                    for i in p.active
                ],
                "packages": [{"ndc": n} for n in p.packages if n in text or n.replace("-", "") in digits],
            }
        )
    return {"products": products}
