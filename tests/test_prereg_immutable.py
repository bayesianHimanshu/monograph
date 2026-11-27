"""Pre-registrations are immutable once committed, and a model version bump never touches a frozen file."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from monograph.cli import app
from monograph.core.version import SCHEMA_VERSION
from monograph.validate.prereg import PreregError, prereg_violations, reference_violations

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


def test_appending_to_deviations_passes_and_rewriting_fails(repo: Path) -> None:
    log = repo / "docs" / "h9" / "DEVIATIONS.md"
    log.write_text("# H9 deviations\n\n## D1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "d1")
    log.write_text(log.read_text(encoding="utf-8") + "\n## D2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "d2")
    assert prereg_violations(repo) == []
    log.write_text("# H9 deviations\n\n## D2\n", encoding="utf-8")
    assert prereg_violations(repo) == [
        "docs/h9/DEVIATIONS.md: the working tree rewrote or removed committed entries (append-only)"
    ]
    _git(repo, "commit", "-qam", "rewrite")
    assert any("rewrote or removed earlier entries" in p for p in prereg_violations(repo))


def test_a_committed_pin_is_immutable(repo: Path) -> None:
    pin = repo / "experiments" / "h9" / "frozen" / "a.json"
    pin.parent.mkdir(parents=True)
    pin.write_text("{}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "pin")
    pin.write_text('{"x": 1}\n', encoding="utf-8")
    assert prereg_violations(repo) == [
        f"experiments/h9/frozen/a.json: the working tree differs from its first commit {_head(repo)}"
    ]


def _prereg(repo: Path, body: str) -> None:
    (repo / "docs" / "h9" / "PREREG.md").write_text(body, encoding="utf-8")


def test_a_prereg_must_reference_pins_not_live_paths(tmp_path: Path) -> None:
    (tmp_path / "docs" / "h9").mkdir(parents=True)
    pin = tmp_path / "experiments" / "h9" / "frozen" / "prompt.md"
    pin.parent.mkdir(parents=True)
    pin.write_text("prompt\n", encoding="utf-8")
    digest = hashlib.sha256(b"prompt\n").hexdigest()
    _prereg(tmp_path, f"See `docs/h9/PLAN.md`.\n\n| `experiments/h9/frozen/prompt.md` | `{digest}` |\n")
    assert reference_violations(tmp_path) == []
    _prereg(tmp_path, f"| `experiments/h9/frozen/prompt.md` | `{'0' * 64}` |\n")
    assert reference_violations(tmp_path) == [
        "docs/h9/PREREG.md: experiments/h9/frozen/prompt.md is missing or differs from its digest"
    ]
    _prereg(tmp_path, f"Prompt `src/monograph/prompt.md`.\n\n| `src/monograph/prompt.md` | `{digest}` |\n")
    [problem] = reference_violations(tmp_path)
    assert problem.startswith("docs/h9/PREREG.md references src/monograph/prompt.md, outside experiments/h9/frozen/")
    # A committed PREREG with a live path is accepted only through a DEVIATIONS pin with matching digests.
    log = tmp_path / "docs" / "h9" / "DEVIATIONS.md"
    log.write_text(f"| `src/monograph/prompt.md` | `experiments/h9/frozen/prompt.md` | `{digest}` |\n")
    assert reference_violations(tmp_path) == []
    pin.write_text("tampered\n", encoding="utf-8")
    assert reference_violations(tmp_path) == [
        "experiments/h9/frozen/prompt.md: sha256 differs from docs/h9/DEVIATIONS.md"
    ]


def test_a_pin_outside_its_experiment_is_refused(tmp_path: Path) -> None:
    (tmp_path / "docs" / "h9").mkdir(parents=True)
    _prereg(tmp_path, "| `fixtures/q.json` | `" + "1" * 64 + "` |\n")
    log = tmp_path / "docs" / "h9" / "DEVIATIONS.md"
    log.write_text("| `fixtures/q.json` | `experiments/h2/frozen/q.json` | `" + "1" * 64 + "` |\n")
    assert "docs/h9/DEVIATIONS.md: pin experiments/h2/frozen/q.json is outside experiments/h9/frozen/" in (
        reference_violations(tmp_path)
    )


def test_the_live_extraction_schema_follows_the_model_version() -> None:
    from monograph.gen.artifacts import EXTRACTION_SCHEMA_FILE

    live = json.loads((ROOT / "generated" / EXTRACTION_SCHEMA_FILE).read_text(encoding="utf-8"))
    assert live["x-schema-version"] == SCHEMA_VERSION


def _head(repo: Path) -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
    return out.stdout.strip()[:12]
