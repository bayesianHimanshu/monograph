"""Build the H1 databases: one per context, holding exactly what the arms in that context may see.

Postgres catalogs (`pg_class`, view definitions, comments) are readable by every role in a database, whatever
its schema privileges. So the contexts do not share a database:

* `monograph_h1_wh` (W-raw, ONT, ONT+OC): schema `wh` only;
* `monograph_h1_sv` (SV, SV+OC): schemas `wh` and `sem`.

Each is built the same way: the canonical tables (schema `h2`) loaded from the validated instances exactly as
H2 and H2.1 load them; the warehouse loaded from them by the generated SQL; the round trip (every canonical
table rebuilt from the warehouse equals the canonical table, row for row; the build aborts otherwise); in the
SV database the semantic views, checked against the canonical tables too; then `h2` and the round-trip views
are dropped, and one read-only agent role is created that may connect to that database only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import psycopg
from psycopg import sql
from pydantic import BaseModel

from monograph import wiring
from monograph.gen import semantic_layer as s
from monograph.gen import warehouse as w

# The generated artifacts a database is built from (and that the pins hold); their hash is the build stamp.
ARTIFACTS: tuple[str, ...] = (
    w.MAPPING_FILE,
    w.DDL_FILE,
    w.LOAD_FILE,
    w.INVERSE_FILE,
    s.MAPPING_FILE,
    s.MODEL_FILE,
    s.VIEWS_FILE,
)
STAMP_SCHEMA = "h1_admin"  # not granted to any agent role

Context = Literal["wh", "sv"]
CONTEXTS: tuple[Context, ...] = ("wh", "sv")
PREFIX = "monograph_h1"
STATEMENT_TIMEOUT = "10s"


def database(context: Context, prefix: str = PREFIX) -> str:
    return f"{prefix}_{context}"


def role(context: Context, prefix: str = PREFIX) -> str:
    return f"{prefix.removeprefix('monograph_')}_{context}_agent"


def schemas(context: Context) -> tuple[str, ...]:
    return (w.SCHEMA, s.SCHEMA) if context == "sv" else (w.SCHEMA,)


@dataclass(frozen=True, slots=True)
class BuildReport:
    warehouse_rows: dict[str, int]
    round_trip: dict[str, int]  # canonical table -> rows compared
    semantic_checks: dict[str, int]  # semantic view -> rows compared


def generated(root: Path, name: str) -> str:
    return (wiring.generated_dir(root) / name).read_text(encoding="utf-8")


def stamp(texts: Sequence[bytes]) -> str:
    """The build stamp: sha256 over the artifacts, each prefixed by its length."""
    import hashlib

    h = hashlib.sha256()
    for t in texts:
        h.update(len(t).to_bytes(8, "big") + t)
    return h.hexdigest()


def live_stamp(root: Path) -> str:
    return stamp([(wiring.generated_dir(root) / n).read_bytes() for n in ARTIFACTS])


def database_stamp(admin: str, context: Context, prefix: str = PREFIX) -> str | None:
    from monograph.ablation.load import with_database

    try:
        with psycopg.connect(with_database(admin, database(context, prefix)), connect_timeout=5) as conn:
            row = conn.execute(f"SELECT sha256 FROM {STAMP_SCHEMA}.build").fetchone()
    except psycopg.Error:
        return None
    return str(row[0]) if row else None


def _one(conn: psycopg.Connection[Any], query: str) -> int:
    row = conn.execute(query.encode("utf-8")).fetchone()
    return int(row[0]) if row else 0


def _diff(conn: psycopg.Connection[Any], a: str, b: str) -> tuple[int, int]:
    q = "SELECT count(*) FROM (({}) EXCEPT ALL ({})) x"
    return _one(conn, q.format(a, b)), _one(conn, q.format(b, a))


def round_trip_differences(
    conn: psycopg.Connection[Any], views: Sequence[w.EqView]
) -> tuple[dict[str, int], list[str]]:
    """(rows compared per table, differences). Compared as multisets, both ways."""
    rows: dict[str, int] = {}
    problems: list[str] = []
    for v in views:
        a, b = f"SELECT * FROM {w.EQ_WH}.{v.name}", f"SELECT * FROM {w.EQ_H2}.{v.name}"
        extra, missing = _diff(conn, a, b)
        rows[v.name] = _one(conn, f"SELECT count(*) FROM {w.EQ_H2}.{v.name}")
        if extra or missing:
            problems.append(f"{v.name}: {extra} rows only in the warehouse, {missing} only in the canonical tables")
    return rows, problems


# Each semantic view against the canonical tables it must show (as the round-trip views key them).
SEMANTIC_CHECKS: dict[str, tuple[str, str]] = {
    "products": (
        "SELECT product_ndc, product_name, dosage_form, product_type, is_finished FROM sem.products",
        "SELECT product_ndc, name, dosage_form, product_type, finished FROM h1_eq_h2.product",
    ),
    "product_routes": (
        "SELECT product_ndc, route FROM sem.product_routes",
        "SELECT product_ndc, unnest(routes) FROM h1_eq_h2.product",
    ),
    "product_ingredients": (
        "SELECT product_ndc, substance_unii, strength_value, strength_unit FROM sem.product_ingredients",
        "SELECT owner__product_ndc, substance__unii, strength_value, strength_unit FROM h1_eq_h2.product__ingredients",
    ),
    "substances": (
        "SELECT substance_unii, substance_name FROM sem.substances",
        "SELECT unii, name FROM h1_eq_h2.substance",
    ),
    "firms": ("SELECT firm_code, firm_name FROM sem.firms", "SELECT slug, name FROM h1_eq_h2.organization"),
    "packages": (
        "SELECT package_ndc, product_ndc, labeler_firm_code, package_description FROM sem.packages",
        "SELECT ndc, product__product_ndc, labeler__slug, description FROM h1_eq_h2.packaged_product",
    ),
    "adverse_event_reports": (
        "SELECT report_id, source_system, received_date, is_serious, patient_age_years,"
        " resulted_in_death, caused_hospitalization FROM sem.adverse_event_reports",
        "SELECT report_id, source_system, receipt_date, serious, patient_age_years,"
        " 'death' = ANY(seriousness_criteria), 'hospitalization' = ANY(seriousness_criteria)"
        " FROM h1_eq_h2.adverse_event_report",
    ),
    "adverse_event_reactions": (
        "SELECT report_id, reaction_term FROM sem.adverse_event_reactions",
        "SELECT owner__report_id, term FROM h1_eq_h2.adverse_event_report__reactions",
    ),
    "adverse_event_drugs": (
        "SELECT report_id, drug_name, drug_role FROM sem.adverse_event_drugs",
        "SELECT owner__report_id, drug_name, role FROM h1_eq_h2.adverse_event_report__drugs",
    ),
    "recalls": (
        "SELECT recall_number, recall_class, reason, report_date, recalling_firm_code FROM sem.recalls",
        "SELECT recall_number, classification, reason, report_date, recalling_firm__slug FROM h1_eq_h2.recall",
    ),
    "recall_products": (
        "SELECT recall_number, product_ndc FROM sem.recall_products",
        "SELECT owner__recall_number, target__product_ndc FROM h1_eq_h2.recall__products",
    ),
}


def semantic_differences(conn: psycopg.Connection[Any]) -> tuple[dict[str, int], list[str]]:
    rows: dict[str, int] = {}
    problems: list[str] = []
    for name, (view, canonical) in SEMANTIC_CHECKS.items():
        extra, missing = _diff(conn, view, canonical)
        rows[name] = _one(conn, f"SELECT count(*) FROM ({view}) x")
        if extra or missing:
            problems.append(f"sem.{name}: {extra} rows only in the view, {missing} only in the canonical tables")
    return rows, problems


def build_warehouse(conn: psycopg.Connection[Any], root: Path) -> tuple[dict[str, int], dict[str, int]]:
    """Load `wh` from `h2` (already loaded) and prove the round trip. Raises on any difference."""
    for schema in (w.SCHEMA, w.EQ_WH, w.EQ_H2, s.SCHEMA):
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    conn.execute(generated(root, w.DDL_FILE).encode("utf-8"))
    conn.execute(generated(root, w.LOAD_FILE).encode("utf-8"))
    conn.execute(generated(root, w.INVERSE_FILE).encode("utf-8"))
    wh = w.analyze(wiring.build_spec())
    counts = {t.name: _one(conn, f"SELECT count(*) FROM {w.SCHEMA}.{t.name}") for t in wh.tables}
    rows, problems = round_trip_differences(conn, w.eq_views(wh))
    if problems:
        raise RuntimeError("the warehouse does not hold the canonical data: " + "; ".join(problems))
    return counts, rows


def load_canonical(
    conn: psycopg.Connection[Any], instances: Mapping[str, Sequence[BaseModel]], root: Path, log: Callable[[str], None]
) -> dict[str, int]:
    from monograph.core.vocab import Vocabulary
    from monograph.gen.artifacts import DDL_FILE
    from monograph.graph.loader import RelationalLoader

    spec = wiring.build_spec()
    counts = RelationalLoader(conn, spec, wiring.mapper(spec, Vocabulary()), generated(root, DDL_FILE)).load(
        instances, log
    )
    conn.execute("SET search_path = public")
    return counts


def ensure_database(admin: str, name: str) -> str:
    from monograph.ablation.load import with_database

    with psycopg.connect(admin, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name]).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return with_database(admin, name)


def agent_dsn(admin: str, context: Context, prefix: str = PREFIX) -> str:
    from monograph.ablation.load import with_database

    r = role(context, prefix)
    return with_database(admin, database(context, prefix), r, r)


def create_role(conn: psycopg.Connection[Any], context: Context, prefix: str = PREFIX) -> None:
    """One read-only role that reads this context's schemas and may connect to this database only."""
    name = role(context, prefix)
    r = sql.Identifier(name)
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [name]).fetchone():
        conn.execute(sql.SQL("DROP OWNED BY {}").format(r))
        conn.execute(sql.SQL("DROP ROLE {}").format(r))
    conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(r, sql.Literal(name)))
    conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(r))
    conn.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(r, sql.Literal(STATEMENT_TIMEOUT)))
    for schema in schemas(context):
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), r))
        conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(sql.Identifier(schema), r))
    conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    path = sql.SQL(", ").join(sql.Identifier(x) for x in schemas(context))
    conn.execute(sql.SQL("ALTER ROLE {} SET search_path = {}").format(r, path))
    db = sql.Identifier(database(context, prefix))
    conn.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(db))
    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(db, r))


def build_context(
    admin: str,
    context: Context,
    instances: Mapping[str, Sequence[BaseModel]],
    root: Path,
    log: Callable[[str], None],
    prefix: str = PREFIX,
) -> BuildReport:
    dsn = ensure_database(admin, database(context, prefix))
    with psycopg.connect(dsn, autocommit=True) as conn:
        load_canonical(conn, instances, root, log)
        counts, rows = build_warehouse(conn, root)
        checks: dict[str, int] = {}
        if context == "sv":
            conn.execute(generated(root, s.VIEWS_FILE).encode("utf-8"))
            checks, problems = semantic_differences(conn)
            if problems:
                raise RuntimeError("the semantic views do not show the canonical data: " + "; ".join(problems))
        for schema in ("h2", w.EQ_WH, w.EQ_H2):
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        create_role(conn, context, prefix)
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(STAMP_SCHEMA)))
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(STAMP_SCHEMA)))
        conn.execute(f"CREATE TABLE {STAMP_SCHEMA}.build (sha256 text NOT NULL, built_at timestamptz NOT NULL)")
        conn.execute(f"INSERT INTO {STAMP_SCHEMA}.build VALUES (%s, now())", [live_stamp(root)])
    log(
        f"{database(context, prefix)}: {sum(counts.values())} warehouse rows, round trip exact on {len(rows)} tables"
        + (f", {len(checks)} semantic views checked" if checks else "")
    )
    return BuildReport(counts, rows, checks)


def build(
    admin: str,
    instances: Mapping[str, Sequence[BaseModel]],
    root: Path,
    log: Callable[[str], None],
    prefix: str = PREFIX,
) -> dict[Context, BuildReport]:
    return {c: build_context(admin, c, instances, root, log, prefix) for c in CONTEXTS}
