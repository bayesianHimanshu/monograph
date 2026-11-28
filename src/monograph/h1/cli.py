"""`monograph h1 ...`: build, questions, run, score (docs/h1/PLAN.md)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from monograph import wiring

h1_app = typer.Typer(no_args_is_help=True, help="H1: ontology-checked text-to-SQL against semantic views alone.")
Root = Annotated[Path, typer.Option("--root", help="Repository root.")]


def admin_dsn() -> str:
    dsn = os.environ.get("MONOGRAPH_AGE_DSN") or os.environ.get("MONOGRAPH_PG_DSN")
    if not dsn:
        raise typer.BadParameter("set MONOGRAPH_AGE_DSN or MONOGRAPH_PG_DSN (make up)")
    return dsn


@h1_app.command("build")
def build_cmd(root: Root = Path()) -> None:
    """Build the H1 database: the canonical tables (the H2.1 dataset, identity on), the warehouse loaded
    from them with the round trip proven, the semantic layer and the agents' roles."""
    from monograph.ablation.data import build_cells_dataset
    from monograph.h1 import build

    dataset = build_cells_dataset(root, identity=True)
    reports = build.build(admin_dsn(), dataset.instances, root, typer.echo)
    writer = wiring.results_writer(root, "H1")
    for context, report in reports.items():
        writer.write(
            "h1.build",
            sum(report.warehouse_rows.values()),
            {
                "context": context,
                "database": build.database(context),
                "instances": dataset.count(),
                "warehouse_rows": report.warehouse_rows,
                "round_trip_rows": report.round_trip,
                "semantic_view_rows": report.semantic_checks,
            },
        )
    typer.echo("built " + ", ".join(build.database(c) for c in reports))


@h1_app.command("questions")
def questions_cmd(root: Root = Path()) -> None:
    """Generate the question set (fixtures/h1/questions.json): gold by code over the canonical instances,
    every question's reference SQL checked on the warehouse."""
    import psycopg

    from monograph.ablation.data import build_cells_dataset
    from monograph.h1 import build, questions
    from monograph.qa.h1_templates import H1Data

    data = H1Data.build(build_cells_dataset(root, identity=True).instances)
    with psycopg.connect(build.agent_dsn(admin_dsn(), "wh")) as conn:
        qs = questions.generate(data, conn, questions.coverage(root), typer.echo)
    path = questions.write(root, qs)
    inside = sum(q["coverage"] == "inside" for q in qs)
    typer.echo(f"wrote {len(qs)} questions to {path} ({inside} inside semantic coverage)")


RESPONSES = Path("data/h1/responses")
BUDGET_FILE = Path("experiments/h1/budget.json")
EXPERIMENT = "H1"


@h1_app.command("run")
def run_cmd(
    arm: Annotated[str, typer.Option(help="W-raw, SV, SV+OC, ONT or ONT+OC")],
    model: Annotated[str, typer.Option(help="claude-haiku-5-5 or claude-sonnet-5-5")],
    split: Annotated[str, typer.Option(help="dev or test")] = "dev",
    limit: Annotated[int | None, typer.Option(help="Only the first N questions (smoke runs).")] = None,
    workers: Annotated[int, typer.Option(help="Questions run concurrently.")] = 4,
    root: Root = Path(),
) -> None:
    """Run one pre-registered configuration on one split (responses cached; results appended to results/h1).
    A test run needs a clean tree, the pins of docs/h1/PREREG.md unchanged, a database built from them, and
    the approved spend cap in experiments/h1/budget.json; it stops at the cap."""
    from typing import Any

    import anthropic

    from monograph.agent.llm import AnthropicToolClient, CachedToolClient, ToolResponseCache
    from monograph.h1 import arms, build, pins
    from monograph.h1.run import Config, configs, connection_executor, run_config, types_for
    from monograph.qa.run import Writer

    config = Config(model, arm)
    if split not in ("dev", "test") or config not in configs():
        raise typer.BadParameter(f"{model} {arm} {split} is not a pre-registered configuration")
    clean = wiring.require_clean_tree(root) if split == "test" else None
    if split == "test":
        drift = pins.frozen_drift(root)
        if drift:
            typer.echo(f"refusing the test run: PREREG frozen files changed or missing: {drift}", err=True)
            raise typer.Exit(1)
    db = arms.DATABASE[arms.CONTEXT[arm]]
    if build.database_stamp(admin_dsn(), db) != pins.pinned_stamp(root):
        typer.echo(
            f"refusing the run: {build.database(db)} was not built from these artifacts (monograph h1 build)", err=True
        )
        raise typer.Exit(1)
    types = types_for(model)
    qs = [q for q in pins.questions(root) if q["split"] == split and (types is None or q["type"] in types)]
    qs = qs[:limit] if limit else qs
    writer = wiring.results_writer(root, EXPERIMENT, clean_commit=clean)
    write = Writer(writer.write)
    network: Any = AnthropicToolClient(anthropic.Anthropic(max_retries=8))
    extra: dict[str, Any] = {}
    guard = None
    if split == "test":  # the approved cap on cumulative test spend, reruns included
        from monograph.agent.budget import BudgetedClient, BudgetExceeded, open_guard

        try:
            guard = open_guard(root / BUDGET_FILE, root / "results" / "h1", "h1", "test", write)
        except BudgetExceeded as exc:
            typer.echo(f"refusing the test run: {exc}", err=True)
            raise typer.Exit(1) from exc
        network = BudgetedClient(network, guard)
        extra["ledgered"] = True
    client = CachedToolClient(network, ToolResponseCache(root / RESPONSES))
    executor = connection_executor(build.agent_dsn(admin_dsn(), db))
    records = run_config(root, qs, config, split, client, write, executor, workers, typer.echo, extra)
    acc = sum(r["correct"] for r in records) / len(records) if records else 0.0
    cost = sum(float(r["cost_usd"]) for r in records if not r["from_cache"])
    typer.echo(
        f"{model} {arm} {split}: {len(records)} questions, accuracy {acc:.3f}, {client.network_calls} API calls,"
        f" new spend USD {cost:.2f}"
    )
    if guard is not None and guard.tripped:
        typer.echo(
            f"stopped at the approved cap: test spend USD {guard.spent:.2f} of USD {guard.cap.usd:.2f}", err=True
        )
        raise typer.Exit(1)


@h1_app.command("score")
def score_cmd(split: Annotated[str, typer.Option(help="dev or test")] = "dev", root: Root = Path()) -> None:
    """Accuracy per model, arm and type from results/h1 (the latest record per question), with the cost."""
    from monograph.h1 import analysis
    from monograph.harness.results import read_results

    runs = analysis.latest_runs(read_results(root / "results" / "h1"), split)
    for line in analysis.summary_lines(runs):
        typer.echo(line)
