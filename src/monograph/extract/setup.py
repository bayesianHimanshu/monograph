"""Composition root for H4: builds the reference graph, the context checker and the extractor."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rdflib import Graph

from monograph import wiring
from monograph.core.instances import Instantiator
from monograph.core.spec import SchemaSpec
from monograph.core.vocab import Vocabulary
from monograph.extract.arms import Extractor
from monograph.extract.config import MAX_REPAIR_ROUNDS, SETTINGS
from monograph.extract.llm import LlmClient
from monograph.extract.prompts import PromptSet, load_prompts
from monograph.extract.registry import load_reference, spl_documents
from monograph.extract.repair.shacl_feedback import CandidatePolicy, GraphContextChecker
from monograph.extract.resolve import NdcResolver, ReferenceGraph, SubstanceResolver
from monograph.extract.sources.dailymed import SplCache, SplDocument
from monograph.ingest.openfda import RawCache
from monograph.validate.prereg import pinned_path
from monograph.validate.pydantic_side import PydanticValidator

FRAME_FILE = Path("fixtures/h4/frame.json")
PROMPT_VERSION = "v3"

# H4 is frozen by docs/h4/PREREG.md, whose artifacts are pinned under experiments/h4/frozen/ (docs/h4/
# DEVIATIONS.md, D1). The data H4 loads comes from the pins, never from the live paths: the live extraction
# schema and context shapes follow the model, which is free to evolve.
SPLIT_REF = "fixtures/h4/split.json"
EXTRACTION_SCHEMA_REF = "generated/monograph.extraction.schema.json"
CONTEXT_SHAPES_REF = "generated/monograph.context.shapes.ttl"
LOADED_FROM_PIN = frozenset({SPLIT_REF, EXTRACTION_SCHEMA_REF, CONTEXT_SHAPES_REF})


def h4_split(root: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(pinned_path(root, "h4", SPLIT_REF).read_text(encoding="utf-8"))
    return data


def h4_extraction_schema(root: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(pinned_path(root, "h4", EXTRACTION_SCHEMA_REF).read_text(encoding="utf-8"))
    return data


def h4_context_shapes(root: Path) -> Path:
    return pinned_path(root, "h4", CONTEXT_SHAPES_REF)


@dataclass
class H4Context:
    spec: SchemaSpec
    split: dict[str, Any]
    docs: dict[str, SplDocument]
    reference: ReferenceGraph
    checker: GraphContextChecker
    checker_label: GraphContextChecker  # A3+: label-derived candidates
    substances: SubstanceResolver
    prompts: PromptSet

    def documents(self, split: str) -> list[SplDocument]:
        return [self.docs[r["set_id"]] for r in self.split[split]]


def build_context(root: Path) -> H4Context:
    spec = wiring.build_spec()
    vocab = Vocabulary()
    mapper = wiring.mapper(spec, vocab)
    split = h4_split(root)
    frame = json.loads((root / FRAME_FILE).read_text())
    set_ids = sorted(r["set_id"] for r in split["dev"] + split["test"])
    docs = {d.set_id: d for d in spl_documents(root / "data" / "raw" / "spl", set_ids)}
    reference = load_reference(
        spec,
        vocab,
        mapper,
        RawCache(wiring.raw_dir(root)),
        SplCache(root / "data" / "raw" / "spl"),
        set_ids,
        list(docs.values()),
        frame["h6_gap_lookup"],
    )
    substances = SubstanceResolver(reference, vocab)
    shapes = Graph().parse(h4_context_shapes(root), format="turtle")
    ndcs = NdcResolver(reference, vocab)
    checker = GraphContextChecker(reference, substances, ndcs, shapes, mapper, CandidatePolicy.INDEPENDENT)
    checker_label = GraphContextChecker(reference, substances, ndcs, shapes, mapper, CandidatePolicy.LABEL)
    prompts = load_prompts(PROMPT_VERSION, h4_extraction_schema(root))
    return H4Context(spec, split, docs, reference, checker, checker_label, substances, prompts)


def extractor(ctx: H4Context, client: LlmClient, model: str) -> Extractor:
    return Extractor(
        client,
        ctx.prompts,
        model,
        SETTINGS,
        PydanticValidator(wiring.today()),
        Instantiator(ctx.spec),
        ctx.checker,
        MAX_REPAIR_ROUNDS,
        ctx.checker_label,
    )
