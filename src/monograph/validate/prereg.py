"""Pre-registrations are immutable: every docs/h*/PREREG.md, once committed, must stay byte-identical to the
version in the commit that added it. No later commit may touch it, and the working tree must match.

The check reads git history, so it refuses to pass on a shallow clone instead of comparing against a
truncated history. Nothing in the code writes a committed PREREG.md: `h2 prereg` refuses an existing file,
and the generators only write generated/.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

PATTERN = "docs/h*/PREREG.md"


class PreregError(RuntimeError):
    """The history needed for the check is not available."""


def _git(root: Path, *args: str) -> bytes:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
    if done.returncode != 0:
        raise PreregError(f"git {' '.join(args)}: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def _is_prereg(path: str) -> bool:
    parts = path.split("/")
    return len(parts) == 3 and parts[0] == "docs" and parts[1].startswith("h") and parts[2] == "PREREG.md"


def prereg_violations(root: Path) -> list[str]:
    """One message per violation; empty when every committed PREREG.md is unchanged since its first commit."""
    if _git(root, "rev-parse", "--is-shallow-repository").strip() == b"true":
        raise PreregError("shallow clone: the PREREG check needs the full history (fetch-depth: 0)")
    if not _git(root, "rev-parse", "--verify", "--quiet", "HEAD").strip():
        return []
    # Every PREREG.md ever added in the history of HEAD, including ones deleted since.
    added = _git(root, "log", "--format=", "--name-only", "--diff-filter=A", "HEAD", "--", PATTERN)
    paths = sorted({p for p in added.decode().splitlines() if _is_prereg(p)})
    problems = []
    for path in paths:
        commits = _git(root, "log", "--format=%H", "--full-history", "HEAD", "--", path).decode().split()
        first = commits[-1]
        original = _git(root, "rev-parse", f"{first}:{path}").strip()
        for later in reversed(commits[:-1]):
            # A merge that carries the file over unchanged lists it too, so compare content, not commits.
            spec = f"{later}:{path}"
            argv = ["git", "rev-parse", "--verify", "--quiet", spec]
            found = subprocess.run(argv, cwd=root, capture_output=True, check=False)
            blob = found.stdout.strip()
            if blob != original:
                what = "deleted" if not blob else "changed"
                problems.append(f"{path}: {what} by commit {later[:12]} after its first commit {first[:12]}")
        target = root / path
        if not target.exists():
            problems.append(f"{path}: deleted from the working tree")
        elif target.read_bytes() != _git(root, "cat-file", "blob", original.decode()):
            problems.append(f"{path}: the working tree differs from its first commit {first[:12]}")
    return problems
