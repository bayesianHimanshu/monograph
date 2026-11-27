"""H2 identity rules: 11-digit NDCs, firm-name normalization, organizations by labeler code, moieties."""

from __future__ import annotations

import pytest

from monograph.graph.dataset import _Orgs, _spl_strength_unit
from monograph.graph.identity import labeler_code, ndc11, normalize_firm
from monograph.graph.spl import moiety_facts

from .conftest import ROOT


@pytest.mark.parametrize(
    ("ndc", "expected"),
    [
        ("0069-3150-14", "00069315014"),
        ("12345-678-90", "12345067890"),
        ("12345-6789-1", "12345678901"),
        ("0069-3150", None),
        ("123-4567-89", None),
    ],
)
def test_ndc11_pads_the_short_segment(ndc: str, expected: str | None) -> None:
    assert ndc11(ndc) == expected


def test_labeler_code_is_the_first_segment() -> None:
    assert labeler_code("0069-3150-14") == "0069"


def test_firm_normalization_is_a_fixed_list_not_similarity() -> None:
    assert normalize_firm("Teva Pharmaceuticals USA, Inc.") == normalize_firm("TEVA PHARMACEUTICALS USA INC")
    assert normalize_firm("Johnson & Johnson Consumer Inc.") == "JOHNSON AND JOHNSON CONSUMER"
    assert normalize_firm("Pfizer Inc") != normalize_firm("Pfizer Laboratories Div Pfizer Inc")


def test_strength_unit_in_ndc_directory_style() -> None:
    assert _spl_strength_unit("mg", 1, "1") == "mg/1"
    assert _spl_strength_unit("mg", 1, "mL") == "mg/mL"
    assert _spl_strength_unit("mg", 5, "mL") == "mg/5mL"
    assert _spl_strength_unit(None, 1, "1") is None


def test_organizations_are_keyed_by_labeler_code_with_aliases() -> None:
    orgs = _Orgs()
    for _ in range(3):
        orgs.see("0093", "Teva Pharmaceuticals USA, Inc.", "ndc_directory", "NDC directory, product 0093-0001")
    orgs.see("0093", "TEVA PHARMACEUTICALS USA INC", "ndc_directory", "NDC directory, product 0093-0002")
    orgs.settle()
    slug = orgs.slug_for_code("0093")
    assert slug == "teva-pharmaceuticals-usa-inc"
    assert orgs.names[slug] == "Teva Pharmaceuticals USA, Inc."
    assert set(orgs.aliases[slug]) == {"TEVA PHARMACEUTICALS USA INC"}


def test_a_recalling_firm_joins_the_labeler_only_through_its_own_product() -> None:
    orgs = _Orgs()
    orgs.see("0093", "Teva Pharmaceuticals USA, Inc.", "ndc_directory", "NDC directory, product 0093-0001")
    orgs.see("0781", "Sandoz Inc", "ndc_directory", "NDC directory, product 0781-0001")
    orgs.settle()
    # Same normalized name and the recall names a Teva product: an alias of Teva.
    assert orgs.recalling_firm("Teva Pharmaceuticals USA Inc.", ["0093-0001"], "D-1-2020") == (
        "teva-pharmaceuticals-usa-inc"
    )
    assert orgs.aliases["teva-pharmaceuticals-usa-inc"]["Teva Pharmaceuticals USA Inc."][0] == "enforcement"
    # Same name but the recall names only a Sandoz product: not tied by an identifier, so its own firm.
    assert orgs.recalling_firm("Teva Pharmaceuticals USA Inc", ["0781-0001"], "D-2-2020") == (
        "teva-pharmaceuticals-usa-inc"
    )  # the H6 slug rule: an identical normalized key is the same organization
    # A different name: its own organization, as in H6.
    assert orgs.recalling_firm("Acme Repackaging LLC", ["0093-0001"], "D-3-2020") == "acme-repackaging-llc"
    assert orgs.names["acme-repackaging-llc"] == "Acme Repackaging LLC"


def test_moieties_come_from_the_spl_active_moiety_element() -> None:
    path = next((ROOT / "fixtures" / "h4" / "spl").glob("006f9a3f-*.xml"))
    facts = moiety_facts(path.read_bytes())
    pairs = {(f.name.upper(), f.moiety_name.upper()) for f in facts}
    assert ("AMPICILLIN SODIUM", "AMPICILLIN") in pairs
