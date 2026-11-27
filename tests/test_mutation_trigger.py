"""The CI mutation job runs on pull requests that touch what the suite guards, and only those."""

from __future__ import annotations

import pytest

from monograph.mutate.trigger import touches_gates


@pytest.mark.parametrize(
    "path",
    [
        "src/monograph/core/models.py",
        "src/monograph/gen/ddl.py",
        "src/monograph/validate/prereg.py",
        "src/monograph/mutate/mutations.py",
        "src/monograph/cli.py",
        "tools/pre-commit",
        ".github/workflows/ci.yml",
        "experiments/h2/frozen/questions.json",
        "experiments/h2_1/frozen/code/src/monograph/tools/service.py",
        "docs/h4/PREREG.md",
        "docs/h2_1/DEVIATIONS.md",
    ],
)
def test_guarded_paths_trigger(path: str) -> None:
    assert touches_gates([path])


@pytest.mark.parametrize(
    "path",
    [
        "src/monograph/tools/service.py",
        "src/monograph/graph/cli.py",
        "docs/h2_1/PLAN.md",
        "docs/h2/RESULTS.md",
        "results/h2/2026-11-27.jsonl",
        "tests/test_mutations.py",
        "experiments/h2/notes.md",
        "README.md",
    ],
)
def test_other_paths_do_not(path: str) -> None:
    assert not touches_gates([path])


def test_any_match_in_a_list_triggers() -> None:
    assert touches_gates(["README.md", "", "src/monograph/gen/age.py"])
    assert not touches_gates([])
