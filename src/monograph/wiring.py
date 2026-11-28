"""Composition root: the one place that builds concrete objects and passes them their dependencies.

Nothing here is cached at module level; every call builds fresh objects.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from rdflib import OWL, RDF, RDFS, XSD, Graph

from monograph.core.instances import Instantiator
from monograph.core.meta import DEFAULT_BASE
from monograph.core.models import MODELS
from monograph.core.regex import check_pattern
from monograph.core.spec import SchemaSpec, analyze
from monograph.core.version import SCHEMA_VERSION
from monograph.core.vocab import Vocabulary
from monograph.gen.age import AgeProjectionGenerator
from monograph.gen.artifacts import (
    AGE_FILE,
    CONTEXT_SHACL_FILE,
    DDL_FILE,
    DDL_TEST_FILE,
    EXTRACTION_SCHEMA_FILE,
    JSON_SCHEMA_FILE,
    OWL_FILE,
    SHACL_FILE,
    ArtifactBuilder,
)
from monograph.gen.ddl import DdlGenerator, DdlProfile
from monograph.gen.jsonschema import JsonSchemaGenerator
from monograph.gen.owl import OwlGenerator
from monograph.gen.shacl import SH, ShaclGenerator
from monograph.gen.turtle import TurtleWriter
from monograph.harness.results import ResultsWriter
from monograph.ingest.conformance import ConformanceRun
from monograph.ingest.openfda import ENDPOINTS, OpenFdaMapper
from monograph.ingest.quarantine import QuarantineStore
from monograph.rdf.mapping import RdfMapper
from monograph.validate.agreement import AgreementChecker
from monograph.validate.pydantic_side import PydanticValidator
from monograph.validate.shacl_side import ShaclValidator


def h2_excluded() -> tuple[type[BaseModel], ...]:
    """Classes outside the H2 data: the H4 extraction views of the model (and their embedded values)."""
    from monograph.core.models import LabelExtraction

    return (LabelExtraction,)


def build_spec() -> SchemaSpec:
    return analyze(MODELS, SCHEMA_VERSION)


def turtle_writer(vocab: Vocabulary) -> TurtleWriter:
    return TurtleWriter(
        [
            ("mono", vocab.ns),
            ("shape", vocab.shapes_ns),
            ("owl", str(OWL)),
            ("rdf", str(RDF)),
            ("rdfs", str(RDFS)),
            ("sh", str(SH)),
            ("xsd", str(XSD)),
        ]
    )


def context_vocab() -> Vocabulary:
    """Context shapes live in their own IRI space, next to the record shapes."""
    from monograph.core.meta import SHAPES_NS, VOCAB_NS

    return Vocabulary(VOCAB_NS, SHAPES_NS + "context-")


def artifact_builder(spec: SchemaSpec, vocab: Vocabulary) -> ArtifactBuilder:
    from monograph.core.models import LabelExtraction

    writer = turtle_writer(vocab)
    json_schema = JsonSchemaGenerator(spec, vocab, f"{DEFAULT_BASE}schema/{spec.version}")
    renderers: dict[str, Callable[[], str]] = {
        OWL_FILE: OwlGenerator(spec, vocab, writer).render,
        SHACL_FILE: ShaclGenerator(spec, vocab, writer, check_pattern).render,
        JSON_SCHEMA_FILE: json_schema.render,
        DDL_FILE: DdlGenerator(spec, check_pattern, DdlProfile.DEFAULT).render,
        DDL_TEST_FILE: DdlGenerator(spec, check_pattern, DdlProfile.TEST).render,
    }
    if any(c.cls is LabelExtraction for c in spec.classes):  # the H4 extraction views of the model
        context = ShaclGenerator(
            spec, context_vocab(), turtle_writer(context_vocab()), check_pattern, (LabelExtraction,)
        )
        renderers[EXTRACTION_SCHEMA_FILE] = lambda: json_schema.render_extraction(LabelExtraction)
        renderers[CONTEXT_SHACL_FILE] = context.render
        renderers[AGE_FILE] = AgeProjectionGenerator(spec, h2_excluded()).render
        from monograph.ablation.cells import cell_renderers  # the H2.1 cells, from the same spec

        renderers.update(cell_renderers(spec, check_pattern, h2_excluded()))
        from monograph.gen.warehouse import warehouse_renderers  # the H1 warehouse, from the same spec

        renderers.update(warehouse_renderers(spec, h2_excluded()))
    return ArtifactBuilder(renderers, time.perf_counter)


def generated_dir(root: Path) -> Path:
    return root / "generated"


def today() -> date:
    """The clock for date-relative validators. Read once here and passed down explicitly."""
    return date.today()


def fixtures_dir(root: Path) -> Path:
    return root / "fixtures"


def shapes_graph(root: Path) -> Graph:
    """The committed shapes, which is what consumers of the model would use."""
    return Graph().parse(generated_dir(root) / SHACL_FILE, format="turtle")


def mapper(spec: SchemaSpec, vocab: Vocabulary, base: str = DEFAULT_BASE) -> RdfMapper:
    return RdfMapper(spec, vocab, base)


def agreement_checker(spec: SchemaSpec, root: Path, today: date) -> AgreementChecker:
    vocab = Vocabulary()
    return AgreementChecker(PydanticValidator(today), ShaclValidator(shapes_graph(root), mapper(spec, vocab)))


def raw_dir(root: Path) -> Path:
    return root / "data" / "raw"


def results_dir(root: Path) -> Path:
    return root / "results" / "h6"


MUTATIONS = "MUTATIONS"  # results/mutations/: the mutation suite's own results, never an experiment's


def mutations_dir(root: Path) -> Path:
    return root / "results" / MUTATIONS.lower()


def mutations_writer(root: Path) -> ResultsWriter:
    """Where `monograph mutate` records its results. The suite belongs to no experiment, so it never writes to
    results/h6/ or any other experiment's results."""
    return results_writer(root, MUTATIONS)


def git_sha(root: Path) -> str:
    import subprocess

    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    # Results and the report are outputs, not inputs: appending to them does not make the source dirty.
    dirty = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "--",
            ".",
            ":!results",
            ":!docs/h6/RESULTS.md",
            ":!docs/MUTATIONS.md",
            ":!docs/h4/RESULTS.md",
            ":!docs/h2/RESULTS.md",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return out.stdout.strip() + ("-dirty" if dirty.strip() else "")


def results_writer(root: Path, experiment: str = "H6", clean_commit: str | None = None) -> ResultsWriter:
    """`clean_commit`: the SHA a test run verified clean (monograph.validate.clean); it is then recorded as
    both `git_sha` and `clean_commit` on every line."""
    import uuid

    directory = root / "results" / experiment.lower()
    sha = clean_commit if clean_commit is not None else git_sha(root)
    return ResultsWriter(
        directory, uuid.uuid4().hex[:12], sha, SCHEMA_VERSION, lambda: datetime.now(UTC), experiment, clean_commit
    )


def require_clean_tree(root: Path) -> str:
    """The clean-tree precondition of every `monograph hN run --split test`: the HEAD SHA, or typer.Exit(1)."""
    import typer

    from monograph.validate.clean import DirtyTree, require_clean

    try:
        return require_clean(root)
    except DirtyTree as exc:
        typer.echo(f"refusing the test run: {exc}; commit or remove it first", err=True)
        raise typer.Exit(code=1) from exc


def conformance_run(spec: SchemaSpec, root: Path) -> ConformanceRun:
    vocab = Vocabulary()
    return ConformanceRun(
        spec, OpenFdaMapper(), Instantiator(spec), mapper(spec, vocab), agreement_checker(spec, root, today())
    )


def openfda_fixtures(root: Path) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for endpoint in ENDPOINTS:
        path = fixtures_dir(root) / "openfda" / f"{endpoint}.json"
        out[endpoint] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    return out


def quarantine_store(root: Path) -> QuarantineStore:
    return QuarantineStore(root / "data" / "quarantine", lambda: datetime.now(UTC))
