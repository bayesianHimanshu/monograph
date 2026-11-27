"""The feedback audit flags a value that only the label's own sources contain."""

from __future__ import annotations

from monograph import wiring
from monograph.core.spec import SchemaSpec
from monograph.core.vocab import Vocabulary
from monograph.extract.audit import audit_feedback, candidates_in
from monograph.extract.resolve import ReferenceGraph, SubstanceSource


def _reference(spec: SchemaSpec) -> ReferenceGraph:
    v = Vocabulary()
    ref = ReferenceGraph(spec, v, wiring.mapper(spec, v))
    ref.add_substances(
        [
            SubstanceSource("R16CO5Y76E", "ASPIRIN", "dailymed-uniis"),
            SubstanceSource("SENT1NEL01", "SODIUM PHOSPHATE, DIBASIC", "spl", "label-a"),
        ]
    )
    ref.link_ndc("0280-3000-10", "other-label")
    ref.link_ndc("0280-2000-10", "label-a")
    return ref


def test_names_with_commas_are_matched_whole(spec: SchemaSpec) -> None:
    values, unmatched = candidates_in("- x; allowed: ASPIRIN, SODIUM PHOSPHATE, DIBASIC", _reference(spec))
    assert (values, unmatched) == (["ASPIRIN", "SODIUM PHOSPHATE, DIBASIC"], 0)


def test_label_only_values_are_counted(spec: SchemaSpec) -> None:
    ref = _reference(spec)
    sent = {"details": {"feedback": "- a; allowed: ASPIRIN, 0280-3000-10\n- b; allowed: 0280-2000-10"}}
    result = audit_feedback({("m", "A3", "label-a"): [sent]}, ref)["m A3"]
    assert (result["offered"], result["label_only"]) == (3, 1)
    assert result["examples"] == [["label-a", "0280-2000-10"]]
