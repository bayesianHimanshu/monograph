"""The `monograph` command line."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated

import typer

from monograph import wiring
from monograph.core.regex import RegexSubsetError
from monograph.core.spec import SchemaError
from monograph.core.vocab import Vocabulary
from monograph.gen.artifacts import write_artifacts
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


def _breaking(root: Path, as_version: str | None = None, record: bool = False) -> bool:
    from monograph.core.version import SCHEMA_VERSION
    from monograph.gen.artifacts import OWL_FILE, SHACL_FILE
    from monograph.validate.compat import CompatChecker

    version = as_version or SCHEMA_VERSION
    files = wiring.artifact_builder(wiring.build_spec(), Vocabulary()).build().files
    checker = CompatChecker(wiring.generated_dir(root) / "releases", OWL_FILE, SHACL_FILE)
    report = checker.check(version, files[OWL_FILE], files[SHACL_FILE])
    for title, changes in (("since previous release", report.changes), ("since own snapshot", report.snapshot_changes)):
        for change in changes:
            if change.breaking:
                typer.echo(f"breaking-change: {title}: BREAKING: {change.kind}: {change.subject}: {change.detail}")
    snapshot_breaking = sum(c.breaking for c in report.snapshot_changes)
    typer.echo(
        f"breaking-change: previous release {report.release}, current {report.current}:"
        f" {len(report.changes)} changes ({len(report.breaking)} breaking) since the previous release,"
        f" {len(report.snapshot_changes)} ({snapshot_breaking} breaking) since the {report.current} snapshot"
    )
    if report.problem:
        typer.echo(f"breaking-change: {report.problem}", err=True)
    if record:
        wiring.results_writer(root).write(
            "compat.check",
            report.ok,
            {
                "release": report.release,
                "checked_version": version,
                "schema_version_in_source": SCHEMA_VERSION,
                "changes": len(report.changes),
                "breaking": [f"{c.kind}: {c.subject}: {c.detail}" for c in report.breaking],
                "additive": len(report.changes) - len(report.breaking),
                "snapshot_changes": len(report.snapshot_changes),
                "snapshot_breaking": [
                    f"{c.kind}: {c.subject}: {c.detail}" for c in report.snapshot_changes if c.breaking
                ],
                "problem": report.problem,
            },
        )
    return report.ok


@check_app.command("breaking")
def check_breaking(
    root: Root = Path(),
    as_version: Annotated[
        str | None, typer.Option(help="Check as if SCHEMA_VERSION were this (to show what the gate demands).")
    ] = None,
    record: Annotated[bool, typer.Option(help="Append the verdict to results/h6.")] = False,
) -> None:
    """Fail if OWL/SHACL changed incompatibly since the last release without a major version bump."""
    try:
        ok = _breaking(root, as_version, record)
    except (SchemaError, RegexSubsetError) as exc:
        raise _fail(f"generation: {exc}") from exc
    if not ok:
        raise _fail("breaking-change check")
    typer.echo("breaking-change check ok")


def _prereg(root: Path) -> bool:
    from monograph.validate.prereg import PreregError, prereg_violations

    try:
        problems = prereg_violations(root)
    except PreregError as exc:
        typer.echo(f"prereg: {exc}", err=True)
        return False
    for problem in problems:
        typer.echo(f"prereg: {problem}", err=True)
    return not problems


@check_app.command("prereg")
def check_prereg(root: Root = Path()) -> None:
    """Fail if a committed PREREG.md or pinned experiments/hN/frozen/ file changed, a DEVIATIONS.md lost an
    entry, or a PREREG references a file outside experiments/hN/frozen/ and docs/hN/ without a logged pin."""
    if not _prereg(root):
        raise _fail("prereg check: pre-registrations are immutable once committed")
    typer.echo("prereg check ok")


@check_app.command("clean")
def check_clean(root: Root = Path()) -> None:
    """Fail if a test run would refuse to start: the working tree is dirty outside data/ and results/."""
    sha = wiring.require_clean_tree(root)
    typer.echo(f"clean tree at {sha}")


@check_app.command("all")
def check_all(root: Root = Path()) -> None:
    """Run drift, breaking-change, lint, agreement and prereg, and report every failure."""
    from monograph.core import models
    from monograph.validate.lint import ValidatorLinter

    failed = []
    try:
        if not _drift(root):
            failed.append("drift")
        if not _breaking(root):
            failed.append("breaking")
    except (SchemaError, RegexSubsetError) as exc:
        typer.echo(f"generation: {exc}", err=True)
        failed.append("generation")
    if not ValidatorLinter(models.MODELS, models).run().ok:
        failed.append("lint")
    if not _agreement(root):
        failed.append("agreement")
    if not _prereg(root):
        failed.append("prereg")
    if failed:
        raise _fail(f"gates failed: {', '.join(failed)}")
    typer.echo("all gates ok")


@app.command()
def mutate(
    root: Root = Path(),
    only: Annotated[list[str] | None, typer.Option("--only", help="Run only these mutation IDs.")] = None,
    record: Annotated[bool, typer.Option(help="Append the results to results/mutations.")] = True,
) -> None:
    """Run the mutation suite: copy the repo, break one thing, run every gate, record what caught it.
    Results go to results/mutations/ only, never to an experiment's results."""
    import sys
    import time

    from monograph.harness.measure import record_mutations
    from monograph.mutate.mutations import BASELINE, MUTATIONS
    from monograph.mutate.runner import MutationRunner, default_gates, subprocess_runner

    runner = MutationRunner(
        root.resolve(), default_gates(sys.executable), subprocess_runner, time.perf_counter, sys.executable
    )
    selected = [m for m in (BASELINE, *MUTATIONS) if not only or m.id in only]
    results = []
    for mutation in selected:
        result = runner.run(mutation)
        results.append(result)
        caught = ", ".join(result.failed_gates) or "nothing"
        verdict = "as expected" if result.as_expected else "UNEXPECTED"
        typer.echo(f"{result.id}: {result.title}: caught by {caught} ({verdict}, {result.seconds:.1f}s)")
        if result.apply_error:
            typer.echo(f"    could not apply: {result.apply_error}", err=True)
    if record:
        record_mutations(wiring.mutations_writer(root), results)
    if not all(r.as_expected for r in results):
        raise _fail("some mutations were not caught by their expected gate")


@app.command()
def measure(
    root: Root = Path(),
    examples: Annotated[int, typer.Option(help="Hypothesis examples per class for valid instances.")] = 50,
    per_site: Annotated[int, typer.Option(help="Hypothesis examples per mutation site.")] = 10,
) -> None:
    """Run every measurement and append the results to results/h6 (the report reads only those)."""
    import os

    from monograph.core import models
    from monograph.core.instances import Instantiator
    from monograph.core.meta import DEFAULT_BASE
    from monograph.core.regex import check_pattern
    from monograph.core.version import CHANGES, DEFERRED
    from monograph.gen.ddl import DdlGenerator, DdlProfile
    from monograph.harness.coverage import CoverageMatrix
    from monograph.harness.measure import Measurement, record_conformance, record_postgres
    from monograph.harness.property_run import PropertyAgreementRun
    from monograph.ingest.openfda import ENDPOINTS, RawCache
    from monograph.validate.fixtures import FixtureLoader
    from monograph.validate.lint import ValidatorLinter
    from monograph.validate.pydantic_side import PydanticValidator
    from monograph.validate.pyshacl_probes import all_probes
    from monograph.validate.strategies import Perturber, RecordStrategies

    spec = wiring.build_spec()
    vocab = Vocabulary()
    today = wiring.today()
    writer = wiring.results_writer(root)
    instantiator = Instantiator(spec)
    mapper = wiring.mapper(spec, vocab)
    checker = wiring.agreement_checker(spec, root, today)
    fixtures = FixtureLoader(spec, wiring.fixtures_dir(root))
    pydantic = PydanticValidator(today)
    m = Measurement(writer, spec, checker, instantiator, mapper, fixtures, pydantic.context)
    typer.echo(f"run {writer.run_id}: fixtures")
    m.fixtures_agreement()

    typer.echo("property-based agreement")
    strategies = RecordStrategies(
        spec, DEFAULT_BASE, today, lambda cls, record: pydantic.check(instantiator.instantiate(cls, record)).valid
    )
    run = PropertyAgreementRun(spec, checker, instantiator, strategies, Perturber(spec), examples, per_site)
    m.property_agreement(run, examples, per_site)

    cache = RawCache(wiring.raw_dir(root))
    raw = {endpoint: cache.all(endpoint) for endpoint in ENDPOINTS}
    source = "data/raw"
    if not any(raw.values()):
        raw, source = wiring.openfda_fixtures(root), "fixtures/openfda"
    typer.echo(f"real records from {source}")
    conformance = wiring.conformance_run(spec, root)
    conformance_report = conformance.run(raw)
    record_conformance(writer, conformance_report, source)
    wiring.quarantine_store(root).add(conformance_report.quarantine, writer.run_id)
    m.deferred(DEFERRED, conformance_report)
    real_valid = []
    for candidate in conformance.candidates(raw):
        instance = instantiator.instantiate(spec.by_name(candidate.cls).cls, candidate.record)
        if pydantic.check(instance).valid:
            real_valid.append(type(instance).model_validate(instance, context=pydantic.context))
    typer.echo("round trip, dataset integrity, leaks")
    m.round_trip(real_valid)
    m.dataset_integrity(wiring.shapes_graph(root))
    ddl = DdlGenerator(spec, check_pattern, DdlProfile.TEST).generate()
    m.leaks(ValidatorLinter(models.MODELS, models).run(), DdlGenerator(spec, check_pattern).generate())
    m.coverage(CoverageMatrix(spec, wiring.shapes_graph(root), DdlGenerator(spec, check_pattern).generate(), vocab))
    m.changelog(CHANGES)
    m.validator_findings(all_probes())

    dsn = os.environ.get("MONOGRAPH_PG_DSN", "postgresql://monograph:monograph@localhost:55432/monograph")
    try:
        from monograph.validate.fixtures import Fixture
        from monograph.validate.postgres_matrix import PostgresMatrixRun, scratch_schema

        nul = Fixture("nul-in-name", spec.by_name("Substance").cls, {"unii": "0000000000", "name": "A\x00B"}, "")
        with scratch_schema(dsn) as conn:
            matrix = PostgresMatrixRun(conn, spec, ddl, mapper, fixtures, instantiator).run([nul])
        record_postgres(writer, ddl, matrix, None)
        typer.echo("postgres matrix recorded")
    except Exception as exc:  # no database is a recorded, visible skip, not a silent one
        record_postgres(writer, ddl, None, f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}")
        typer.echo(f"postgres matrix skipped: {exc}", err=True)

    builder = wiring.artifact_builder(spec, vocab)
    m.sizes(root / "src" / "monograph" / "core" / "models.py", builder.build().files)
    m.generation_time(builder, wiring.build_spec, repeats=5)
    typer.echo(f"measurements appended to {wiring.results_dir(root)}")


report_app = typer.Typer(no_args_is_help=True, help="Regenerate experiment reports from results JSONL.")
app.add_typer(report_app, name="report")


@report_app.command("h6")
def report_h6(root: Root = Path()) -> None:
    """Regenerate docs/h6/RESULTS.md from results/h6/*.jsonl and the mutation suite's results/mutations/*.jsonl
    (the latest suite run, wherever it was recorded: runs before 2026-11-28 were recorded in results/h6/)."""
    from monograph.harness.report import ReportBuilder
    from monograph.harness.results import read_results

    records = [*read_results(wiring.results_dir(root)), *read_results(wiring.mutations_dir(root))]
    if not records:
        raise _fail("no results in results/h6; run `monograph measure` and `monograph mutate` first")
    text = ReportBuilder(records).render()
    path = root / "docs" / "h6" / "RESULTS.md"
    path.write_text(text, encoding="utf-8")
    typer.echo(f"wrote {path} from {len(records)} result lines")


@report_app.command("mutations")
def report_mutations(root: Root = Path()) -> None:
    """Regenerate docs/MUTATIONS.md from results/mutations/*.jsonl only: the latest run of each mutation, the
    gate that caught it, and the run's date and commit."""
    from monograph.harness.report_mutations import render
    from monograph.harness.results import read_results

    directory = wiring.mutations_dir(root)
    records = list(read_results(directory)) if directory.exists() else []
    path = root / "docs" / "MUTATIONS.md"
    path.write_text(render(records), encoding="utf-8")
    typer.echo(f"wrote {path} from {len(records)} result lines")


@report_app.command("h2")
def report_h2(root: Root = Path()) -> None:
    """Regenerate docs/h2/RESULTS.md from results/h2/*.jsonl alone."""
    from monograph.graph.cli import report_h2 as run

    run(root)


@report_app.command("h2_1")
def report_h2_1(root: Root = Path()) -> None:
    """docs/h2_1/RESULTS.md from results/h2_1/*.jsonl alone."""
    from monograph.ablation.cli import report_h2_1 as render_report

    render_report(root)


@report_app.command("h4")
def report_h4(root: Root = Path(), split: str = "test") -> None:
    """Regenerate docs/h4/RESULTS.md from results/h4/*.jsonl alone."""
    from monograph.harness.report_h4 import H4Report
    from monograph.harness.results import read_results

    records = list(read_results(root / "results" / "h4"))
    if not records:
        raise _fail("no results in results/h4; run `monograph h4 run` and `monograph h4 score` first")
    text = H4Report(records, split).render()
    path = root / "docs" / "h4" / "RESULTS.md"
    path.write_text(text, encoding="utf-8")
    typer.echo(f"wrote {path} from {len(records)} result lines")


@app.command()
def release(root: Root = Path()) -> None:
    """Snapshot generated/ into generated/releases/<SCHEMA_VERSION>/. Refuses to overwrite a release."""
    import shutil

    from monograph.core.version import SCHEMA_VERSION

    if not _drift(root):
        raise _fail("generated/ is out of date; run `monograph gen` first")
    target = wiring.generated_dir(root) / "releases" / SCHEMA_VERSION
    if target.exists():
        raise _fail(f"release {SCHEMA_VERSION} already exists; releases are immutable")
    target.mkdir(parents=True)
    for path in sorted(wiring.generated_dir(root).glob("monograph.*")):
        shutil.copy2(path, target / path.name)
    typer.echo(f"released {SCHEMA_VERSION} to {target}")


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
    from monograph.gen.artifacts import JSON_SCHEMA_FILE
    from monograph.validate.jsonschema_side import DECLARED_LEAKS, JsonSchemaValidator

    schema = JsonSchemaValidator(json.loads((wiring.generated_dir(root) / JSON_SCHEMA_FILE).read_text()))
    for f in loader.valid():
        if errors := schema.errors_for(f.cls.__name__, f.record):
            failures += 1
            typer.echo(f"agreement: JSON Schema rejects valid {f.name}: {errors[0]}", err=True)
    for f in loader.invalid():
        if not schema.errors_for(f.cls.__name__, f.record) and f.name not in DECLARED_LEAKS:
            failures += 1
            typer.echo(f"agreement: JSON Schema accepts invalid {f.name} ({f.violates})", err=True)
    typer.echo("agreement: fixtures checked against the generated JSON Schema")
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
    if report.quarantine:
        for q in report.quarantine:
            typer.echo(f"quarantined {q['source']}: {'; '.join(q['reasons'])}")
    if record:
        writer = wiring.results_writer(root)
        record_conformance(writer, report, source)
        wiring.quarantine_store(root).add(report.quarantine, writer.run_id)


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


from monograph.extract.cli import h4_app  # noqa: E402

app.add_typer(h4_app, name="h4")

if __name__ == "__main__":
    app()

from monograph.graph.cli import h2_app  # noqa: E402

app.add_typer(h2_app, name="h2")

from monograph.ablation.cli import h2_1_app  # noqa: E402

app.add_typer(h2_1_app, name="h2_1")
