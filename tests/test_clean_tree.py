"""`monograph hN run --split test` starts only from a clean tree, and records the clean commit on every line."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from monograph import wiring
from monograph.cli import app
from monograph.harness.results import ResultsWriter
from monograph.mutate.runner import commit_copy
from monograph.validate.clean import DirtyTree, dirty_paths, require_clean


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / ".gitignore").write_text("cache/\n")
    (tmp_path / "src.py").write_text("x = 1\n")
    for d in ("data", "results"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "kept.txt").write_text("tracked\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_a_clean_tree_gives_the_head_sha(repo: Path) -> None:
    assert require_clean(repo) == _git(repo, "rev-parse", "HEAD")


def test_data_results_and_ignored_files_do_not_count(repo: Path) -> None:
    (repo / "data" / "new.json").write_text("{}\n")
    (repo / "data" / "kept.txt").write_text("changed\n")
    (repo / "results" / "2026-11-27.jsonl").write_text("{}\n")
    (repo / "cache").mkdir()
    (repo / "cache" / "x").write_text("ignored\n")
    assert dirty_paths(repo) == []


@pytest.mark.parametrize("change", ["modified", "untracked", "nested-untracked", "staged", "deleted"])
def test_anything_else_is_dirty(repo: Path, change: str) -> None:
    if change == "modified":
        (repo / "src.py").write_text("x = 2\n")
    elif change == "untracked":
        (repo / "stray.txt").write_text("?\n")
    elif change == "nested-untracked":
        (repo / "docs" / "h9").mkdir(parents=True)
        (repo / "docs" / "h9" / "notes.md").write_text("?\n")
    elif change == "staged":
        (repo / "new.py").write_text("y = 1\n")
        _git(repo, "add", "new.py")
    else:
        (repo / "src.py").unlink()
    with pytest.raises(DirtyTree, match="dirty outside data/ and results/"):
        require_clean(repo)


def test_not_a_repository_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DirtyTree):
        require_clean(tmp_path)


@pytest.mark.parametrize(
    "argv",
    [
        ["h2", "run", "--arm", "S-free", "--model", "claude-haiku-5-5", "--split", "test"],
        ["h4", "run", "--arm", "A0", "--model", "claude-haiku-5-5", "--split", "test"],
    ],
)
def test_a_test_run_refuses_to_start_on_a_dirty_tree(repo: Path, argv: list[str]) -> None:
    (repo / "stray.txt").write_text("?\n")
    result = CliRunner().invoke(app, [*argv, "--root", str(repo)])
    assert result.exit_code == 1
    assert "refusing the test run" in result.output
    assert "stray.txt" in result.output


def test_check_clean_is_the_same_precondition(repo: Path) -> None:
    assert CliRunner().invoke(app, ["check", "clean", "--root", str(repo)]).exit_code == 0
    (repo / "stray.txt").write_text("?\n")
    assert CliRunner().invoke(app, ["check", "clean", "--root", str(repo)]).exit_code == 1


def test_every_test_result_line_records_the_clean_commit(repo: Path) -> None:
    sha = require_clean(repo)
    writer = wiring.results_writer(repo, "H9", clean_commit=sha)
    writer.write("h9.run", 1.0, {"q": 1})
    writer.write("h9.run", 0.0, {"q": 2})
    lines = [json.loads(x) for x in (repo / "results" / "h9").glob("*.jsonl").__next__().read_text().splitlines()]
    assert [(r["git_sha"], r["clean_commit"]) for r in lines] == [(sha, sha), (sha, sha)]


def test_lines_without_a_clean_commit_do_not_claim_one(tmp_path: Path) -> None:
    ResultsWriter(tmp_path, "r", "abc", "1.0.0", lambda: datetime(2026, 1, 1, tzinfo=UTC)).write("m", 1)
    assert "clean_commit" not in json.loads(next(tmp_path.glob("*.jsonl")).read_text())


def test_the_mutation_runner_commits_the_copy(repo: Path) -> None:
    (repo / "src.py").write_text("x = 3\n")
    (repo / "new.py").write_text("z = 1\n")
    (repo / "results" / "run.jsonl").write_text("{}\n")
    commit_copy(repo)
    assert dirty_paths(repo) == []
    assert "results/run.jsonl" not in _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()
