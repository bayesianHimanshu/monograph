"""H2.1 cells: the identity and description switches, generated from the H6 source (docs/h2_1/PLAN.md)."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from monograph.ablation.cells import (
    BASELINE,
    CELLS,
    IDENTITY_FIELDS,
    Cell,
    ablate_spec,
    cell_renderers,
)
from monograph.core.regex import check_pattern
from monograph.core.spec import Kind
from monograph.core.vocab import Vocabulary
from monograph.gen.age import Projection, edge_label
from monograph.gen.artifacts import AGE_FILE, DDL_FILE
from monograph.gen.ddl import child_table, column_name, table_name
from monograph.graph.dataset import RECALLING_FIRM_PREFIX, Dataset, DatasetBuilder, _Orgs, recalling_firm_slug
from monograph.tools.freeform import graph_schema_text, sql_schema_text
from monograph.wiring import artifact_builder, build_spec, h2_excluded

from .conftest import ROOT


@pytest.fixture(scope="module")
def files() -> dict[str, str]:
    return {name: render() for name, render in cell_renderers(build_spec(), check_pattern, h2_excluded()).items()}


def _ddl(files: dict[str, str], cell: Cell) -> str:
    return files[cell.artifact(DDL_FILE)]


def _age(files: dict[str, str], cell: Cell) -> Projection:
    return Projection.parse(files[cell.artifact(AGE_FILE)])


def _cell(identity: bool, descriptions: bool) -> Cell:
    return next(c for c in CELLS if c.identity is identity and c.descriptions is descriptions)


def _tables(ddl: str) -> set[str]:
    return set(re.findall(r"^CREATE TABLE (\w+)", ddl, re.MULTILINE))


def _columns(ddl: str) -> set[str]:
    out = set()
    for block in re.split(r"\n\n(?=CREATE TABLE )", ddl):
        m = re.search(r"CREATE TABLE (\w+) \(\n(.*?)\n\);", block, re.DOTALL)
        if m:
            out |= {f"{m.group(1)}.{line.split()[0]}" for line in m.group(2).splitlines() if "CONSTRAINT" not in line}
    return out


def test_four_cells_with_distinct_names_databases_and_roles() -> None:
    assert len({c.name for c in CELLS}) == len({c.database for c in CELLS}) == 4
    assert len({c.sql_role for c in CELLS} | {c.graph_role for c in CELLS}) == 8
    assert Cell(identity=True, descriptions=True) == BASELINE


def test_identity_on_is_the_h6_spec_itself() -> None:
    spec = build_spec()
    assert ablate_spec(spec, identity=True) is spec


def test_identity_off_removes_exactly_the_identity_fields() -> None:
    spec = build_spec()
    off = ablate_spec(spec, identity=False)
    before = {(c.cls.__name__, f.name) for c in spec.classes for f in c.fields}
    after = {(c.cls.__name__, f.name) for c in off.classes for f in c.fields}
    assert before - after == set(IDENTITY_FIELDS)
    assert after <= before
    assert [c.cls for c in off.classes] == [c.cls for c in spec.classes]


def test_the_baseline_cell_is_h2(files: dict[str, str]) -> None:
    generated = ROOT / "generated"
    assert _ddl(files, BASELINE) == (generated / DDL_FILE).read_text(encoding="utf-8")
    assert files[BASELINE.artifact(AGE_FILE)] == (generated / AGE_FILE).read_text(encoding="utf-8")


def test_every_cell_is_generated_and_drift_checked(files: dict[str, str]) -> None:
    built = artifact_builder(build_spec(), Vocabulary()).build().files
    for name, text in files.items():
        assert built[name] == text
        assert (ROOT / "generated" / name).read_text(encoding="utf-8") == text, f"{name} drifted"


@pytest.mark.parametrize("identity", [True, False])
def test_descriptions_off_is_names_and_types_only(files: dict[str, str], identity: bool) -> None:
    on, off = _cell(identity, True), _cell(identity, False)
    assert "COMMENT ON" in _ddl(files, on)
    assert "COMMENT ON" not in _ddl(files, off)
    p = _age(files, off)
    texts = [v.description for v in p.vertices] + [x.description for v in p.vertices for x in v.properties]
    texts += [s.description for e in p.edges for s in e.sources]
    texts += [x.description for e in p.edges for s in e.sources for x in s.properties]
    assert set(texts) == {""}
    # what the agents read: no description text survives in either store
    described = _age(files, on)
    phrases = {x.description for v in described.vertices for x in v.properties if len(x.description) > 25}
    for text in (sql_schema_text(_ddl(files, off)), graph_schema_text(p)):
        assert not [ph for ph in phrases if ph in text]


@pytest.mark.parametrize("identity", [True, False])
def test_the_description_switch_changes_nothing_else(files: dict[str, str], identity: bool) -> None:
    on, off = _cell(identity, True), _cell(identity, False)
    stripped = re.sub(r"\nCOMMENT ON [^\n]*? IS '(?:[^']|'')*';", "", _ddl(files, on))
    assert "COMMENT ON" not in stripped
    assert stripped == _ddl(files, off)

    def blank(p: Projection) -> Any:
        data = json.loads(p.render())
        for v in data["vertices"]:
            v["description"] = ""
            for x in v["properties"]:
                x["description"] = ""
        for e in data["edges"]:
            for s in e["sources"]:
                s["description"] = ""
                for x in s["properties"]:
                    x["description"] = ""
        return data

    assert blank(_age(files, on)) == json.loads(_age(files, off).render())


def _identity_names() -> tuple[set[str], set[str], set[str]]:
    """Tables, columns and edge labels that the identity fields generate."""
    spec = build_spec()
    tables, columns, edges = set(), set(), set()
    for cls_name, field_name in IDENTITY_FIELDS:
        c = spec.by_name(cls_name)
        f = c.field(field_name)
        if f.kind in (Kind.REF, Kind.EMBEDDED):
            edges.add(edge_label(f.prop.name))
        if f.kind is Kind.EMBEDDED or f.is_collection:
            owner = table_name(c.cls) if c.is_entity else None
            if owner is not None:
                tables.add(child_table(owner, f))
        elif c.is_entity:
            columns.add(f"{table_name(c.cls)}.{column_name(f)}")
    return tables, columns, edges


@pytest.mark.parametrize("descriptions", [True, False])
def test_identity_off_has_no_identity_tables_columns_or_edges(files: dict[str, str], descriptions: bool) -> None:
    on, off = _cell(True, descriptions), _cell(False, descriptions)
    tables, columns, edges = _identity_names()
    assert tables
    assert columns
    assert edges
    assert tables <= _tables(_ddl(files, on))
    assert not tables & _tables(_ddl(files, off))
    assert columns <= _columns(_ddl(files, on))
    assert not columns & _columns(_ddl(files, off))
    on_edges = {e.label for e in _age(files, on).edges}
    off_edges = {e.label for e in _age(files, off).edges}
    assert edges <= on_edges
    assert not edges & off_edges
    # nothing else changes: every other edge label and vertex label is the same
    assert on_edges - off_edges == edges
    removed = {"OrganizationAlias"}  # an embedded value that only an identity field holds
    assert {v.label for v in _age(files, on).vertices} - {v.label for v in _age(files, off).vertices} == removed


def test_identity_off_leaves_no_identity_text(files: dict[str, str]) -> None:
    # The one survivor is RxNorm's own term-type vocabulary ("SCD generic clinical drug"), which describes
    # what a concept is, not a link between concepts.
    term_type = build_spec().by_name("ClinicalDrug").field("term_type").description or ""
    assert "generic" in term_type
    for cell in CELLS:
        if not cell.identity:
            text = (_ddl(files, cell) + _age(files, cell).render()).replace(term_type, "").lower()
            for word in ("ndc11", "moiety", "alias", "generic", "drug_mapping", "harmoniz", "crosswalk"):
                assert word not in text, (cell.name, word)


# ------------------------------------------------------------------------------- the dataset switch


def _builder() -> DatasetBuilder:
    spec = build_spec()
    return DatasetBuilder(spec, None, None, None, lambda _: None, identity=False)  # type: ignore[arg-type]


def test_identity_off_strips_the_layer_and_keeps_recalling_firms_apart() -> None:
    orgs = _Orgs()
    for product in ("0069-1", "0069-3"):
        orgs.see("0069", "Pfizer Laboratories Div Pfizer Inc", "ndc_directory", f"NDC directory, product {product}")
    orgs.see("0069", "PFIZER LABS", "ndc_directory", "NDC directory, product 0069-2")
    orgs.settle()
    base = "https://example.org/monograph/"
    products: list[dict[str, Any]] = [
        {"product_ndc": "0069-1", "drug_mappings": [{"x": 1}], "clinical_drug": base + "clinical-drug/1"}
    ]
    packages: list[dict[str, Any]] = [{"ndc": "0069-1-01", "ndc11": "00069000101"}]
    substances: list[dict[str, Any]] = [{"unii": "A", "active_moiety": {"moiety": base + "substance/B"}}, {"unii": "B"}]
    clinical: list[dict[str, Any]] = [{"rxcui": "1", "generic": {"drug": base + "clinical-drug/2"}}, {"rxcui": "2"}]
    recalls: list[dict[str, Any]] = [
        {
            "recall_number": "D-1",
            "recalling_firm": base + "organization/x",
            "_firm": "Pfizer Laboratories Div Pfizer Inc",
        },
        {
            "recall_number": "D-2",
            "recalling_firm": base + "organization/x",
            "_firm": "PFIZER LABORATORIES DIV PFIZER INC",
        },
        {"recall_number": "D-3", "recalling_firm": base + "organization/y", "_firm": "Other Firm LLC"},
    ]
    ds = Dataset()
    out = _builder()._without_identity(ds, products, packages, substances, clinical, recalls, orgs)
    assert products == [{"product_ndc": "0069-1"}]
    assert packages == [{"ndc": "0069-1-01"}]
    assert substances == [{"unii": "A"}, {"unii": "B"}]
    assert clinical == [{"rxcui": "1"}, {"rxcui": "2"}]
    labeler = {
        "slug": "pfizer-laboratories-div-pfizer-inc",
        "name": "Pfizer Laboratories Div Pfizer Inc",
        "aliases": [],
    }
    assert out[0] == labeler
    firm = recalling_firm_slug("Pfizer Laboratories Div Pfizer Inc")
    assert firm == "recalling-firm-pfizer-laboratories-div-pfizer-inc"
    # two spellings of one firm in enforcement records share the H6 ingest key; never the labeler's IRI
    assert [r["recalling_firm"] for r in recalls] == [
        base + "organization/" + firm,
        base + "organization/" + firm,
        base + "organization/recalling-firm-other-firm-llc",
    ]
    assert all("_firm" not in r for r in recalls)
    assert {o["slug"] for o in out} == {labeler["slug"], firm, "recalling-firm-other-firm-llc"}
    assert all(o["aliases"] == [] for o in out)


def test_a_recalling_firm_slug_fits_the_model() -> None:
    from monograph.core.models import Organization

    long = "A" * 60 + " " + "B" * 80
    slug = recalling_firm_slug(long)
    assert slug.startswith(RECALLING_FIRM_PREFIX)
    assert len(slug) <= 120
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug)
    Organization(slug=slug, name=long)


@pytest.mark.skipif(not (ROOT / "data" / "raw" / "ndc").exists(), reason="the raw caches are not in this copy")
def test_the_real_identity_off_build() -> None:
    from monograph.ablation.data import build_cells_dataset

    on = build_cells_dataset(ROOT, identity=True)
    off = build_cells_dataset(ROOT, identity=False)
    assert on.rejected == off.rejected
    for cls_name in on.instances:
        a = {i.model_dump_json() for i in on.instances[cls_name]}
        if cls_name == "Organization":
            continue
        keys = {json.dumps(_key(i)) for i in off.instances[cls_name]}
        assert keys == {json.dumps(_key(i)) for i in on.instances[cls_name]}, cls_name
        assert a, cls_name
    for cls_name, field_name in IDENTITY_FIELDS:
        for i in off.instances.get(cls_name, []):
            assert getattr(i, field_name) in (None, [], frozenset()), (cls_name, field_name)
    for r in off.instances["AdverseEventReport"]:
        for d in r.drugs:  # type: ignore[attr-defined]
            assert d.clinical_drug is None
            assert not d.harmonized_clinical_drugs
    labelers_on = {p.labeler.iri for p in on.instances["PackagedProduct"]}  # type: ignore[attr-defined]
    labelers_off = {p.labeler.iri for p in off.instances["PackagedProduct"]}  # type: ignore[attr-defined]
    assert labelers_on == labelers_off
    firms = {r.recalling_firm.iri for r in off.instances["Recall"]}  # type: ignore[attr-defined]
    assert all(f.rsplit("/", 1)[1].startswith(RECALLING_FIRM_PREFIX) for f in firms)
    assert not firms & labelers_off


def _key(instance: Any) -> Any:
    meta = type(instance).__ont__
    return [type(instance).__name__, *(str(getattr(instance, k)) for k in meta.key)]
