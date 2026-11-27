"""`monograph h2 ...` commands."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer

from monograph import wiring

h2_app = typer.Typer(no_args_is_help=True, help="H2: graph vs relational, free-form vs tools.")
Root = Annotated[Path, typer.Option("--root", help="Repository root.")]

FRAME_FILE = Path("fixtures/h2/frame.json")


def _clients(root: Path) -> tuple[Any, Any, Any, Any]:
    import os

    import httpx

    from monograph.extract.sources.dailymed import DailyMedClient, SplCache
    from monograph.ingest.openfda import OpenFdaClient, RawCache
    from monograph.ingest.rxnav import RxNavCache, RxNavClient

    raw = RawCache(wiring.raw_dir(root))
    http = httpx.Client(timeout=60.0)
    openfda = OpenFdaClient(http, raw, os.environ.get("OPENFDA_API_KEY"), 0.3, time.sleep, time.monotonic)
    dailymed = DailyMedClient(http, SplCache(wiring.raw_dir(root) / "spl"), 0.3, time.sleep, time.monotonic)
    rxnav = RxNavClient(http, RxNavCache(wiring.raw_dir(root) / "rxnav"), 0.1, time.sleep, time.monotonic)
    return raw, openfda, dailymed, rxnav


@h2_app.command("fetch")
def fetch(root: Root = Path()) -> None:
    """Fetch the H2 frame (cached under data/raw/; only missing responses hit the network)."""
    from monograph.graph.frame import FrameConfig, FrameFetcher

    raw, openfda, dailymed, rxnav = _clients(root)
    base = [
        *raw.all("ndc"),
        *(
            r
            for p in sorted((wiring.raw_dir(root) / "ndc" / "search").glob("*.json"))
            for r in json.loads(p.read_text())
        ),
    ]
    config = FrameConfig()
    report = FrameFetcher(openfda, raw, dailymed.spl, rxnav, config, typer.echo).fetch(base)
    path = root / FRAME_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    out = {"config": asdict(config), **asdict(report)}
    out["network_calls"] = {
        "openfda": openfda.network_calls,
        "dailymed": dailymed.network_calls,
        "rxnav": rxnav.network_calls,
    }
    path.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    typer.echo(f"frame written to {FRAME_FILE}")


AGE_DSN = "postgresql://monograph:monograph@localhost:55433/monograph"
ROLE_PASSWORDS = {"h2_sql": "h2_sql", "h2_graph": "h2_graph"}  # local, read-only roles on a local container


def admin_dsn() -> str:
    import os

    return os.environ.get("MONOGRAPH_AGE_DSN", AGE_DSN)


def role_dsn(role: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(admin_dsn())
    host = parts.hostname or "localhost"
    netloc = f"{role}:{ROLE_PASSWORDS[role]}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def build_dataset(root: Path) -> Any:
    from monograph.core.instances import Instantiator
    from monograph.graph.dataset import DatasetBuilder, read_raw
    from monograph.validate.pydantic_side import PydanticValidator

    _, _, dailymed, rxnav = _offline_clients(root)
    spec = wiring.build_spec()
    builder = DatasetBuilder(spec, Instantiator(spec), PydanticValidator(wiring.today()), rxnav, dailymed.spl)
    return builder.build(read_raw(wiring.raw_dir(root)))


class _Offline:
    """An HTTP client that refuses: dataset building reads caches only."""

    def get(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"cache miss during an offline build: {args[0] if args else ''}")


def _offline_clients(root: Path) -> tuple[Any, Any, Any, Any]:
    from monograph.extract.sources.dailymed import DailyMedClient, SplCache
    from monograph.ingest.openfda import OpenFdaClient, RawCache
    from monograph.ingest.rxnav import RxNavCache, RxNavClient

    http: Any = _Offline()
    raw = RawCache(wiring.raw_dir(root))
    openfda = OpenFdaClient(http, raw, None, 0.0, time.sleep, time.monotonic)
    dailymed = DailyMedClient(http, SplCache(wiring.raw_dir(root) / "spl"), 0.0, time.sleep, time.monotonic)
    rxnav = RxNavClient(http, RxNavCache(wiring.raw_dir(root) / "rxnav"), 0.0, time.sleep, time.monotonic)
    return raw, openfda, _CachedSpl(dailymed, root), rxnav


class _CachedSpl:
    def __init__(self, client: Any, root: Path) -> None:
        self._dir = wiring.raw_dir(root) / "spl"

    def spl(self, set_id: str) -> bytes | None:
        path = self._dir / f"{set_id}.xml"
        return path.read_bytes() if path.exists() else None


@h2_app.command("load")
def load(root: Root = Path()) -> None:
    """Build the dataset from the caches, load it into Postgres (schema h2) and project it into AGE."""
    import psycopg

    from monograph.core.vocab import Vocabulary
    from monograph.gen.age import Projection
    from monograph.gen.artifacts import AGE_FILE, DDL_FILE
    from monograph.graph.loader import GraphLoader, RelationalLoader, create_roles

    ds = build_dataset(root)
    summary = ds.summary()
    typer.echo(json.dumps(summary["instances"]))
    spec = wiring.build_spec()
    ddl = (wiring.generated_dir(root) / DDL_FILE).read_text(encoding="utf-8")
    projection = Projection.parse((wiring.generated_dir(root) / AGE_FILE).read_text(encoding="utf-8"))
    with psycopg.connect(admin_dsn(), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS age")
        rows = RelationalLoader(conn, spec, wiring.mapper(spec, Vocabulary()), ddl).load(ds.instances, typer.echo)
        vertices, edges = GraphLoader(conn, projection).load(typer.echo)
        create_roles(conn, projection.graph, ROLE_PASSWORDS)
    writer = wiring.results_writer(root, "H2")
    writer.write("h2.dataset", sum(summary["instances"].values()), summary)
    writer.write("h2.load", sum(rows.values()), {"rows": rows, "vertices": vertices, "edges": edges})
    typer.echo(f"loaded: {sum(rows.values())} rows, {sum(vertices.values())} vertices, {sum(edges.values())} edges")


def tool_service(store: str, root: Path) -> Any:
    """The shared tool service over one store, connected with that store's read-only role."""
    import psycopg

    from monograph.gen.age import Projection
    from monograph.gen.artifacts import AGE_FILE
    from monograph.tools.cypher_backend import CypherBackend
    from monograph.tools.service import ToolService
    from monograph.tools.sql_backend import SqlBackend

    projection = Projection.parse((wiring.generated_dir(root) / AGE_FILE).read_text(encoding="utf-8"))
    if store == "sql":
        return ToolService(SqlBackend(psycopg.connect(role_dsn("h2_sql"), autocommit=True), projection))
    if store == "graph":
        return ToolService(CypherBackend(psycopg.connect(role_dsn("h2_graph"), autocommit=True), projection))
    raise typer.BadParameter("store must be sql or graph")


@h2_app.command("mcp")
def mcp(
    store: Annotated[str, typer.Option(help="sql or graph")] = "graph",
    root: Root = Path(),
) -> None:
    """Serve the tool registry over MCP (stdio), backed by one store."""
    import anyio

    from monograph.tools.mcp_server import serve_stdio

    service = tool_service(store, root)
    anyio.run(serve_stdio, service)


QUESTIONS_FILE = Path("fixtures/h2/questions.json")
QUESTION_SEED = 20261127


@h2_app.command("questions")
def questions(root: Root = Path()) -> None:
    """Generate the question set and its gold (checked through the tools on both stores)."""
    from monograph.qa.generate import generate, recheck
    from monograph.qa.index import Index

    ds = build_dataset(root)
    ix = Index.build(ds.instances)
    qs, report = generate(ix, tool_service("sql", root), QUESTION_SEED, typer.echo)
    bad = recheck(ix, qs, tool_service("graph", root), QUESTION_SEED)
    typer.echo(f"recheck on the graph store: {len(bad)} disagreements")
    if bad:
        typer.echo(f"the stores disagree on {bad[:10]}", err=True)
        raise typer.Exit(1)
    counts: dict[str, dict[str, int]] = {}
    for q in qs:
        counts.setdefault(q["type"], {"dev": 0, "test": 0})[q["split"]] += 1
    out = {"seed": QUESTION_SEED, "dataset": ds.summary()["instances"], "counts": counts, "questions": qs}
    path = root / QUESTIONS_FILE
    path.write_text(json.dumps(out, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    from dataclasses import asdict as _asdict

    wiring.results_writer(root, "H2").write("h2.questions", len(qs), {"counts": counts, **_asdict(report)})
    typer.echo(f"{len(qs)} questions written to {QUESTIONS_FILE}: {counts}")


H2_RESPONSES = Path("data/h2/responses")


def load_questions(root: Path, split: str) -> list[dict[str, Any]]:
    data = json.loads((root / QUESTIONS_FILE).read_text(encoding="utf-8"))
    return [q for q in data["questions"] if q["split"] == split]


def known_ids(store_dsn: str) -> set[str]:
    import psycopg

    tables = (
        "substance",
        "organization",
        "clinical_drug",
        "product",
        "packaged_product",
        "label",
        "adverse_event_report",
        "recall",
    )
    with psycopg.connect(store_dsn) as conn:
        conn.execute("SET search_path = h2")
        return {str(r[0]) for t in tables for r in conn.execute(f"SELECT iri FROM {t}").fetchall()}


def make_arm_factory(arm: str, root: Path) -> Any:
    import psycopg

    from monograph.agent.arms import free_arm, tools_arm
    from monograph.gen.age import Projection
    from monograph.gen.artifacts import AGE_FILE, DDL_FILE
    from monograph.tools.freeform import CypherRunner, SqlRunner

    projection = Projection.parse((wiring.generated_dir(root) / AGE_FILE).read_text(encoding="utf-8"))
    ddl = (wiring.generated_dir(root) / DDL_FILE).read_text(encoding="utf-8")

    def make() -> Any:
        if arm == "S-free":
            return free_arm(arm, SqlRunner(psycopg.connect(role_dsn("h2_sql"), autocommit=True), ddl))
        if arm == "G-free":
            return free_arm(arm, CypherRunner(psycopg.connect(role_dsn("h2_graph"), autocommit=True), projection))
        return tools_arm(arm, tool_service("sql" if arm == "S-tools" else "graph", root))

    return make


@h2_app.command("run")
def run(
    arm: Annotated[str, typer.Option(help="S-free, S-tools, G-free or G-tools")],
    model: Annotated[str, typer.Option(help="claude-sonnet-5-5 or claude-haiku-5-5")],
    split: Annotated[str, typer.Option(help="dev or test")] = "dev",
    limit: Annotated[int | None, typer.Option(help="Only the first N questions (smoke runs).")] = None,
    workers: Annotated[int, typer.Option(help="Questions run concurrently.")] = 4,
    root: Root = Path(),
) -> None:
    """Run one arm with one model on one split (responses cached; results appended to results/h2)."""
    import anthropic

    from monograph.agent.arms import ARMS
    from monograph.agent.llm import AnthropicToolClient, CachedToolClient, ToolResponseCache
    from monograph.extract.config import MODELS
    from monograph.qa.run import Writer, run_split

    if arm not in ARMS or model not in MODELS or split not in ("dev", "test"):
        raise typer.BadParameter("unknown arm, model or split")
    if split == "test":
        drift = h2_frozen_drift(root)
        if drift:
            typer.echo(f"refusing the test run: PREREG frozen files changed or missing: {drift}", err=True)
            raise typer.Exit(1)
    qs = load_questions(root, split)[:limit] if limit else load_questions(root, split)
    client = CachedToolClient(
        AnthropicToolClient(anthropic.Anthropic(max_retries=8)), ToolResponseCache(root / H2_RESPONSES)
    )
    writer = wiring.results_writer(root, "H2")
    records = run_split(
        qs,
        arm,
        model,
        split,
        make_arm_factory(arm, root),
        client,
        Writer(writer.write),
        known_ids(role_dsn("h2_sql")),
        workers,
        log=typer.echo,
    )
    acc = sum(r["correct"] for r in records) / len(records) if records else 0.0
    cost = sum(float(r["cost_usd"]) for r in records if not r["from_cache"])
    typer.echo(
        f"{arm} {model} {split}: {len(records)} questions, accuracy {acc:.3f}, {client.network_calls} API calls,"
        f" new spend USD {cost:.2f}"
    )


def h2_frozen_drift(root: Path) -> list[str]:
    """Files listed with a sha256 in docs/h2/PREREG.md whose content changed (or the PREREG is missing)."""
    import hashlib
    import re

    prereg = root / "docs" / "h2" / "PREREG.md"
    if not prereg.exists():
        return ["docs/h2/PREREG.md"]
    text = prereg.read_text(encoding="utf-8")
    out = []
    for path, digest in re.findall(r"^\| `([^`]+)` \| `([0-9a-f]{64})` \|$", text, re.MULTILINE):
        target = root / path
        if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            out.append(path)
    return out


def current_hashes(root: Path) -> dict[str, tuple[str, str]]:
    """arm -> (system prompt sha256, tools sha256) for the prompts and tools as they are now."""
    from monograph.agent.arms import ARMS, free_arm, sha256, system_prompt, tools_arm
    from monograph.gen.age import Projection
    from monograph.gen.artifacts import AGE_FILE
    from monograph.tools.freeform import CypherRunner, SqlRunner
    from monograph.tools.service import ToolService

    projection = Projection.parse((wiring.generated_dir(root) / AGE_FILE).read_text(encoding="utf-8"))
    arms = {
        "S-free": free_arm("S-free", SqlRunner(None, "")),  # type: ignore[arg-type]
        "G-free": free_arm("G-free", CypherRunner(None, projection)),  # type: ignore[arg-type]
        "S-tools": tools_arm("S-tools", ToolService(None)),  # type: ignore[arg-type]
        "G-tools": tools_arm("G-tools", ToolService(None)),  # type: ignore[arg-type]
    }
    return {a: (sha256(system_prompt(a)), arms[a].tools_hash()) for a in ARMS}


def _records(root: Path, split: str) -> list[dict[str, Any]]:
    from monograph.harness.results import read_results
    from monograph.qa.report import select_runs

    return select_runs(read_results(root / "results" / "h2"), split, current_hashes(root))


@h2_app.command("score")
def score(split: Annotated[str, typer.Option(help="dev or test")] = "test", root: Root = Path()) -> None:
    """Score the latest runs (current prompts and tools) and append the summary to results/h2."""
    from monograph.extract.config import MODELS
    from monograph.qa.report import ARMS, score_lines

    records = _records(root, split)
    scores = score_lines(records, MODELS)
    wiring.results_writer(root, "H2").write(f"h2.score.{split}", len(records), scores)
    for m in MODELS:
        for g in ("T1", "T2", "T3", "T4", "T5", "T6", "core", "all"):
            row = [scores["cells"].get(f"{m}|{a}|{g}") for a in ARMS]
            if all(row):
                typer.echo(
                    f"{m} {g:4s} " + "  ".join(f"{a} {c['accuracy']:.2f}" for a, c in zip(ARMS, row, strict=True) if c)
                )
    if scores["verdict"]:
        typer.echo(f"verdict: {scores['verdict']['verdict']}")


@h2_app.command("estimate")
def estimate(root: Root = Path()) -> None:
    """Estimate the test cost from the dev runs (current prompts): mean USD per question x test questions."""
    from decimal import Decimal

    from monograph.extract.config import MODELS
    from monograph.qa.report import ARMS

    records = _records(root, "dev")
    n_test = len(load_questions(root, "test"))
    total = Decimal(0)
    for m in MODELS:
        for a in ARMS:
            rs = [r for r in records if r["model"] == m and r["arm"] == a]
            if not rs:
                typer.echo(f"{m} {a}: no dev runs with the current prompt")
                continue
            per = sum((Decimal(r["cost_usd"]) for r in rs), Decimal(0)) / len(rs)
            total += per * n_test
            typer.echo(f"{m} {a}: USD {per:.4f} per question x {n_test} = {per * n_test:.2f}")
    typer.echo(f"estimated test cost: USD {total:.2f} (approval threshold USD 40)")
    wiring.results_writer(root, "H2").write("h2.estimate", float(total), {"test_questions": n_test})


RESULTS_FILE = Path("docs/h2/RESULTS.md")


def report_h2(root: Path) -> None:
    from decimal import Decimal

    from monograph.extract.config import MODELS
    from monograph.qa.pov import pov_section
    from monograph.qa.report import render, score_lines

    records = _records(root, "test")
    questions = {
        r["question_id"]: {"type": r["type"], "text": r.get("question", ""), "gold": r.get("gold", {"kind": "none"})}
        for r in records
    }
    scores = score_lines(records, MODELS)
    spend = sum(Decimal(r["cost_usd"]) for r in records)
    meta = {"test_spend": float(spend), "pov": pov_section(scores, records, MODELS)}
    text = render(scores, records, questions, MODELS, meta)
    path = root / RESULTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    typer.echo(f"wrote {RESULTS_FILE} ({len(records)} test records)")


PREREG_FILE = Path("docs/h2/PREREG.md")
LIMITATIONS = (
    "Dev questions are only 60 (10 per type), so prompt tuning had little signal; the same budget applied to"
    " every arm, and the dev log above shows each arm's iterations.",
    "The frame is targeted: recalls that name products, their NDC records, and events sampled by RxCUI. It is"
    " connected by construction and is not a random sample of FDA data.",
    "Organization identity is by NDC labeler code (plus a recall's own product): firms that share a parent but"
    " not a labeler code stay separate, and a recalling firm that is not a labeler of the recalled product"
    " stays its own organization.",
    "Tool results and query results are capped at 200 rows; questions whose tool plan would hit a cap were not"
    " generated, so no question needs more than the cap on any arm.",
    "Gold for 'a report involves a product' follows openFDA's harmonization of the reported drug to RxCUIs,"
    " which is broad (every strength and brand of the reported ingredient); it is the defined relation, not a"
    " clinical judgement.",
    "AGE limitations found on dev are listed in the report; the G-free arm meets them, the G-tools backend"
    " works around them.",
    "Dev accuracy was at or near the ceiling for every arm and both models (0.97 to 1.00 after the gold fix),"
    " so no arm's prompt was tuned: all four ran prompt v1 on dev and test. The question set may not separate"
    " the arms; that is a property of these templates, not a reason to change them after seeing dev.",
    "One gold fix on dev: an alias that names a second firm in the data ('Azurity Pharmaceuticals, Inc."
    " (formerly Arbor Pharmaceuticals)', while Arbor Pharmaceuticals is its own organization) made the gold"
    " ambiguous; such aliases are excluded for dev and test alike, and doubled periods in question text were"
    " removed. 23 questions changed; dev was rerun for every arm.",
)


def _dev_log(root: Path) -> list[dict[str, Any]]:
    from monograph.harness.results import read_results

    latest: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for line in read_results(root / "results" / "h2"):
        if line["metric"] != "h2.run" or line["details"].get("split") != "dev":
            continue
        d = line["details"]
        version = f"base {d['prompt_versions']['base']}, arm {d['prompt_versions'][d['arm']]}"
        latest[(d["arm"], d["model"], version, d["question_id"])] = d
    groups: dict[tuple[str, str, str], list[bool]] = {}
    runs: dict[tuple[str, str, str], int] = {}
    for (arm, model, version, _), d in latest.items():
        groups.setdefault((arm, model, version), []).append(bool(d["correct"]))
    for line in read_results(root / "results" / "h2"):
        if line["metric"] == "h2.run" and line["details"].get("split") == "dev":
            d = line["details"]
            version = f"base {d['prompt_versions']['base']}, arm {d['prompt_versions'][d['arm']]}"
            runs[(d["arm"], d["model"], version)] = runs.get((d["arm"], d["model"], version), 0) + 1
    return [
        {"arm": a, "model": m, "version": v, "n": runs[(a, m, v)], "accuracy": sum(xs) / len(xs)}
        for (a, m, v), xs in sorted(groups.items())
    ]


@h2_app.command("prereg")
def prereg(root: Root = Path()) -> None:
    """Write docs/h2/PREREG.md from the code, the question set and the dev log."""
    from monograph.qa.prereg import render_prereg

    data = json.loads((root / QUESTIONS_FILE).read_text(encoding="utf-8"))
    text = render_prereg(root, current_hashes(root), _dev_log(root), data["dataset"], data["counts"], LIMITATIONS)
    (root / PREREG_FILE).write_text(text, encoding="utf-8")
    typer.echo(f"wrote {PREREG_FILE}; frozen drift now: {h2_frozen_drift(root)}")
