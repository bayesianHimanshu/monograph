"""The `monograph` command line."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from monograph import wiring
from monograph.core.spec import SchemaError
from monograph.core.vocab import Vocabulary
from monograph.gen.artifacts import write_artifacts
from monograph.gen.regex import RegexSubsetError
from monograph.validate.drift import DriftChecker

app = typer.Typer(no_args_is_help=True, add_completion=False, help="MONOGRAPH H6 tooling.")

Root = Annotated[Path, typer.Option("--root", help="Repository root.")]


def _fail(message: str) -> typer.Exit:
    typer.echo(f"FAIL: {message}", err=True)
    return typer.Exit(code=1)


@app.command()
def gen(
    root: Root = Path(),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Generate in memory only.")] = False,
) -> None:
    """Generate OWL, SHACL, JSON Schema and DDL from the models."""
    try:
        spec = wiring.build_spec()
        artifacts = wiring.artifact_builder(spec, Vocabulary()).build()
    except (SchemaError, RegexSubsetError) as exc:
        raise _fail(f"generation: {exc}") from exc
    if dry_run:
        typer.echo(f"generation ok: {len(artifacts.files)} artifacts")
        return
    written = write_artifacts(artifacts, wiring.generated_dir(root))
    typer.echo(f"generated {len(artifacts.files)} artifacts, {len(written)} changed: {', '.join(written) or 'none'}")


def _drift(root: Path) -> bool:
    spec = wiring.build_spec()
    artifacts = wiring.artifact_builder(spec, Vocabulary()).build()
    report = DriftChecker(wiring.generated_dir(root)).check(artifacts.files)
    for name in report.changed:
        typer.echo(f"drift: {name} differs from what the models generate", err=True)
    for name in report.missing:
        typer.echo(f"drift: {name} is missing", err=True)
    return report.ok


check_app = typer.Typer(no_args_is_help=True, help="Run one CI gate, or all of them.")
app.add_typer(check_app, name="check")


@check_app.command("drift")
def check_drift(root: Root = Path()) -> None:
    """Fail if committed generated/ files differ from a fresh generation."""
    try:
        ok = _drift(root)
    except (SchemaError, RegexSubsetError) as exc:
        raise _fail(f"generation: {exc}") from exc
    if not ok:
        raise _fail("drift check")
    typer.echo("drift check ok")


if __name__ == "__main__":
    app()
