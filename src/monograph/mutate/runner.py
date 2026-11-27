"""Copy the repository, apply one mutation, run every gate, and record which gates caught it."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from monograph.mutate.mutations import Mutation, MutationError

# .git is copied: the prereg gate compares each PREREG.md with the commit that added it.
_IGNORE = shutil.ignore_patterns(
    ".venv", "data", "results", "__pycache__", ".mypy_cache", ".pytest_cache", ".hypothesis", ".ruff_cache"
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    output: str


RunCommand = Callable[[list[str], Path, dict[str, str]], CommandResult]


@dataclass(frozen=True, slots=True)
class Gate:
    name: str
    argv: tuple[str, ...]


def default_gates(python: str) -> tuple[Gate, ...]:
    cli = (python, "-m", "monograph.cli")
    return (
        Gate("generation", (*cli, "gen", "--dry-run")),
        Gate("drift", (*cli, "check", "drift")),
        Gate("breaking", (*cli, "check", "breaking")),
        Gate("lint", (*cli, "check", "lint")),
        Gate("agreement", (*cli, "check", "agreement")),
        Gate("prereg", (*cli, "check", "prereg")),
        # tests/test_mutations.py checks that the mutations apply to the pristine source, which a mutant
        # copy no longer is, so it is the one test file the mutant pipeline leaves out.
        Gate(
            "tests",
            (
                python,
                "-m",
                "pytest",
                "-q",
                "-x",
                "-m",
                "not integration",
                "-p",
                "no:cacheprovider",
                "--ignore=tests/test_mutations.py",
            ),
        ),
    )


@dataclass
class MutationResult:
    id: str
    title: str
    expected_gate: str | None
    failed_gates: list[str] = field(default_factory=list)
    gate_seconds: dict[str, float] = field(default_factory=dict)
    seconds: float = 0.0
    apply_error: str | None = None
    evidence: dict[str, str] = field(default_factory=dict)

    @property
    def caught(self) -> bool:
        return bool(self.failed_gates)

    @property
    def as_expected(self) -> bool:
        if self.apply_error is not None:
            return False
        if self.expected_gate is None:
            return not self.failed_gates
        return any(g in self.failed_gates for g in self.expected_gate.split("|"))


@contextmanager
def temp_copy(source: Path) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="monograph-mutant-") as tmp:
        target = Path(tmp) / "repo"
        shutil.copytree(source, target, ignore=_IGNORE)
        yield target


class MutationRunner:
    def __init__(
        self,
        source: Path,
        gates: tuple[Gate, ...],
        run_command: RunCommand,
        clock: Callable[[], float],
        python: str,
        workspace: Callable[[Path], AbstractContextManager[Path]] = temp_copy,
    ) -> None:
        self._source = source
        self._gates = gates
        self._run = run_command
        self._clock = clock
        self._python = python
        self._workspace = workspace

    def _env(self, root: Path) -> dict[str, str]:
        return {"PYTHONPATH": str(root / "src"), "HYPOTHESIS_PROFILE": "mutation"}

    def run(self, mutation: Mutation) -> MutationResult:
        result = MutationResult(mutation.id, mutation.title, mutation.expected_gate)
        start = self._clock()
        with self._workspace(self._source) as root:
            env = self._env(root)

            def cli(*args: str) -> None:
                out = self._run([self._python, "-m", "monograph.cli", *args], root, env)
                if out.returncode != 0:
                    raise MutationError(f"`monograph {' '.join(args)}` failed: {out.output[-300:]}")

            try:
                mutation.apply(root, cli)
            except MutationError as exc:
                result.apply_error = str(exc)
                result.seconds = self._clock() - start
                return result
            for gate in self._gates:
                gate_start = self._clock()
                out = self._run(list(gate.argv), root, env)
                result.gate_seconds[gate.name] = round(self._clock() - gate_start, 3)
                if out.returncode != 0:
                    result.failed_gates.append(gate.name)
                    lines = [line for line in out.output.splitlines() if line.strip()]
                    result.evidence[gate.name] = " | ".join(lines[-3:])[-400:]
        result.seconds = round(self._clock() - start, 3)
        return result


def subprocess_runner(argv: list[str], cwd: Path, env: dict[str, str]) -> CommandResult:
    import os
    import subprocess

    full_env = {**os.environ, **env}
    done = subprocess.run(argv, cwd=cwd, env=full_env, capture_output=True, text=True, check=False, timeout=900)
    return CommandResult(done.returncode, done.stdout + done.stderr)
