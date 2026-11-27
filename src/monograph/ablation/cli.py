"""`monograph h2_1 ...` commands (docs/h2_1/PLAN.md)."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer

from monograph import wiring

h2_1_app = typer.Typer(no_args_is_help=True, help="H2.1: identity and description ablations of the H2 arms.")
Root = Annotated[Path, typer.Option("--root", help="Repository root.")]

EXPERIMENT = "H2_1"
FRAME_FILE = Path("fixtures/h2_1/extension.json")


@h2_1_app.command("fetch")
def fetch(root: Root = Path()) -> None:
    """Fetch the high fan-out extension (cached under data/raw/h2_1/; only missing responses hit the network)."""
    import os

    import httpx

    from monograph.ablation.data import extension_dir
    from monograph.ablation.frame import ExtensionConfig, ExtensionFetcher
    from monograph.graph.cli import _clients
    from monograph.graph.dataset import read_raw
    from monograph.ingest.openfda import OpenFdaClient, RawCache

    _, _, _, rxnav = _clients(root)
    openfda = OpenFdaClient(
        httpx.Client(timeout=60.0),
        RawCache(extension_dir(root)),
        os.environ.get("OPENFDA_API_KEY"),
        0.3,
        time.sleep,
        time.monotonic,
    )
    config = ExtensionConfig()
    frame = read_raw(wiring.raw_dir(root)).ndc
    report = ExtensionFetcher(openfda, rxnav, config, typer.echo).fetch(frame)
    out = {
        "config": asdict(config),
        **asdict(report),
        "network_calls": {"openfda": openfda.network_calls, "rxnav": rxnav.network_calls},
    }
    path = root / FRAME_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    wiring.results_writer(root, EXPERIMENT).write("h2_1.extension", report.new_products, out)
    typer.echo(f"extension written to {FRAME_FILE}")


# ------------------------------------------------------------------------------------------- loading


def _datasets(root: Path) -> dict[bool, Any]:
    from monograph.ablation.data import build_cells_dataset

    return {identity: build_cells_dataset(root, identity) for identity in (True, False)}


def admin_dsn() -> str:
    from monograph.graph.cli import admin_dsn as h2_admin

    return h2_admin()


@h2_1_app.command("load")
def load(root: Root = Path()) -> None:
    """Build both identity levels from the caches and load every cell into its own database (both stores,
    compared in full after loading)."""
    from monograph.ablation.cells import CELLS
    from monograph.ablation.load import CellStore, load_cell

    datasets = _datasets(root)
    writer = wiring.results_writer(root, EXPERIMENT)
    for identity, ds in datasets.items():
        summary = ds.summary()
        typer.echo(f"identity {'on' if identity else 'off'}: {json.dumps(summary['instances'])}")
        writer.write("h2_1.dataset", sum(summary["instances"].values()), {"identity": identity, **summary})
    for cell in CELLS:
        done = load_cell(admin_dsn(), cell, CellStore.of(cell), datasets[cell.identity].instances, root, typer.echo)
        writer.write(
            "h2_1.load",
            sum(done.rows.values()),
            {"cell": cell.name, "rows": done.rows, "vertices": done.vertices, "edges": done.edges, "identical": True},
        )
        typer.echo(
            f"{cell.name}: {sum(done.rows.values())} rows, {sum(done.vertices.values())} vertices,"
            f" {sum(done.edges.values())} edges"
        )


def cell_service(root: Path, cell: Any, store: str) -> Any:
    """The shared tool service over one cell's store, connected with that cell's read-only role."""
    from monograph.ablation.load import CellStore, cell_projection, connect
    from monograph.tools.cypher_backend import CypherBackend
    from monograph.tools.service import ToolService
    from monograph.tools.sql_backend import SqlBackend

    s = CellStore.of(cell)
    projection = cell_projection(root, cell)
    if store == "sql":
        return ToolService(SqlBackend(connect(s.sql_dsn(admin_dsn())), projection), identity=cell.identity)
    return ToolService(CypherBackend(connect(s.graph_dsn(admin_dsn())), projection), identity=cell.identity)


# ----------------------------------------------------------------------------------------- questions

QUESTIONS_FILE = Path("fixtures/h2_1/questions.json")
QUESTION_SEED = 20261128


@h2_1_app.command("questions")
def questions(root: Root = Path()) -> None:
    """Generate the question set and its gold per identity level, checked through the tools in every cell
    (SQL store) and re-checked in every cell's Cypher store."""
    from dataclasses import asdict as _asdict

    from monograph.ablation.cells import CELLS
    from monograph.ablation.questions import generate, recheck
    from monograph.qa.index import Index

    datasets = _datasets(root)
    on, off = Index.build(datasets[True].instances), Index.build(datasets[False].instances)
    sql = {c.name: (c.identity, cell_service(root, c, "sql")) for c in CELLS}
    graph = {c.name: (c.identity, cell_service(root, c, "graph")) for c in CELLS}
    qs, report = generate(on, off, sql, QUESTION_SEED, typer.echo)
    bad = recheck(on, off, qs, graph, QUESTION_SEED)
    typer.echo(f"recheck on every cell's graph store: {len(bad)} disagreements")
    if bad:
        typer.echo(f"the stores disagree on {bad[:10]}", err=True)
        raise typer.Exit(1)
    counts: dict[str, dict[str, int]] = {}
    for q in qs:
        counts.setdefault(q["type"], {"dev": 0, "test": 0})[q["split"]] += 1
    out = {
        "seed": QUESTION_SEED,
        "dataset": {("identity_on" if k else "identity_off"): v.summary()["instances"] for k, v in datasets.items()},
        "counts": counts,
        "questions": qs,
    }
    path = root / QUESTIONS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    wiring.results_writer(root, EXPERIMENT).write("h2_1.questions", len(qs), {"counts": counts, **_asdict(report)})
    typer.echo(f"{len(qs)} questions written to {QUESTIONS_FILE}: {counts}")


# ---------------------------------------------------------------------------------------------- runs

RESPONSES = Path("data/h2_1/responses")


def load_questions(root: Path, split: str) -> list[dict[str, Any]]:
    from monograph.ablation import pins

    return [q for q in pins.questions(root)["questions"] if q["split"] == split]


def known_ids(cell: Any) -> set[str]:
    from monograph.ablation.load import CellStore, connect

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
    with connect(CellStore.of(cell).sql_dsn(admin_dsn())) as conn:
        return {str(r[0]) for t in tables for r in conn.execute(f"SELECT iri FROM h2.{t}").fetchall()}


def make_arm_factory(root: Path, cell: Any, arm: str) -> Any:
    from monograph.ablation import pins
    from monograph.ablation.load import CellStore, connect
    from monograph.agent.arms import free_arm, tools_arm
    from monograph.tools.freeform import CypherRunner, SqlRunner

    store = CellStore.of(cell)
    ddl, projection = pins.ddl(root, cell), pins.projection(root, cell)

    def make() -> Any:
        if arm == "S-free":
            live = free_arm(
                arm, SqlRunner(connect(store.sql_dsn(admin_dsn())), ddl), pins.schema_tool_description(root)
            )
        elif arm == "G-free":
            live = free_arm(
                arm, CypherRunner(connect(store.graph_dsn(admin_dsn())), projection), pins.schema_tool_description(root)
            )
        else:
            live = tools_arm(arm, cell_service(root, cell, "sql" if arm == "S-tools" else "graph"))
        return pins.pin_arm(live, root, cell)

    return make


def _materialized(q: dict[str, Any], cell: Any) -> dict[str, Any]:
    from monograph.ablation.questions import anchors_for, gold_for

    return {**q, "gold": gold_for(q, cell.identity), "anchors": anchors_for(q, cell.identity)}


def run_one(root: Path, model: str, cell_name: str, arm: str, split: str, limit: int | None, workers: int) -> None:
    import anthropic

    from monograph.ablation import pins
    from monograph.ablation.cells import CELL_BY_NAME
    from monograph.ablation.run import SETTINGS, Config, configs, crosswalk_classifier
    from monograph.agent.llm import AnthropicToolClient, CachedToolClient, ToolResponseCache
    from monograph.qa.run import Writer, run_split

    cell = CELL_BY_NAME.get(cell_name)
    if cell is None or split not in ("dev", "test"):
        raise typer.BadParameter("unknown cell or split")
    config = Config(model, cell, arm)
    if config not in configs():
        raise typer.BadParameter(f"{model} {cell.name} {arm} is not a pre-registered configuration")
    clean = wiring.require_clean_tree(root) if split == "test" else None
    if split == "test":
        drift = pins.frozen_drift(root)
        if drift:
            typer.echo(f"refusing the test run: PREREG frozen files changed or missing: {drift}", err=True)
            raise typer.Exit(1)
    qs = [_materialized(q, cell) for q in load_questions(root, split)]
    qs = qs[:limit] if limit else qs
    client = CachedToolClient(
        AnthropicToolClient(anthropic.Anthropic(max_retries=8)), ToolResponseCache(root / RESPONSES)
    )
    writer = wiring.results_writer(root, EXPERIMENT, clean_commit=clean)
    records = run_split(
        qs,
        arm,
        model,
        split,
        make_arm_factory(root, cell, arm),
        pins.system_prompt(root, arm),
        client,
        Writer(writer.write),
        known_ids(cell),
        workers,
        settings=SETTINGS,
        log=typer.echo,
        metric="h2_1",
        tag=config.tag,
        extra={"cell": cell.name, "identity": cell.identity, "descriptions": cell.descriptions},
        prompt_versions=pins.prompt_versions(root),
        classify=crosswalk_classifier(cell),
    )
    acc = sum(r["correct"] for r in records) / len(records) if records else 0.0
    cost = sum(float(r["cost_usd"]) for r in records if not r["from_cache"])
    typer.echo(
        f"{model} {cell.name} {arm} {split}: {len(records)} questions, accuracy {acc:.3f},"
        f" {client.network_calls} API calls, new spend USD {cost:.2f}"
    )


@h2_1_app.command("run")
def run(
    model: Annotated[str, typer.Option(help="claude-haiku-5-5 or claude-sonnet-5-5")],
    cell: Annotated[str, typer.Option(help="id-on_desc-on, id-on_desc-off, id-off_desc-on or id-off_desc-off")],
    arm: Annotated[str, typer.Option(help="S-free, S-tools, G-free or G-tools")],
    split: Annotated[str, typer.Option(help="dev or test")] = "dev",
    limit: Annotated[int | None, typer.Option(help="Only the first N questions (smoke runs).")] = None,
    workers: Annotated[int, typer.Option(help="Questions run concurrently.")] = 4,
    root: Root = Path(),
) -> None:
    """Run one pre-registered configuration on one split (responses cached; results appended to results/h2_1).
    A test run refuses to start from a dirty tree or if a frozen file changed."""
    run_one(root, model, cell, arm, split, limit, workers)


@h2_1_app.command("run-all")
def run_all(
    split: Annotated[str, typer.Option(help="dev or test")] = "dev",
    model: Annotated[str | None, typer.Option(help="Only this model.")] = None,
    workers: Annotated[int, typer.Option(help="Questions run concurrently.")] = 4,
    root: Root = Path(),
) -> None:
    """Run every pre-registered configuration (optionally of one model) on one split, one after another."""
    from monograph.ablation.run import configs

    for c in configs():
        if model is None or c.model == model:
            run_one(root, c.model, c.cell.name, c.arm, split, None, workers)
