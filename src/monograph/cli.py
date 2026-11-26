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
    report = wiring.conformance_run(spec, root).run(wiring.openfda_fixtures(root))
    agreed, total = report.agreement()
    typer.echo(f"agreement: {total} real openFDA candidates checked, {agreed} agree")
    for d in report.disagreements:
        failures += 1
        typer.echo(f"agreement: real {d['class']} {d['source']}: Pydantic and SHACL disagree", err=True)
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


ingest_app = typer.Typer(no_args_is_help=True, help="Fetch openFDA samples and measure conformance.")
app.add_typer(ingest_app, name="ingest")


@ingest_app.command("fetch")
def ingest_fetch(root: Root = Path()) -> None:
    """Fetch the openFDA samples into data/raw (cached; only missing pages hit the network)."""
    import os
    import time

    import httpx

    from monograph.ingest.openfda import SAMPLE_SIZES, OpenFdaClient, RawCache

    with httpx.Client(timeout=60.0) as http:
        client = OpenFdaClient(
            http, RawCache(wiring.raw_dir(root)), os.environ.get("OPENFDA_API_KEY"), 0.5, time.sleep, time.monotonic
        )
        for endpoint, size in SAMPLE_SIZES.items():
            records = client.sample(endpoint, size, 100)
            typer.echo(f"{endpoint}: {len(records)} records")
        typer.echo(f"network calls: {client.network_calls}")


@ingest_app.command("map")
def ingest_map(
    root: Root = Path(),
    source: Annotated[str, typer.Option(help="raw (data/raw) or fixtures (fixtures/openfda)")] = "raw",
    record: Annotated[bool, typer.Option(help="Append the results to results/h6.")] = True,
) -> None:
    """Map raw records to models and report conformance and agreement per class."""
    from monograph.harness.measure import record_conformance
    from monograph.ingest.openfda import ENDPOINTS, RawCache

    spec = wiring.build_spec()
    if source == "raw":
        cache = RawCache(wiring.raw_dir(root))
        raw = {endpoint: cache.all(endpoint) for endpoint in ENDPOINTS}
    else:
        raw = wiring.openfda_fixtures(root)
    report = wiring.conformance_run(spec, root).run(raw)
    for name, c in report.classes.items():
        typer.echo(f"{name}: {c.valid}/{c.candidates} conform ({c.rate:.0%})")
        for reason, count in c.reasons.most_common(3):
            typer.echo(f"    {count} x {reason}")
    agreed, total = report.agreement()
    typer.echo(f"agreement on real records: {agreed}/{total}")
    if record:
        record_conformance(wiring.results_writer(root), report, source)


@ingest_app.command("fixtures")
def ingest_fixtures(
    root: Root = Path(), force: Annotated[bool, typer.Option(help="Replace existing fixture files.")] = False
) -> None:
    """Copy a small representative subset of data/raw into fixtures/openfda for offline CI."""
    import json

    from monograph.core.instances import Instantiator
    from monograph.ingest.openfda import ENDPOINTS, OpenFdaMapper, RawCache, select_fixture_records
    from monograph.validate.pydantic_side import PydanticValidator

    spec = wiring.build_spec()
    mapper, instantiator, pydantic = OpenFdaMapper(), Instantiator(spec), PydanticValidator(wiring.today())

    def conforms(endpoint: str, raw: dict[str, object]) -> bool:
        return all(
            pydantic.check(instantiator.instantiate(spec.by_name(c.cls).cls, c.record)).valid
            for c in mapper.map(endpoint, raw)
        )

    cache = RawCache(wiring.raw_dir(root))
    chosen = select_fixture_records({e: cache.all(e) for e in ENDPOINTS}, conforms)
    directory = wiring.fixtures_dir(root) / "openfda"
    directory.mkdir(parents=True, exist_ok=True)
    for endpoint, records in chosen.items():
        path = directory / f"{endpoint}.json"
        if path.exists() and not force:
            raise _fail(f"{path} exists; pass --force to replace it")
        path.write_text(json.dumps(records, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        typer.echo(f"{endpoint}: {len(records)} records -> {path}")


if __name__ == "__main__":
    app()
