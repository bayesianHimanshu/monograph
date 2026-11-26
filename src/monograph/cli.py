"""The `monograph` command line."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer

from monograph import wiring
from monograph.core.spec import SchemaError
from monograph.core.vocab import Vocabulary
from monograph.gen.artifacts import write_artifacts
from monograph.gen.regex import RegexSubsetError
from monograph.validate.drift import DriftChecker

logging.getLogger("rdflib").setLevel(logging.ERROR)  # invalid IRIs in negative tests are expected

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


@check_app.command("lint")
def check_lint() -> None:
    """Fail if a validator is neither generated from a rule nor declared pydantic_only."""
    from monograph.core import models
    from monograph.validate.lint import ValidatorLinter

    report = ValidatorLinter(models.MODELS, models).run()
    for problem in report.problems:
        typer.echo(f"lint: {problem.cls}.{problem.validator}: {problem.problem}", err=True)
    if not report.ok:
        raise _fail("validator lint")
    typer.echo(f"validator lint ok: {len(report.rule_engines)} rule engine, {len(report.declared)} pydantic_only")


def _agreement(root: Path) -> bool:
    from monograph.core.instances import Instantiator
    from monograph.validate.fixtures import FixtureLoader

    spec = wiring.build_spec()
    checker = wiring.agreement_checker(spec, root, wiring.today())
    instantiator = Instantiator(spec)
    loader = FixtureLoader(spec, wiring.fixtures_dir(root))
    failures = 0
    groups = (("valid", loader.valid()), ("invalid", loader.invalid()), ("probe", loader.probes()))
    for group, fixtures in groups:
        for f in fixtures:
            outcome = checker.check(instantiator.instantiate(f.cls, f.record)).outcome
            if outcome.value != f.expect:
                failures += 1
                typer.echo(f"agreement: {group} {f.name}: expected {f.expect}, got {outcome.value}", err=True)
        typer.echo(f"agreement: {len(fixtures)} {group} fixtures checked")
    return failures == 0


@check_app.command("agreement")
def check_agreement(root: Root = Path()) -> None:
    """Fail if Pydantic and SHACL verdicts differ from what each fixture expects."""
    if not _agreement(root):
        raise _fail("agreement check")
    typer.echo("agreement check ok")


if __name__ == "__main__":
    app()
