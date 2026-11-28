"""What H1 agents are sent, from the pinned copies under experiments/h1/frozen/ once they exist.

Before the PREREG (dev runs) the live artifacts are used; `monograph h1 prereg` copies them byte for byte into
experiments/h1/frozen/ and names each with its sha256 in docs/h1/PREREG.md. From then on every run reads the
pins: the prompts, the tool definitions, the three contexts describe_data returns, the two mappings the check
reads, the warehouse and semantic-layer SQL the databases are built from, and the question set. A test run
refuses to start if a pin no longer matches the PREREG, or if code that executes (pinned under frozen/code/)
or a live generated artifact no longer equals its pin.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from monograph import wiring
from monograph.gen import semantic_layer as s
from monograph.gen import warehouse as w
from monograph.h1 import arms, build, context
from monograph.h1.questions import QUESTIONS
from monograph.sqlcheck.catalog import Catalog, from_files

FROZEN = Path("experiments/h1/frozen")
PREREG = Path("docs/h1/PREREG.md")
PROMPTS = Path("src/monograph/agent/prompts")

GENERATED: tuple[str, ...] = build.ARTIFACTS


# Code that executes in a run, pinned under frozen/code/ (the same relative path).
CODE: tuple[str, ...] = (
    "src/monograph/agent/budget.py",
    "src/monograph/agent/llm.py",
    "src/monograph/agent/loop.py",
    "src/monograph/h1/analysis.py",
    "src/monograph/h1/arms.py",
    "src/monograph/h1/build.py",
    "src/monograph/h1/cli.py",
    "src/monograph/h1/context.py",
    "src/monograph/h1/pins.py",
    "src/monograph/h1/questions.py",
    "src/monograph/h1/record.py",
    "src/monograph/h1/run.py",
    "src/monograph/h1/score.py",
    "src/monograph/qa/h1_templates.py",
    "src/monograph/qa/run.py",
    "src/monograph/sqlcheck/catalog.py",
    "src/monograph/sqlcheck/check.py",
    "src/monograph/sqlcheck/gate.py",
    "src/monograph/tools/freeform.py",
)


def frozen(root: Path) -> bool:
    return (root / FROZEN / "questions.json").exists()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prompt_files() -> dict[str, str]:
    """Pinned name -> live path of every prompt H1 sends."""
    v = arms.PROMPT_VERSIONS
    names = [f"h1_base_{v['base']}.md", f"h1_check_{v['check']}.md"]
    names += [f"h1_arm_{c}_{v[c]}.md" for c in context.CONTEXTS]
    return {n: f"{PROMPTS}/{n}" for n in names}


def _read(root: Path, pinned: str, live: Path) -> str:
    path = root / FROZEN / pinned if frozen(root) else live
    return path.read_text(encoding="utf-8")


def system_prompt(root: Path, arm: str) -> str:
    if not frozen(root):
        return arms.system_prompt(arm)
    v = arms.PROMPT_VERSIONS
    ctx = arms.CONTEXT[arm]
    parts = [f"h1_base_{v['base']}.md", f"h1_arm_{ctx}_{v[ctx]}.md"]
    if arms.checked(arm):
        parts.append(f"h1_check_{v['check']}.md")
    return "\n\n".join((root / FROZEN / p).read_text(encoding="utf-8").rstrip() for p in parts)


def context_file(ctx: str) -> str:
    return f"context.{ctx}.txt"


def describe_text(root: Path, ctx: str) -> str:
    if frozen(root):
        return (root / FROZEN / context_file(ctx)).read_text(encoding="utf-8")
    return context.describe(root, ctx)


def tools(root: Path) -> tuple[dict[str, Any], ...]:
    if frozen(root):
        return tuple(json.loads((root / FROZEN / "tools.json").read_text(encoding="utf-8")))
    return arms.TOOLS


def render_tools(defs: tuple[dict[str, Any], ...]) -> bytes:
    """The bytes Arm.tools_hash() hashes, so the pinned tools file and the run records agree."""
    return json.dumps(list(defs), sort_keys=True).encode("utf-8")


def catalog(root: Path) -> Catalog:
    base = root / FROZEN if frozen(root) else wiring.generated_dir(root)
    return from_files(base / w.MAPPING_FILE, base / s.MAPPING_FILE)


def generated(root: Path, name: str) -> str:
    return _read(root, name, wiring.generated_dir(root) / name)


def pinned_stamp(root: Path) -> str:
    """The stamp of the artifacts a run uses (pinned once frozen): the database must carry the same one."""
    base = root / FROZEN if frozen(root) else wiring.generated_dir(root)
    return build.stamp([(base / n).read_bytes() for n in GENERATED])


def questions_sha256(root: Path) -> str:
    """The question set a run uses (recorded on every run line, so dev rounds can be told apart)."""
    path = root / FROZEN / "questions.json" if frozen(root) else root / QUESTIONS
    return sha256(path.read_bytes())


def questions(root: Path) -> list[dict[str, Any]]:
    data = json.loads(_read(root, "questions.json", root / QUESTIONS))
    qs: list[dict[str, Any]] = data["questions"]
    return qs


# --------------------------------------------------------------------------------------------- pin


def write_pins(root: Path) -> dict[str, str]:
    """Copy every artifact a run reads into frozen/, byte for byte; return pinned path -> sha256."""
    out_dir = root / FROZEN
    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, bytes] = {}
    for name, live in prompt_files().items():
        files[name] = (root / live).read_bytes()
    for ctx in context.CONTEXTS:
        files[context_file(ctx)] = context.describe(root, ctx).encode("utf-8")
    files["tools.json"] = render_tools(arms.TOOLS)
    for name in GENERATED:
        files[name] = (wiring.generated_dir(root) / name).read_bytes()
    files["questions.json"] = (root / QUESTIONS).read_bytes()
    for name, data in files.items():
        (out_dir / name).write_bytes(data)
    for rel in CODE:
        pin = out_dir / "code" / rel
        pin.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / rel, pin)
    hashes = {f"{FROZEN}/{n}": sha256(d) for n, d in sorted(files.items())}
    hashes |= {f"{FROZEN}/code/{rel}": sha256((root / rel).read_bytes()) for rel in CODE}
    return hashes


# ------------------------------------------------------------------------------------------- checks

PIN_ROW = re.compile(r"^\| `(experiments/h1/frozen/[^`]+)` \| `([0-9a-f]{64})` \|$", re.MULTILINE)


def pinned_hashes(root: Path) -> dict[str, str]:
    path = root / PREREG
    if not path.exists():
        return {}
    return dict(PIN_ROW.findall(path.read_text(encoding="utf-8")))


def frozen_drift(root: Path) -> list[str]:
    """Why a test run must not start: a pin missing or changed against the PREREG, executing code that no
    longer equals its pin, or a live generated artifact (what the databases were built from) that no longer
    equals its pin."""
    hashes = pinned_hashes(root)
    if not hashes:
        return [str(PREREG)]
    problems = []
    for rel, digest in sorted(hashes.items()):
        path = root / rel
        if not path.exists() or sha256(path.read_bytes()) != digest:
            problems.append(rel)
    for rel in CODE:
        pin, live = root / FROZEN / "code" / rel, root / rel
        if not pin.exists() or not live.exists() or pin.read_bytes() != live.read_bytes():
            problems.append(rel)
    for name in GENERATED:
        pin, live = root / FROZEN / name, wiring.generated_dir(root) / name
        if not pin.exists() or not live.exists() or pin.read_bytes() != live.read_bytes():
            problems.append(f"generated/{name}")
    return sorted(set(problems))
