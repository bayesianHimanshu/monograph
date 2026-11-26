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
from monograph.gen.artifacts import CONTEXT_SHACL_FILE, EXTRACTION_SCHEMA_FILE
from monograph.ingest.openfda import RawCache
from monograph.validate.pydantic_side import PydanticValidator

SPLIT_FILE = Path("fixtures/h4/split.json")
FRAME_FILE = Path("fixtures/h4/frame.json")
PROMPT_VERSION = "v1"


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
    split = json.loads((root / SPLIT_FILE).read_text())
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
    shapes = Graph().parse(wiring.generated_dir(root) / CONTEXT_SHACL_FILE, format="turtle")
    ndcs = NdcResolver(reference, vocab)
    checker = GraphContextChecker(reference, substances, ndcs, shapes, mapper, CandidatePolicy.INDEPENDENT)
    checker_label = GraphContextChecker(reference, substances, ndcs, shapes, mapper, CandidatePolicy.LABEL)
    schema = json.loads((wiring.generated_dir(root) / EXTRACTION_SCHEMA_FILE).read_text())
    prompts = load_prompts(PROMPT_VERSION, schema)
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
    )
