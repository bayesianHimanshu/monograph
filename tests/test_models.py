from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from monograph.core.models import (
    AdverseEventReport,
    DrugRole,
    Label,
    Product,
    ProductType,
    Route,
    SeriousnessCriterion,
    SourceSystem,
)
from monograph.core.refs import Ref
from monograph.core.rules import PydanticOnlyViolation, RuleViolation

B = "https://example.org/monograph/"


def _report(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "report_id": "10003301",
        "source_system": SourceSystem.OPENFDA_E2B,
        "receipt_date": date(2024, 1, 2),
        "serious": True,
        "seriousness_criteria": frozenset({"hospitalization"}),
        "drugs": [{"drug_name": "ASPIRIN", "role": "primary suspect"}],
        "reactions": [{"term": "Nausea"}],
    }
    data.update(changes)
    return data


def _error(cls: type[Any], data: dict[str, Any], **kw: Any) -> BaseException:
    with pytest.raises(ValidationError) as info:
        cls.model_validate(data, **kw)
    err = info.value.errors()[0]
    return err.get("ctx", {}).get("error", info.value)  # type: ignore[no-any-return]


def test_strict_mode_rejects_coercion() -> None:
    with pytest.raises(ValidationError):
        AdverseEventReport.model_validate(_report(serious=1))
    with pytest.raises(ValidationError):
        AdverseEventReport.model_validate(_report(receipt_date="2024-01-02"))


def test_valid_report() -> None:
    data = _report(seriousness_criteria=frozenset({SeriousnessCriterion.HOSPITALIZATION}))
    data["drugs"] = [{"drug_name": "ASPIRIN", "role": DrugRole.PRIMARY_SUSPECT}]
    report = AdverseEventReport.model_validate(data)
    assert report.serious


def test_rules_raise_rule_violation() -> None:
    data = _report(seriousness_criteria=frozenset())
    data["drugs"] = [{"drug_name": "ASPIRIN", "role": DrugRole.PRIMARY_SUSPECT}]
    err = _error(AdverseEventReport, data)
    assert isinstance(err, RuleViolation)
    assert err.rule_id == "serious-requires-criterion"

    data = _report(serious=False, seriousness_criteria=frozenset())
    data["drugs"] = [{"drug_name": "ASPIRIN", "role": DrugRole.CONCOMITANT}]
    err = _error(AdverseEventReport, data)
    assert isinstance(err, RuleViolation)
    assert err.rule_id == "has-suspect"


def test_pydantic_only_future_date_uses_context_clock() -> None:
    data = {
        "set_id": "0" * 8 + "-0000-0000-0000-" + "0" * 12,
        "version": 1,
        "effective_date": date(2026, 12, 1),
    }
    Label.model_validate(data, context={"today": date(2027, 1, 1)})
    err = _error(Label, data, context={"today": date(2026, 11, 26)})
    assert isinstance(err, PydanticOnlyViolation)
    assert err.constraint_id == "label-effective-date-not-future"


def test_pydantic_only_duplicate_substances() -> None:
    ingredient = {
        "substance": Ref(B + "substance/R16CO5Y76E"),
        "strength_value": Decimal(1),
        "strength_unit": "mg/1",
    }
    data = {
        "product_ndc": "0280-2000",
        "product_type": ProductType.HUMAN_OTC_DRUG,
        "finished": True,
        "name": "aspirin",
        "dosage_form": "TABLET",
        "routes": frozenset({Route.ORAL}),
        "ingredients": [ingredient, ingredient],
    }
    err = _error(Product, data)
    assert isinstance(err, PydanticOnlyViolation)


def test_any_suspect_role_satisfies_the_suspect_rule() -> None:
    for role in (DrugRole.PRIMARY_SUSPECT, DrugRole.SECONDARY_SUSPECT, DrugRole.SUSPECT):
        data = _report(seriousness_criteria=frozenset({SeriousnessCriterion.OTHER}))
        data["drugs"] = [{"drug_name": "X", "role": role}]
        AdverseEventReport.model_validate(data)


def test_safe_strings_reject_control_characters() -> None:
    data = _report(seriousness_criteria=frozenset({SeriousnessCriterion.OTHER}))
    data["drugs"] = [{"drug_name": "X", "role": DrugRole.SUSPECT}]
    data["reactions"] = [{"term": "Nausea\nand vomiting"}]
    AdverseEventReport.model_validate(data)  # newlines are fine in text
    data["reactions"] = [{"term": "Nausea\x00"}]
    with pytest.raises(ValidationError, match="control character U\\+0000"):
        AdverseEventReport.model_validate(data)
    data["reactions"] = [{"term": "Nausea"}]
    data["report_id"] = "10003301\n"
    with pytest.raises(ValidationError, match="control"):
        AdverseEventReport.model_validate(data)


def test_route_required_only_for_finished_rx_and_otc() -> None:
    base = {"product_ndc": "0280-2000", "name": "x", "dosage_form": "POWDER"}
    Product.model_validate({**base, "product_type": ProductType.BULK_INGREDIENT, "finished": False})
    Product.model_validate({**base, "product_type": ProductType.HUMAN_OTC_DRUG, "finished": False})
    err = _error(Product, {**base, "product_type": ProductType.HUMAN_PRESCRIPTION_DRUG, "finished": True})
    assert isinstance(err, RuleViolation)
    assert err.rule_id == "finished-rx-otc-requires-route"
