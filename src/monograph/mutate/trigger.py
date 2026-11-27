"""Which pull requests run the mutation suite in CI.

The suite is slow, so it runs nightly, on demand, and on any pull request that touches what it guards: the
model, the generators, the check and gate code, a frozen experiment file, or a PREREG or DEVIATIONS file.

    git diff --name-only BASE HEAD | python -m monograph.mutate.trigger    # prints true or false
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable

PATTERNS: tuple[str, ...] = (
    r"src/monograph/core/.+",
    r"src/monograph/gen/.+",
    # the check and gate code: validators and gates, the mutation suite, the CLI that runs them, the
    # pre-commit hook and this workflow
    r"src/monograph/validate/.+",
    r"src/monograph/mutate/.+",
    r"src/monograph/cli\.py",
    r"tools/pre-commit",
    r"\.github/workflows/ci\.yml",
    r"experiments/[^/]+/frozen/.+",
    r"docs/[^/]+/(PREREG|DEVIATIONS)\.md",
)
_ANY = re.compile("|".join(f"(?:{p})" for p in PATTERNS))


def touches_gates(paths: Iterable[str]) -> bool:
    return any(_ANY.fullmatch(p.strip()) for p in paths if p.strip())


def main() -> None:
    print("true" if touches_gates(sys.stdin.read().splitlines()) else "false")


if __name__ == "__main__":
    main()
