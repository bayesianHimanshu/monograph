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
    report = build.build(admin_dsn(), dataset.instances, root, typer.echo)
    writer = wiring.results_writer(root, "H1")
    writer.write(
        "h1.build",
        sum(report.warehouse_rows.values()),
        {
            "instances": dataset.count(),
            "warehouse_rows": report.warehouse_rows,
            "round_trip_rows": report.round_trip,
        },
    )
    typer.echo(f"built {build.DATABASE}")
