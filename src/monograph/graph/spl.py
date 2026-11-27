"""Active moieties and ingredient pairing from the structured part of SPL documents.

Every active ingredient's `ingredientSubstance` carries `activeMoiety/activeMoiety` with the moiety's UNII
and name, whatever the ingredient's classCode. That is the salt-to-moiety data H4 used (H4 read it only
for ACTIM strengths).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

from monograph.extract.sources.dailymed import ACTIVE_CLASSES, NDC_SYSTEM, NS, UNII_SYSTEM


@dataclass(frozen=True, slots=True)
class MoietyFact:
    set_id: str
    product_ndc: str | None
    unii: str
    name: str
    moiety_unii: str
    moiety_name: str


def _code(el: ET.Element | None, system: str) -> str | None:
    if el is None or el.get("codeSystem") != system:
        return None
    return el.get("code")


def _text(el: ET.Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def moiety_facts(data: bytes) -> list[MoietyFact]:
    root = ET.fromstring(data)
    set_el = root.find("v3:setId", NS)
    set_id = (set_el.get("root", "") if set_el is not None else "").lower()
    out: list[MoietyFact] = []
    for product in root.iter("{urn:hl7-org:v3}manufacturedProduct"):
        ndc = _code(product.find("v3:code", NS), NDC_SYSTEM)
        for ingredient in product.iter("{urn:hl7-org:v3}ingredient"):
            if ingredient.get("classCode") not in ACTIVE_CLASSES:
                continue
            substance = ingredient.find("v3:ingredientSubstance", NS)
            if substance is None:
                continue
            unii = _code(substance.find("v3:code", NS), UNII_SYSTEM)
            if not unii:
                continue
            for moiety in substance.findall("v3:activeMoiety/v3:activeMoiety", NS):
                m_unii = _code(moiety.find("v3:code", NS), UNII_SYSTEM)
                if m_unii:
                    out.append(
                        MoietyFact(
                            set_id,
                            ndc,
                            unii,
                            _text(substance.find("v3:name", NS)),
                            m_unii,
                            _text(moiety.find("v3:name", NS)),
                        )
                    )
    return out
