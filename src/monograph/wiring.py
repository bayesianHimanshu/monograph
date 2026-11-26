"""Composition root: the one place that builds concrete objects and passes them their dependencies.

Nothing here is cached at module level; every call builds fresh objects.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

from rdflib import OWL, RDF, RDFS, XSD, Graph

from monograph.core.meta import DEFAULT_BASE
from monograph.core.models import MODELS
from monograph.core.spec import SchemaSpec, analyze
from monograph.core.version import SCHEMA_VERSION
from monograph.core.vocab import Vocabulary
from monograph.gen.artifacts import DDL_FILE, JSON_SCHEMA_FILE, OWL_FILE, SHACL_FILE, ArtifactBuilder
from monograph.gen.ddl import DdlGenerator
from monograph.gen.jsonschema import JsonSchemaGenerator
from monograph.gen.owl import OwlGenerator
from monograph.gen.regex import check_pattern
from monograph.gen.shacl import SH, ShaclGenerator
from monograph.gen.turtle import TurtleWriter
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
