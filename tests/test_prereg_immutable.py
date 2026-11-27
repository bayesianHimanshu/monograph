"""Pre-registrations are immutable once committed, and a model version bump never touches a frozen file."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from monograph.cli import app
from monograph.core.models import MODELS, LabelExtraction
from monograph.core.version import EXTRACTION_TARGET_VERSION, SCHEMA_VERSION
from monograph.validate.prereg import PreregError, prereg_violations

from .conftest import ROOT


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "docs" / "h9").mkdir(parents=True)
    (tmp_path / "docs" / "h9" / "PREREG.md").write_text("# H9\n\nthreshold 0.05\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "prereg")
    return tmp_path


def test_unchanged_prereg_passes(repo: Path) -> None:
    (repo / "notes.md").write_text("other files may change\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "unrelated")
    assert prereg_violations(repo) == []


def test_working_tree_edit_is_caught(repo: Path) -> None:
    (repo / "docs" / "h9" / "PREREG.md").write_text("# H9\n\nthreshold 0.02\n", encoding="utf-8")
    expected = f"docs/h9/PREREG.md: the working tree differs from its first commit {_head(repo)}"
    assert prereg_violations(repo) == [expected]


def test_committed_edit_is_caught_even_when_reverted_on_disk_later(repo: Path) -> None:
    first = _head(repo)
    path = repo / "docs" / "h9" / "PREREG.md"
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "amended\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "amend")
    amend = _head(repo)
    problems = prereg_violations(repo)
    assert f"docs/h9/PREREG.md: changed by commit {amend} after its first commit {first}" in problems
    assert len(problems) == 2  # the commit and the working tree


def test_deletion_is_caught(repo: Path) -> None:
    _git(repo, "rm", "-q", "docs/h9/PREREG.md")
    _git(repo, "commit", "-qm", "drop")
    problems = prereg_violations(repo)
    assert any("deleted by commit" in p for p in problems)
    assert "docs/h9/PREREG.md: deleted from the working tree" in problems


def test_an_uncommitted_new_prereg_is_not_yet_frozen(repo: Path) -> None:
    (repo / "docs" / "h10").mkdir()
    (repo / "docs" / "h10" / "PREREG.md").write_text("draft\n", encoding="utf-8")
    assert prereg_violations(repo) == []


def test_shallow_history_refuses(repo: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    (repo / "notes.md").write_text("y\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "second")
    clone = tmp_path_factory.mktemp("shallow") / "c"
    subprocess.run(["git", "clone", "-q", "--depth", "1", repo.as_uri(), str(clone)], check=True, capture_output=True)
    with pytest.raises(PreregError, match="shallow"):
        prereg_violations(clone)


def test_cli_gate_fails_on_an_edit(repo: Path) -> None:
    (repo / "docs" / "h9" / "PREREG.md").write_text("changed\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["check", "prereg", "--root", str(repo)])
    assert result.exit_code == 1
    assert "immutable" in result.output


def test_h2_prereg_refuses_to_rewrite(tmp_path: Path) -> None:
    (tmp_path / "docs" / "h2").mkdir(parents=True)
    (tmp_path / "docs" / "h2" / "PREREG.md").write_text("frozen\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["h2", "prereg", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert (tmp_path / "docs" / "h2" / "PREREG.md").read_text(encoding="utf-8") == "frozen\n"


def test_generators_write_only_generated_files() -> None:
    from monograph import wiring
    from monograph.core.vocab import Vocabulary

    files = wiring.artifact_builder(wiring.build_spec(), Vocabulary()).build().files
    assert all("/" not in name and not name.endswith(".md") for name in files)


def test_the_h4_extraction_target_keeps_its_stamp_across_a_version_bump() -> None:
    """The frozen extraction schema carries the version H4 ran under, whatever SCHEMA_VERSION is, so a bump
    leaves it byte-identical. Code that needs the current version reads SCHEMA_VERSION."""
    from monograph import wiring
    from monograph.core.spec import analyze
    from monograph.core.vocab import Vocabulary
    from monograph.gen.artifacts import EXTRACTION_SCHEMA_FILE

    assert EXTRACTION_TARGET_VERSION == "2.1.1"
    committed = (ROOT / "generated" / EXTRACTION_SCHEMA_FILE).read_text(encoding="utf-8")
    assert json.loads(committed)["x-schema-version"] == EXTRACTION_TARGET_VERSION
    bumped = analyze(MODELS, "99.0.0")
    assert bumped.version != SCHEMA_VERSION
    files = wiring.artifact_builder(bumped, Vocabulary()).build().files
    assert files[EXTRACTION_SCHEMA_FILE] == committed
    assert LabelExtraction.__name__ in committed


def _head(repo: Path) -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
    return out.stdout.strip()[:12]
