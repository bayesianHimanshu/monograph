"""The H1 ontology check on hand-written good and bad queries, one or more per rule, plus the false positives
the design knowingly accepts; and the gate that runs it in front of run_sql."""

from __future__ import annotations

import inspect

import pytest

from monograph import wiring
from monograph.sqlcheck.catalog import Catalog, from_files
from monograph.sqlcheck.check import OntologyChecker
from monograph.sqlcheck.gate import MAX_EXEC_ERRORS, QueryGate
from monograph.tools.freeform import QueryOut
from monograph.tools.registry import ToolError

from .conftest import ROOT


@pytest.fixture(scope="module")
def cat() -> Catalog:
    g = wiring.generated_dir(ROOT)
    return from_files(g / "h1.warehouse.mapping.json", g / "h1.semantic.mapping.json")


@pytest.fixture(scope="module")
def wh(cat: Catalog) -> OntologyChecker:
    return OntologyChecker(cat, ("wh",))


@pytest.fixture(scope="module")
def sv(cat: Catalog) -> OntologyChecker:
    return OntologyChecker(cat, ("wh", "sem"))


GOOD_WH = [
    "SELECT COUNT(DISTINCT p.PRD_SK) FROM T_PKG k JOIN T_PRD_MSTR p ON p.PRD_SK = k.PRD_SK"
    " JOIN T_ORG_MSTR o ON o.ORG_SK = k.LBLR_ORG_SK WHERE o.ORG_NM = 'Pfizer Laboratories Div Pfizer Inc'",
    "SELECT COUNT(DISTINCT p.PRD_SK) FROM T_PRD_MSTR p WHERE p.DSG_FRM_CD = 'C42998'",
    "SELECT COUNT(*) FROM T_PRD_MSTR p JOIN T_REF_CD c ON c.CD_VAL = p.DSG_FRM_CD AND c.CD_DOM = 'DSG_FRM'"
    " WHERE c.CD_DSC = 'TABLET'",
    "SELECT COUNT(*) FROM T_PRD_MSTR p WHERE p.DSG_FRM_CD IN"
    " (SELECT CD_VAL FROM T_REF_CD WHERE CD_DOM = 'DSG_FRM' AND CD_DSC = 'TABLET')",
    "SELECT COUNT(*) FROM T_AE_HDR WHERE RCPT_DT BETWEEN 20240101 AND 20241231 AND SRS_FLG = 'Y'",
    "SELECT COUNT(DISTINCT a.AE_SK) FROM T_AE_HDR a JOIN T_AE_RXN x ON x.AE_SK = a.AE_SK WHERE x.RXN_TRM = 'Nausea'",
    "SELECT COUNT(DISTINCT RCL_NO) FROM T_RCL_PRD_DNRM WHERE RCL_CLS_CD = '2'",
    "SELECT SUM(i.STRNTH_VAL) FROM T_PRD_INGR i WHERE i.SUBST_SK = 5",
    "SELECT COUNT(*) FROM T_PRD_MSTR p WHERE EXISTS"
    " (SELECT 1 FROM T_PRD_RTE r WHERE r.PRD_SK = p.PRD_SK AND r.RTE_CD = 'C38288')",
    "WITH x AS (SELECT DISTINCT a.AE_SK FROM T_AE_HDR a JOIN T_AE_RXN r ON r.AE_SK = a.AE_SK) SELECT COUNT(*) FROM x",
    "SELECT COUNT(*) FROM (SELECT DISTINCT a.AE_SK FROM T_AE_HDR a JOIN T_AE_RXN r ON r.AE_SK = a.AE_SK) t",
    "SELECT p.PRD_CD, COUNT(*) AS n FROM T_PRD_MSTR p JOIN T_PKG k ON k.PRD_SK = p.PRD_SK GROUP BY p.PRD_CD",
    "SELECT CD_VAL, CD_DSC FROM T_REF_CD WHERE CD_DOM = 'RTE' AND CD_DSC ILIKE '%oral%'",
    "SELECT DISTINCT CD_DOM FROM T_REF_CD",
    "SELECT table_name FROM information_schema.columns WHERE table_schema = 'wh'",
    "SELECT RCPT_DT / 10000 AS yr, COUNT(*) AS n FROM T_AE_HDR GROUP BY yr",
    "SELECT COUNT(*) FROM T_AE_HDR WHERE to_date(RCPT_DT::text, 'YYYYMMDD') >= DATE '2024-01-01'",
    "SELECT * FROM T_PRD_MSTR LIMIT 5",
    # the moiety crosswalk, two references to one class
    "SELECT DISTINCT p.PRD_CD FROM X_SALT_MOIETY m JOIN T_PRD_INGR i ON i.SUBST_SK = m.SUBST_SK"
    " JOIN T_PRD_MSTR p ON p.PRD_SK = i.PRD_SK WHERE m.MOIETY_SUBST_SK = 12",
    # a business key joined to a denormalized copy of it
    "SELECT DISTINCT r.RCL_NO FROM T_RCL_PRD_DNRM r JOIN T_PRD_MSTR p ON p.PRD_CD = r.PRD_CD",
    # an embedded row and its nested bridge, by owner and position
    "SELECT COUNT(DISTINCT x.CLIN_DRG_SK) FROM T_AE_DRG d JOIN X_AE_DRG_RXN x ON x.AE_SK = d.AE_SK"
    " AND x.DRG_SEQ = d.DRG_SEQ WHERE d.DRG_ROLE_CD = 'PS'",
]

GOOD_SV = [
    "SELECT COUNT(DISTINCT p.product_ndc) FROM products p JOIN product_routes r ON r.product_ndc = p.product_ndc"
    " WHERE r.route = 'ORAL' AND p.dosage_form = 'TABLET'",
    "SELECT COUNT(*) FROM recalls WHERE recall_class = 'I'",
    "SELECT COUNT(*) FROM adverse_event_reports WHERE is_serious AND received_date >= DATE '2024-01-01'",
    "SELECT COUNT(DISTINCT p.product_ndc) FROM products p JOIN wh.T_PRD_MSTR m ON m.PRD_CD = p.product_ndc",
    "SELECT AVG(patient_age_years) FROM adverse_event_reports WHERE resulted_in_death",
    "SELECT COUNT(DISTINCT r.recall_number) FROM recall_products rp"
    " JOIN recalls r ON r.recall_number = rp.recall_number",
]

BAD = [
    # (search path, query, rule)
    ("wh", "SELECT COUNT(*) FROM T_PKG k JOIN T_PRD_MSTR p ON p.PRD_SK = k.PKG_SK", "join"),
    ("wh", "SELECT COUNT(*) FROM T_PKG k JOIN T_ORG_MSTR o ON o.ORG_SK = k.PRD_SK", "join"),
    ("wh", "SELECT COUNT(*) FROM T_PRD_MSTR p JOIN T_PKG k ON k.PRD_SK = p.PRD_CD", "join"),
    ("wh", "SELECT COUNT(*) FROM T_PRD_MSTR p JOIN T_SUBST_MSTR s ON s.SUBST_NM = p.PRD_NM", "join"),
    (
        "wh",
        "SELECT COUNT(*) FROM T_PRD_MSTR p JOIN T_REF_CD c ON c.CD_VAL = p.DSG_FRM_CD WHERE c.CD_DSC = 'TABLET'",
        "join",
    ),
    ("wh", "SELECT COUNT(*) FROM T_PRD_MSTR p JOIN T_REF_CD c ON c.CD_VAL = p.DSG_FRM_CD AND c.CD_DOM = 'RTE'", "join"),
    ("wh", "SELECT COUNT(*) FROM T_PRD_MSTR p, T_ORG_MSTR o WHERE o.ORG_NM = 'x'", "join"),
    ("wh", "SELECT COUNT(*) FROM T_PRD_MSTR p WHERE p.DSG_FRM_CD = 'TABLET'", "code"),
    ("wh", "SELECT COUNT(*) FROM T_PRD_RTE r WHERE r.RTE_CD IN ('C38288', 'ORAL')", "code"),
    ("wh", "SELECT COUNT(DISTINCT RCL_NO) FROM T_RCL_PRD_DNRM WHERE RCL_CLS_CD = 'II'", "code"),
    ("wh", "SELECT COUNT(*) FROM T_AE_HDR WHERE SRS_FLG = '1'", "code"),
    ("wh", "SELECT COUNT(*) FROM T_AE_HDR WHERE RCPT_DT >= '2024-01-01'", "code"),
    ("wh", "SELECT COUNT(*) FROM T_AE_HDR WHERE RCPT_DT >= 2024", "code"),
    ("wh", "SELECT COUNT(*) FROM T_AE_HDR WHERE EXTRACT(YEAR FROM RCPT_DT) = 2024", "code"),
    ("wh", "SELECT COUNT(*) FROM T_REF_CD WHERE CD_DOM = 'DSG_FRM' AND CD_DSC = 'Tablets'", "code"),
    (
        "wh",
        "SELECT COUNT(*) FROM T_AE_HDR a JOIN T_AE_RXN x ON x.AE_SK = a.AE_SK WHERE x.RXN_TRM = 'Nausea'",
        "cardinality",
    ),
    ("wh", "SELECT COUNT(*) FROM T_RCL_PRD_DNRM WHERE RCL_CLS_CD = '1'", "cardinality"),
    ("wh", "SELECT AVG(a.PT_AGE_YRS) FROM T_AE_HDR a JOIN T_AE_RXN x ON x.AE_SK = a.AE_SK", "cardinality"),
    ("wh", "SELECT COUNT(DISTINCT PRD_NM) FROM T_PRD_MSTR", "cardinality"),
    (
        "wh",
        "SELECT COUNT(k.PRD_SK) FROM T_PKG k JOIN T_ORG_MSTR o ON o.ORG_SK = k.LBLR_ORG_SK WHERE o.ORG_NM = 'x'",
        "cardinality",
    ),
    (
        "wh",
        "SELECT COUNT(*) FROM T_PRD_MSTR p JOIN T_PRD_RTE r ON r.PRD_SK = p.PRD_SK"
        " JOIN T_PRD_INGR i ON i.PRD_SK = p.PRD_SK",
        "cardinality",
    ),
    ("wh", "SELECT PRD_NAME FROM T_PRD_MSTR", "existence"),
    ("wh", "SELECT * FROM PRODUCTS", "existence"),
    ("wh", "SELECT p.FORM FROM T_PRD_MSTR p", "existence"),
    ("wh", "SELECT OLD_PRD_CD FROM T_PRD_MSTR", "existence"),
    ("wh", "SELECT COUNT(*) FROM products", "existence"),  # a view the warehouse context does not have
    ("wh", "SELECT COUNT(DISTINCT RECALL_NUMBER) FROM STG_RCL_FEED WHERE CLASSIFICATION = 'Class II'", "existence"),
    ("wh", "SELECT DISTINCT PRD_CD FROM STG_PRD_LOAD WHERE DSG_FRM_LGCY_CD = '120'", "existence"),
    ("wh", "SELECT DISTINCT k.PKG_NDC FROM T_PKG k WHERE k.LBLR_NM = 'Pfizer Inc'", "existence"),
    ("wh", "SELECT COUNT(*) FROM T_AE_HDR WHERE LD_DT BETWEEN 20140101 AND 20141231", "existence"),
    ("wh", "SELECT COUNT(*) FROM T_AE_HDR WHERE SRS_IND = 'Y'", "existence"),
    ("wh", "SELECT COUNT(*) FROM T_PRD_MSTR p JOIN T_LKP_CD l ON l.LKP_CD = p.DSG_FRM_CD", "existence"),
    ("sv", "SELECT COUNT(*) FROM products WHERE dosage_form = 'Tablet'", "code"),
    ("sv", "SELECT COUNT(*) FROM adverse_event_reports WHERE is_serious = 'Y'", "code"),
    ("sv", "SELECT COUNT(*) FROM recalls WHERE recall_class = '1'", "code"),
    (
        "sv",
        "SELECT COUNT(*) FROM recall_products rp JOIN recalls r ON r.recall_number = rp.recall_number",
        "cardinality",
    ),
    ("sv", "SELECT COUNT(*) FROM packages k JOIN products p ON p.product_ndc = k.package_ndc", "join"),
]

# Accepted false positives: queries that can be right for their question but that the check flags, by design.
ACCEPTED = [
    # counting reaction mentions on purpose: the check cannot tell that from counting reports
    ("wh", "SELECT COUNT(*) FROM T_AE_RXN WHERE RXN_TRM = 'Nausea'", "cardinality"),
    # matching a firm alias to a recall's denormalized firm name: a name join, not an ontology relation
    ("wh", "SELECT DISTINCT r.RCL_NO FROM T_RCL_PRD_DNRM r JOIN X_ORG_ALIAS a ON a.ALIAS_NM = r.FIRM_NM", "join"),
]


@pytest.mark.parametrize("query", GOOD_WH)
def test_good_warehouse_queries_pass(wh: OntologyChecker, query: str) -> None:
    r = wh.check(query)
    assert r.parsed
    assert r.violations == [], r.violations


@pytest.mark.parametrize("query", GOOD_SV)
def test_good_semantic_queries_pass(sv: OntologyChecker, query: str) -> None:
    r = sv.check(query)
    assert r.violations == [], r.violations


@pytest.mark.parametrize(("path", "query", "rule"), BAD + ACCEPTED)
def test_bad_queries_are_flagged_under_their_rule(
    wh: OntologyChecker, sv: OntologyChecker, path: str, query: str, rule: str
) -> None:
    r = (wh if path == "wh" else sv).check(query)
    assert rule in r.rules(), (query, r.violations)


def test_every_rule_has_bad_examples() -> None:
    assert {rule for _, _, rule in BAD} == {"join", "code", "cardinality", "existence"}


def test_feedback_is_ontology_language_and_suggests_valid_codes(wh: OntologyChecker) -> None:
    r = wh.check("SELECT COUNT(DISTINCT r.PRD_SK) FROM T_PRD_RTE r WHERE r.RTE_CD = 'ORAL'")
    (v,) = r.violations
    assert "Product.routes" in v.message
    assert "'C38288' (ORAL)" in v.message
    r = wh.check("SELECT COUNT(*) FROM T_AE_HDR a JOIN T_AE_RXN x ON x.AE_SK = a.AE_SK")
    assert "AdverseEventReport.report_id" in r.violations[0].message


def test_an_unparseable_query_is_not_blocked(wh: OntologyChecker) -> None:
    r = wh.check("SELEC nonsense FROM")
    assert not r.parsed
    assert r.ok


def test_the_checker_never_sees_a_question_or_gold() -> None:
    for fn in (OntologyChecker.__init__, OntologyChecker.check, QueryGate.__init__, QueryGate.run_sql):
        params = set(inspect.signature(fn).parameters)
        assert not params & {"question", "gold", "answer", "anchors"}, fn


# ----------------------------------------------------------------------------------------- the gate

SENTINEL = "SENTINEL-7f3a-GOLD"


def _executor(log: list[str]) -> object:
    def execute(query: str) -> QueryOut:
        log.append(query)
        if "boom" in query:
            raise ToolError("query error: syntax error at or near boom")
        return QueryOut(columns=["x"], rows=[[SENTINEL]])

    return execute


def test_enforce_blocks_then_runs_after_three_rounds(wh: OntologyChecker) -> None:
    ran: list[str] = []
    gate = QueryGate(wh, _executor(ran), "enforce")  # type: ignore[arg-type]
    bad = "SELECT COUNT(*) FROM T_PRD_MSTR WHERE DSG_FRM_CD = 'TABLET'"
    for n in (1, 2, 3):
        with pytest.raises(ToolError) as err:
            gate.run_sql(bad)
        assert f"repair round {n} of 3" in str(err.value)
        assert SENTINEL not in str(err.value)  # the shadow result is never sent
    text = gate.run_sql(bad)  # the rounds are used up: executed as written, violations noted
    assert "the ontology check still reports" in text
    log = gate.log()
    assert [e["blocked"] for e in log] == [True, True, True, False]
    assert all(e["shadow"] and e["rows"] == [[SENTINEL]] for e in log[:3])
    assert len(ran) == 4  # three shadow executions and one real one


def test_shadow_mode_records_and_never_blocks(wh: OntologyChecker) -> None:
    gate = QueryGate(wh, _executor([]), "shadow")  # type: ignore[arg-type]
    text = gate.run_sql("SELECT COUNT(*) FROM T_PRD_MSTR WHERE DSG_FRM_CD = 'TABLET'")
    assert "ontology" not in text
    assert gate.log()[0]["violations"][0]["rule"] == "code"
    assert not gate.log()[0]["blocked"]


def test_execution_errors_get_three_retries(wh: OntologyChecker) -> None:
    gate = QueryGate(wh, _executor([]), "shadow")  # type: ignore[arg-type]
    for _ in range(MAX_EXEC_ERRORS):
        with pytest.raises(ToolError, match="boom"):
            gate.run_sql("SELECT boom FROM T_PRD_MSTR")
    with pytest.raises(ToolError, match="no execution retries left"):
        gate.run_sql("SELECT PRD_CD FROM T_PRD_MSTR")
    assert gate.retries_exhausted
