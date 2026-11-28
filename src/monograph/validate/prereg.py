"""Pre-registrations and the artifacts they freeze.

Three rules, all checked by `monograph check prereg`:

1. Immutable: every docs/hN/PREREG.md and every file under experiments/hN/frozen/, once committed, stays
   byte-identical to the version in the commit that added it (no later commit may change it, and the working
   tree must match).
2. Append-only: docs/hN/DEVIATIONS.md only grows; each committed version starts with the previous one.
3. Pinned references: every file a PREREG references (its frozen table and any path in backticks) lies under
   experiments/hN/frozen/ or docs/hN/. A PREREG committed before this rule may reference a live path only if
   docs/hN/DEVIATIONS.md maps it to a pin under experiments/hN/frozen/ whose sha256 matches, and a pin of a
   frozen-table entry must hash to the PREREG's digest.

The history checks refuse a shallow clone instead of comparing against a truncated history.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

FROZEN_ROW = re.compile(r"^\| `([^`]+)` \| `([0-9a-f]{64})` \|$", re.MULTILINE)
PIN_ROW = re.compile(r"^\| (.+?) \| `(experiments/[^`]+)` \| `([0-9a-f]{64})` \|$", re.MULTILINE)
PATH_REF = re.compile(r"`([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.[A-Za-z0-9]+)`")


class PreregError(RuntimeError):
    """The history needed for the check is not available, or a reference has no pin."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> bytes:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
    if done.returncode != 0:
        raise PreregError(f"git {' '.join(args)}: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def _blob(root: Path, commit: str, path: str) -> bytes | None:
    argv = ["git", "rev-parse", "--verify", "--quiet", f"{commit}:{path}"]
    found = subprocess.run(argv, cwd=root, capture_output=True, check=False).stdout.strip()
    return _git(root, "cat-file", "blob", found.decode()) if found else None


# An experiment folder: h2, h4, and a follow-up such as h2_1 (or h2.1).
EXPERIMENT_DIR = r"h\d+([._]\d+)?"


def _experiment(parts: list[str], top: str) -> bool:
    return len(parts) >= 3 and parts[0] == top and re.fullmatch(EXPERIMENT_DIR, parts[1]) is not None


def is_immutable(path: str) -> bool:
    parts = path.split("/")
    if _experiment(parts, "docs"):
        return parts[2:] == ["PREREG.md"]
    return _experiment(parts, "experiments") and len(parts) >= 4 and parts[2] == "frozen"


def is_append_only(path: str) -> bool:
    parts = path.split("/")
    return _experiment(parts, "docs") and parts[2:] == ["DEVIATIONS.md"]


def _history(root: Path, path: str) -> list[str]:
    """Commits touching `path` in the history of HEAD, oldest first."""
    return list(reversed(_git(root, "log", "--format=%H", "--full-history", "HEAD", "--", path).decode().split()))


def history_violations(root: Path) -> list[str]:
    """Rules 1 and 2. Empty when every committed PREREG and pin is unchanged and every DEVIATIONS only grew."""
    if _git(root, "rev-parse", "--is-shallow-repository").strip() == b"true":
        raise PreregError("shallow clone: the PREREG check needs the full history (fetch-depth: 0)")
    if not _git(root, "rev-parse", "--verify", "--quiet", "HEAD").strip():
        return []
    added = _git(root, "log", "--format=", "--name-only", "--diff-filter=A", "HEAD", "--", "docs", "experiments")
    paths = sorted({p for p in added.decode().splitlines() if is_immutable(p) or is_append_only(p)})
    problems = []
    for path in paths:
        commits = _history(root, path)
        first = commits[0]
        target = root / path
        current = target.read_bytes() if target.exists() else None
        if is_immutable(path):
            original = _blob(root, first, path)
            # A merge that carries the file over unchanged lists it too, so compare content, not commits.
            for later in commits[1:]:
                blob = _blob(root, later, path)
                if blob != original:
                    what = "deleted" if blob is None else "changed"
                    problems.append(f"{path}: {what} by commit {later[:12]} after its first commit {first[:12]}")
            if current is None:
                problems.append(f"{path}: deleted from the working tree")
            elif current != original:
                problems.append(f"{path}: the working tree differs from its first commit {first[:12]}")
            continue
        previous = b""
        for commit in commits:
            blob = _blob(root, commit, path)
            if blob is None or not blob.startswith(previous):
                problems.append(f"{path}: commit {commit[:12]} rewrote or removed earlier entries (append-only)")
            previous = blob if blob is not None else previous
        if current is None or not current.startswith(previous):
            problems.append(f"{path}: the working tree rewrote or removed committed entries (append-only)")
    return problems


@dataclass(frozen=True, slots=True)
class Pin:
    reference: str  # the path (or rendered artifact) as the PREREG or DEVIATIONS names it
    pinned: str  # experiments/hN/frozen/...
    sha256: str


def pins(root: Path, experiment: str) -> dict[str, Pin]:
    """docs/hN/DEVIATIONS.md pin rows, keyed by reference; a later row for the same reference wins."""
    path = root / "docs" / experiment / "DEVIATIONS.md"
    if not path.exists():
        return {}
    out = {}
    for raw, pinned, digest in PIN_ROW.findall(path.read_text(encoding="utf-8")):
        ref = raw.strip().strip("`")
        out[ref] = Pin(ref, pinned, digest)
    return out


def frozen_table(root: Path, experiment: str) -> dict[str, str]:
    """The PREREG's frozen-file table: path -> sha256."""
    text = (root / "docs" / experiment / "PREREG.md").read_text(encoding="utf-8")
    return dict(FROZEN_ROW.findall(text))


def pinned_path(root: Path, experiment: str, reference: str) -> Path:
    """Where a PREREG reference lives now: itself if it is already a pin, else its DEVIATIONS pin."""
    if reference.startswith(f"experiments/{experiment}/frozen/"):
        return root / reference
    pin = pins(root, experiment).get(reference)
    if pin is None:
        raise PreregError(f"{reference}: no pin in docs/{experiment}/DEVIATIONS.md")
    return root / pin.pinned


def _references(text: str) -> set[str]:
    return set(PATH_REF.findall(text)) | {p for p, _ in FROZEN_ROW.findall(text)}


def reference_violations(root: Path) -> list[str]:
    """Rule 3, for every docs/hN/PREREG.md in the working tree."""
    problems = []
    for prereg in sorted((root / "docs").glob("h*/PREREG.md")):
        exp = prereg.parent.name
        frozen_dir, docs_dir = f"experiments/{exp}/frozen/", f"docs/{exp}/"
        text = prereg.read_text(encoding="utf-8")
        table = dict(FROZEN_ROW.findall(text))
        known = pins(root, exp)
        for pin in known.values():
            target = root / pin.pinned
            if not pin.pinned.startswith(frozen_dir):
                problems.append(f"docs/{exp}/DEVIATIONS.md: pin {pin.pinned} is outside {frozen_dir}")
            elif not target.is_file():
                problems.append(f"docs/{exp}/DEVIATIONS.md: pin {pin.pinned} is missing")
            elif sha256_file(target) != pin.sha256:
                problems.append(f"{pin.pinned}: sha256 differs from docs/{exp}/DEVIATIONS.md")
        for ref in sorted(_references(text)):
            if ref.startswith(frozen_dir) or ref.startswith(docs_dir):
                target = root / ref
                if ref in table and (not target.is_file() or sha256_file(target) != table[ref]):
                    problems.append(f"{prereg.relative_to(root)}: {ref} is missing or differs from its digest")
                continue
            if ref not in known:
                problems.append(
                    f"{prereg.relative_to(root)} references {ref}, outside {frozen_dir} and {docs_dir}; pin it"
                    f" under {frozen_dir} (for a committed PREREG, log the pin in {docs_dir}DEVIATIONS.md)"
                )
            elif ref in table and known[ref].sha256 != table[ref]:
                problems.append(f"{known[ref].pinned}: pinned digest differs from the PREREG's digest for {ref}")
    return problems


def prereg_violations(root: Path) -> list[str]:
    return history_violations(root) + reference_violations(root)
