"""The committed H1 question set: shape, split, coverage by rule, analyst language, real fan-out traps, and (with
the local H1 database) every question's reference SQL on the warehouse reproducing its canonical gold."""

from __future__ import annotations

import os
import re
from collections import Counter

import psycopg
import pytest

from monograph.h1 import build, questions
from monograph.h1.score import result_matches, score_answer
from monograph.qa import h1_templates as t

from .conftest import ROOT

QS = questions.load(ROOT)


def test_sixty_per_type_fifteen_dev_and_ids_unique() -> None:
    by = Counter((q["type"], q["split"]) for q in QS)
    for qtype in t.TYPES:
        assert by[(qtype, "dev")] == t.DEV_PER_TYPE
        assert by[(qtype, "test")] == t.PER_TYPE - t.DEV_PER_TYPE
    assert len({q["id"] for q in QS}) == len(QS)
    assert len({q["text"] for q in QS}) == len(QS)


def test_coverage_is_tagged_by_the_rule() -> None:
    cov = questions.coverage(ROOT)
    for q in QS:
        tpl = t.TEMPLATE_BY_ID[q["template"]]
        assert q["coverage"] == ("inside" if t.inside(tpl, cov) else "outside"), q["id"]
    by_type = {qt: Counter(q["coverage"] for q in QS if q["type"] == qt) for qt in t.TYPES}
    assert by_type["Q1"]["outside"] == 0  # Q1 is the inside-coverage control
    assert by_type["Q3"]["inside"] < by_type["Q3"]["outside"]  # mostly outside
    assert by_type["Q4"]["inside"] == 0  # outside by definition


def test_questions_use_analyst_language() -> None:
    wh_names = re.compile(r"\b(T_[A-Z_]+|X_[A-Z_]+|[A-Z]+_(SK|CD|FLG|DT|NM))\b")
    view_names = re.compile(r"\b(sem\.|recall_products|adverse_event_reports|product_ndc|is_serious)\b")
    for q in QS:
        assert not wh_names.search(q["text"]), q["text"]
        assert not view_names.search(q["text"]), q["text"]


def test_every_fan_out_question_is_a_real_trap() -> None:
    for q in QS:
        if q["type"] == "Q5":
            assert "naive" in q
            assert abs(q["naive"] - q["gold"]["number"]) > max(0.01, 0.001 * abs(q["gold"]["number"])), q["id"]


def test_gold_shapes() -> None:
    for q in QS:
        g = q["gold"]
        if q["type"] == "Q6":
            assert g["kind"] == "none"
        elif g["kind"] == "values":
            assert 1 <= len(g["entities"]) <= t.MAX_VALUES
        else:
            assert g["kind"] == "number"
            assert g["number"] > 0


def test_identity_questions_name_their_crosswalk() -> None:
    for q in QS:
        if q["type"] == "Q3":
            assert q["identity"], q["id"]


def test_scoring() -> None:
    gold = {"kind": "values", "entities": [{"key": "acme", "names": ["Acme Inc"]}, {"key": "beta", "names": []}]}
    assert score_answer({"kind": "values", "values": ["ACME INC ", "beta"]}, gold)
    assert score_answer({"kind": "values", "values": ["acme", "Acme Inc", "beta"]}, gold)
    assert not score_answer({"kind": "values", "values": ["acme"]}, gold)
    assert not score_answer({"kind": "values", "values": ["acme", "beta", "gamma"]}, gold)
    assert score_answer({"kind": "number", "number": 47.06}, {"kind": "number", "number": 47.0556})
    assert not score_answer({"kind": "number", "number": 47.2}, {"kind": "number", "number": 47.0556})
    assert not score_answer({"kind": "number", "number": 12}, {"kind": "number", "number": 13})
    assert score_answer({"kind": "none"}, {"kind": "none"})
    assert score_answer({"kind": "values", "values": []}, {"kind": "none"})
    assert not score_answer({"kind": "number", "number": 0}, {"kind": "none"})
    assert result_matches(["n"], [[13]], {"kind": "number", "number": 13})
    assert result_matches(["a", "b"], [["x", "acme"], ["y", "beta"]], gold)
    assert result_matches(["n"], [[0]], {"kind": "none"})


def test_the_warehouse_reproduces_every_gold() -> None:
    """Needs the local H1 database (monograph h1 build); skipped where it is not reachable."""
    admin = os.environ.get("MONOGRAPH_AGE_DSN")
    if not admin:
        pytest.skip("MONOGRAPH_AGE_DSN is not set (the local H1 database)")
    try:
        conn = psycopg.connect(build.agent_dsn(admin, "wh"), connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip("the H1 warehouse database is not built here")
    with conn:
        for q in QS:
            columns, rows = questions.run_reference(conn, q["reference_sql"])
            assert result_matches(columns, rows, q["gold"]), q["id"]
