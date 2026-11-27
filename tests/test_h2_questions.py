"""The H2 question set: structure, split, gold kinds, phrasings, and the gold relations on the mini dataset."""

from __future__ import annotations

import json
from collections import Counter

from monograph.core.meta import DEFAULT_BASE
from monograph.graph.identity import normalize_firm
from monograph.qa.generate import largest_remainder
from monograph.qa.index import Index
from monograph.qa.templates import TEMPLATES

from .conftest import ROOT
from .h2_support import mini_instances

B = DEFAULT_BASE


def _questions() -> list[dict[str, object]]:
    data = json.loads((ROOT / "fixtures" / "h2" / "questions.json").read_text(encoding="utf-8"))
    qs: list[dict[str, object]] = data["questions"]
    return qs


def test_300_questions_50_per_type_10_dev_each() -> None:
    qs = _questions()
    assert len(qs) == 300
    assert Counter(q["type"] for q in qs) == {f"T{i}": 50 for i in range(1, 7)}
    assert Counter(q["type"] for q in qs if q["split"] == "dev") == {f"T{i}": 10 for i in range(1, 7)}
    assert len({q["id"] for q in qs}) == 300


def test_gold_kinds_follow_the_type() -> None:
    for q in _questions():
        kind = q["gold"]["kind"]  # type: ignore[index]
        expected = {"T5": "number", "T6": "none"}.get(str(q["type"]), "ids")
        assert kind == expected, q["id"]
        if kind == "number":
            assert q["gold"]["number"] >= 1  # type: ignore[index]


def test_every_template_has_three_to_five_hand_written_phrasings() -> None:
    for t in TEMPLATES:
        assert 3 <= len(t.phrasings) <= 5, t.id
        assert len(set(t.phrasings)) == len(t.phrasings)
    used = Counter((q["template"], q["phrasing"]) for q in _questions())
    for t in TEMPLATES:
        assert {p for (tid, p) in used if tid == t.id} == set(range(len(t.phrasings)))


def test_alias_questions_name_the_firm_by_a_recorded_variant() -> None:
    qs = [q for q in _questions() if q["template"] in ("t3_alias_recalls", "t4_substances_recalled_by_alias")]
    assert qs
    from monograph.ingest.openfda import slugify

    differs = [q for q in qs if slugify(str(q["params"]["firm"])) != str(q["anchors"][0]).rsplit("/", 1)[1]]  # type: ignore[index]
    assert len(differs) >= 0.8 * len(qs)  # variants beyond case and punctuation come first


def test_largest_remainder() -> None:
    assert largest_remainder(10, [17, 17, 16]) == [4, 3, 3]
    assert sum(largest_remainder(10, [13, 13, 12, 12])) == 10


def test_gold_relations_on_the_mini_dataset() -> None:
    ix = Index.build(mini_instances())
    amp = f"{B}substance/7C782967RD"
    amp_na = f"{B}substance/JFN36L5S8K"
    assert ix.products_containing(amp) == set()
    assert ix.products_containing(amp, salts=True) == {f"{B}product/0069-1000", f"{B}product/0069-2000"}
    assert ix.firms_recalling(ix.products_containing(amp_na)) == {f"{B}organization/pfizer-laboratories-div-pfizer-inc"}
    assert ix.labelers_of({f"{B}product/12345-678"}) == {f"{B}organization/acme-repack-llc"}
    assert ix.generic[f"{B}clinical-drug/1001"] == f"{B}clinical-drug/1000"
    assert ix.products_mapped_to(f"{B}clinical-drug/1000") == {f"{B}product/0069-1000", f"{B}product/0069-2000"}
    assert ix.reports_involving({f"{B}product/0069-1000"}) == {f"{B}adverse-event-report/1001"}
    assert ix.reports_involving({f"{B}product/12345-678"}) == {f"{B}adverse-event-report/1002"}
    pfizer = ix.orgs[f"{B}organization/pfizer-laboratories-div-pfizer-inc"]
    assert {normalize_firm(a.name) for a in pfizer.aliases} == {normalize_firm(pfizer.name)}
