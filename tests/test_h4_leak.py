"""No gold value can reach a prompt through the structured XML.

The sentinel SPL puts unique values in every structured element (product data section, subjects nested
inside a narrative section, codes, quantities, packaging, routes) and in sections the extractor must
not read. The gold parser must find them; the narrative input must contain none of them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from monograph.extract.sources.dailymed import NARRATIVE_SECTIONS, narrative_text, parse_spl

from .conftest import ROOT

SPL_DIR = ROOT / "fixtures" / "h4" / "spl"
SENTINEL = SPL_DIR / "sentinel.xml"
STRUCTURED_ONLY = (
    "SENTINELPRODUCTNAME",
    "SENTINELFORM",
    "SENTINELROUTE",
    "SENTINELSUBSTANCE",
    "SENTINELINACTIVE",
    "SENT1NEL01",
    "SENT1NEL02",
    "99999-999",
    "777.777",
    "SENTINELPRODUCTDATATEXT",
    "SENTINELINDICATIONS",
    "SENTINELINDEXING",
    "SENTINELNESTEDSUBJECT",
)


def test_gold_parser_finds_the_structured_sentinels() -> None:
    doc = parse_spl(SENTINEL.read_bytes())
    [product] = doc.products
    assert product.name == "SENTINELPRODUCTNAME"
    assert product.dosage_form == "SENTINELFORM"
    assert product.routes == ("SENTINELROUTE",)
    assert [(i.unii, str(i.numerator_value)) for i in product.active] == [("SENT1NEL01", "777.777")]
    assert product.packages == ("99999-999-99",)
    assert product.inactive == (("SENT1NEL02", "SENTINELINACTIVE"),)


def test_no_structured_value_reaches_the_narrative_input() -> None:
    text = narrative_text(parse_spl(SENTINEL.read_bytes()))
    leaked = [s for s in STRUCTURED_ONLY if s in text]
    assert leaked == []
    assert "examplium hydrochloride equivalent to 50 mg" in text  # the narrative itself is kept
    assert "NDC 12345-678-90" in text
    assert "Each tablet is round and white." in text  # narrative subsections are kept


@pytest.mark.parametrize(
    "path", sorted(p for p in SPL_DIR.glob("*.xml") if p.name != "sentinel.xml"), ids=lambda p: p.stem[:8]
)
def test_real_labels_expose_only_whitelisted_narrative(path: Path) -> None:
    doc = parse_spl(path.read_bytes())
    text = narrative_text(doc)
    assert "<" not in text or "<" in "".join(s.text for s in doc.sections)
    assert {s.code for s in doc.sections} <= set(NARRATIVE_SECTIONS)
    assert text == "\n\n".join([f"TITLE\n{doc.title}", *(f"{s.title.upper()}\n{s.text}" for s in doc.sections)])
    # A UNII may appear in the prompt only if the label's own narrative prints it.
    narrative_uniis = set(re.findall(r"\b[0-9A-Z]{10}\b", "".join(s.text for s in doc.sections) + doc.title))
    gold_uniis = {i.unii for p in doc.products for i in p.active} | {u for p in doc.products for u, _ in p.inactive}
    assert (gold_uniis & set(re.findall(r"\b[0-9A-Z]{10}\b", text))) <= narrative_uniis
