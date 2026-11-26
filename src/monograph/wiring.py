"""Composition root: the one place that builds concrete objects and passes them their dependencies.

Nothing here is cached at module level; every call builds fresh objects.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from rdflib import OWL, RDF, RDFS, XSD, Graph

from monograph.core.instances import Instantiator
from monograph.core.meta import DEFAULT_BASE
from monograph.core.models import MODELS
from monograph.core.regex import check_pattern
from monograph.core.spec import SchemaSpec, analyze
from monograph.core.version import SCHEMA_VERSION
from monograph.core.vocab import Vocabulary
from monograph.gen.artifacts import DDL_FILE, JSON_SCHEMA_FILE, OWL_FILE, SHACL_FILE, ArtifactBuilder
from monograph.gen.ddl import DdlGenerator
from monograph.gen.jsonschema import JsonSchemaGenerator
from monograph.gen.owl import OwlGenerator
from monograph.gen.shacl import SH, ShaclGenerator
from monograph.gen.turtle import TurtleWriter
from monograph.harness.results import ResultsWriter
from monograph.ingest.conformance import ConformanceRun
from monograph.ingest.openfda import ENDPOINTS, OpenFdaMapper
from monograph.rdf.mapping import RdfMapper
from monograph.validate.agreement import AgreementChecker
from monograph.validate.pydantic_side import PydanticValidator
from monograph.validate.shacl_side import ShaclValidator


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


def artifact_builder(spec: SchemaSpec, vocab: Vocabulary) -> ArtifactBuilder:
    writer = turtle_writer(vocab)
    renderers = {
        OWL_FILE: OwlGenerator(spec, vocab, writer).render,
        SHACL_FILE: ShaclGenerator(spec, vocab, writer, check_pattern).render,
        JSON_SCHEMA_FILE: JsonSchemaGenerator(spec, vocab, f"{DEFAULT_BASE}schema/{spec.version}").render,
        DDL_FILE: DdlGenerator(spec, check_pattern).render,
    }
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


def git_sha(root: Path) -> str:
    import subprocess

    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    # Results and the report are outputs, not inputs: appending to them does not make the source dirty.
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", ".", ":!results", ":!docs/h6/RESULTS.md"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return out.stdout.strip() + ("-dirty" if dirty.strip() else "")


def results_writer(root: Path) -> ResultsWriter:
    import uuid

    return ResultsWriter(
        results_dir(root), uuid.uuid4().hex[:12], git_sha(root), SCHEMA_VERSION, lambda: datetime.now(UTC)
    )


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
