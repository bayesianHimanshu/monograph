from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from monograph.mutate.mutations import BASELINE, MUTATIONS, Mutation, MutationError, replace_once
from monograph.mutate.runner import CommandResult, Gate, MutationRunner, temp_copy

from .conftest import ROOT


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda m: m.id)
def test_every_mutation_still_applies_to_the_source(mutation: Mutation) -> None:
    with temp_copy(ROOT) as root:
        before = (
            {p: p.read_bytes() for p in (root / "src").rglob("*.py")}
            | {p: p.read_bytes() for p in (root / "generated").glob("*.*")}
            | {p: p.read_bytes() for p in (root / "docs").rglob("PREREG.md")}
            | {p: p.read_bytes() for p in (root / "experiments").rglob("*") if p.is_file()}
        )
        listing = set(root.iterdir())
        mutation.apply(root, lambda *args: None)
        after = {p: p.read_bytes() for p in before}
        assert before != after or set(root.iterdir()) != listing, f"{mutation.id} changed nothing"


def test_replace_once_refuses_ambiguous_targets(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("a a")
    with pytest.raises(MutationError):
        replace_once(tmp_path, Path("f.py"), "a", "b")
    with pytest.raises(MutationError):
        replace_once(tmp_path, Path("f.py"), "zzz", "b")


@contextmanager
def _fake_workspace(source: Path) -> Iterator[Path]:
    yield source


def _runner(failing: set[str], tmp_path: Path) -> MutationRunner:
    def run(argv: list[str], cwd: Path, env: dict[str, str]) -> CommandResult:
        return CommandResult(1 if argv[0] in failing else 0, f"{argv[0]} output")

    gates = (Gate("drift", ("drift",)), Gate("lint", ("lint",)))
    ticks = iter(range(1000))
    return MutationRunner(tmp_path, gates, run, lambda: float(next(ticks)), "python", _fake_workspace)


def test_runner_records_which_gates_caught_a_mutation(tmp_path: Path) -> None:
    mutation = Mutation("MX", "x", "drift", lambda root, regen: None)
    result = _runner({"drift"}, tmp_path).run(mutation)
    assert result.failed_gates == ["drift"]
    assert result.as_expected
    assert result.evidence["drift"] == "drift output"
    wrong = _runner({"lint"}, tmp_path).run(mutation)
    assert wrong.caught
    assert not wrong.as_expected


def test_false_alarm_probe_must_pass_every_gate(tmp_path: Path) -> None:
    assert _runner(set(), tmp_path).run(BASELINE).as_expected
    assert not _runner({"lint"}, tmp_path).run(BASELINE).as_expected


def test_a_mutation_that_cannot_apply_is_not_counted_as_caught(tmp_path: Path) -> None:
    def broken(root: Path, regen: object) -> None:
        raise MutationError("target text gone")

    result = _runner({"drift"}, tmp_path).run(Mutation("MX", "x", "drift", broken))
    assert result.apply_error == "target text gone"
    assert not result.as_expected
