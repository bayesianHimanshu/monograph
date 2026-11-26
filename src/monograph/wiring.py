"""Composition root: the one place that builds concrete objects and passes them their dependencies.

Nothing here is cached at module level; every call builds fresh objects.
"""

from __future__ import annotations

import time
from pathlib import Path

from rdflib import OWL, RDF, RDFS, XSD

from monograph.core.meta import DEFAULT_BASE
from monograph.core.models import MODELS
from monograph.core.spec import SchemaSpec, analyze
from monograph.core.version import SCHEMA_VERSION
from monograph.core.vocab import Vocabulary
from monograph.gen.artifacts import JSON_SCHEMA_FILE, OWL_FILE, SHACL_FILE, ArtifactBuilder
from monograph.gen.jsonschema import JsonSchemaGenerator
from monograph.gen.owl import OwlGenerator
from monograph.gen.regex import check_pattern
from monograph.gen.shacl import SH, ShaclGenerator
from monograph.gen.turtle import TurtleWriter


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
    }
    return ArtifactBuilder(renderers, time.perf_counter)


def generated_dir(root: Path) -> Path:
    return root / "generated"
