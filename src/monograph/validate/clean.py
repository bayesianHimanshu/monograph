"""A test run starts only from a clean, committed tree, so every test result names the exact code that made it.

Clean means `git status` reports nothing outside data/ and results/: no modified, staged, deleted or
untracked file (files .gitignore excludes are not untracked). data/ holds caches and raw downloads and
results/ is the append-only output, so neither counts. The commit SHA verified here is recorded on every
result line the run writes (`clean_commit`).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

EXEMPT = ("data", "results")


class DirtyTree(RuntimeError):
    """The working tree has changes outside data/ and results/, or git cannot tell."""


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise DirtyTree(f"git {' '.join(args)} failed: {done.stderr.strip() or 'not a git repository'}")
    return done.stdout


def dirty_paths(root: Path) -> list[str]:
    """`git status` entries outside data/ and results/, as `XY path` lines."""
    top = Path(_git(root, "rev-parse", "--show-toplevel").strip())
    if top.resolve() != root.resolve():
        raise DirtyTree(f"{root} is not the repository root ({top})")
    out = _git(root, "status", "--porcelain=v1", "--untracked-files=all", "--", ".", *(f":!{d}" for d in EXEMPT))
    return [line for line in out.splitlines() if line.strip()]


def require_clean(root: Path) -> str:
    """The HEAD commit SHA, if the tree is clean; otherwise DirtyTree naming what is dirty."""
    dirty = dirty_paths(root)
    if dirty:
        shown = ", ".join(line[3:] for line in dirty[:10]) + (f" and {len(dirty) - 10} more" if len(dirty) > 10 else "")
        raise DirtyTree(f"the working tree is dirty outside data/ and results/: {shown}")
    return _git(root, "rev-parse", "--verify", "HEAD").strip()
