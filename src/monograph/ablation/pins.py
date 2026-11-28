"""What H2.1 agents are sent, from the pinned copies under experiments/h2_1/frozen/ once they exist.

Before the PREREG (dev runs) the live artifacts are used; `monograph h2_1 prereg` copies them byte for byte
into experiments/h2_1/frozen/ and names each with its sha256 in docs/h2_1/PREREG.md. From then on every run
reads the pins: the system prompts, the tool definitions, the schema descriptions describe_schema returns,
the cells' DDL and projections, and the question set. A test run refuses to start if a pin no longer
matches the PREREG, or if code that executes (pinned under frozen/code/) or a live generated cell artifact
no longer equals its pin.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from monograph import wiring
from monograph.ablation.cells import CELLS, Cell
from monograph.agent.arms import ARMS, Arm
from monograph.gen.age import Projection
from monograph.gen.artifacts import AGE_FILE, DDL_FILE

FROZEN = Path("experiments/h2_1/frozen")
PREREG = Path("docs/h2_1/PREREG.md")
QUESTIONS = Path("fixtures/h2_1/questions.json")
PROMPTS = Path("src/monograph/agent/prompts")

# Code that executes in a run, pinned under frozen/code/ (the same relative path).
CODE: tuple[str, ...] = (
    "src/monograph/ablation/analysis.py",  # the decision rules
    "src/monograph/ablation/cells.py",
    "src/monograph/ablation/cli.py",
    "src/monograph/ablation/load.py",
    "src/monograph/ablation/pins.py",
    "src/monograph/ablation/questions.py",
    "src/monograph/ablation/run.py",
    "src/monograph/agent/arms.py",
    "src/monograph/agent/llm.py",
    "src/monograph/agent/loop.py",
    "src/monograph/qa/run.py",
    "src/monograph/qa/score.py",
    "src/monograph/tools/cypher_backend.py",
    "src/monograph/tools/freeform.py",
    "src/monograph/tools/registry.py",
    "src/monograph/tools/service.py",
    "src/monograph/tools/sql_backend.py",
)


def frozen(root: Path) -> bool:
    return (root / FROZEN / "questions.json").exists()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------------------------- reads


def prompt_versions(root: Path) -> dict[str, str]:
    from monograph.ablation.run import PROMPT_VERSIONS

    return dict(PROMPT_VERSIONS)


def prompt_files() -> dict[str, str]:
    """Pinned name -> live path of every prompt H2.1 sends."""
    from monograph.ablation.run import PROMPT_VERSIONS

    out = {f"base_{PROMPT_VERSIONS['base']}.md": f"{PROMPTS}/base_{PROMPT_VERSIONS['base']}.md"}
    for arm in ARMS:
        name = f"arm_{arm}_{PROMPT_VERSIONS[arm]}.md"
        out[name] = f"{PROMPTS}/{name}"
    return out


def system_prompt(root: Path, arm: str) -> str:
    from monograph.ablation.run import PROMPT_VERSIONS
    from monograph.ablation.run import system_prompt as live

    if not frozen(root):
        return live(arm)
    v = PROMPT_VERSIONS
    base = (root / FROZEN / f"base_{v['base']}.md").read_text(encoding="utf-8")
    addendum = (root / FROZEN / f"arm_{arm}_{v[arm]}.md").read_text(encoding="utf-8")
    return base.rstrip() + "\n\n" + addendum.rstrip()


def questions(root: Path) -> dict[str, Any]:
    path = root / FROZEN / "questions.json" if frozen(root) else root / QUESTIONS
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def ddl(root: Path, cell: Cell) -> str:
    path = (
        root / FROZEN / cell.artifact(DDL_FILE)
        if frozen(root)
        else wiring.generated_dir(root) / cell.artifact(DDL_FILE)
    )
    return path.read_text(encoding="utf-8")


def projection(root: Path, cell: Cell) -> Projection:
    name = cell.artifact(AGE_FILE)
    path = root / FROZEN / name if frozen(root) else wiring.generated_dir(root) / name
    return Projection.parse(path.read_text(encoding="utf-8"))


def schema_tool_description(root: Path) -> str:
    from monograph.ablation.run import SCHEMA_DESCRIPTION

    return SCHEMA_DESCRIPTION


def tools_file(arm: str, identity: bool) -> str:
    return f"tools.{arm}.{'id-on' if identity else 'id-off'}.json"


def schema_file(cell: Cell, store: str) -> str:
    return f"schema.{cell.name}.{store}.txt"


def render_tools(tools: Sequence[dict[str, Any]]) -> bytes:
    """The bytes Arm.tools_hash() hashes, so a pinned tools file and the run records agree."""
    return json.dumps(list(tools), sort_keys=True).encode("utf-8")


def pin_arm(arm: Arm, root: Path, cell: Cell) -> Arm:
    """Once pinned: the tool definitions sent and the schema text describe_schema returns come from the pins;
    every other call goes to the live store."""
    if not frozen(root):
        return arm
    tools = tuple(json.loads((root / FROZEN / tools_file(arm.name, cell.identity)).read_text(encoding="utf-8")))
    if arm.interface == "tools":
        return replace(arm, tools=tools)
    text = (root / FROZEN / schema_file(cell, arm.store)).read_text(encoding="utf-8")
    live = arm.execute

    def execute(tool: str, arguments: dict[str, Any]) -> str:
        if tool == "describe_schema":
            return text
        return live(tool, arguments)

    return replace(arm, tools=tools, execute=execute)


# ------------------------------------------------------------------------------------------- checks

PIN_ROW = re.compile(r"^\| `(experiments/h2_1/frozen/[^`]+)` \| `([0-9a-f]{64})` \|$", re.MULTILINE)


def pinned_hashes(root: Path) -> dict[str, str]:
    """Pinned path -> sha256, from the PREREG's frozen-file table."""
    path = root / PREREG
    if not path.exists():
        return {}
    return dict(PIN_ROW.findall(path.read_text(encoding="utf-8")))


def frozen_drift(root: Path) -> list[str]:
    """Why a test run must not start: a pin missing or changed against the PREREG, executing code that no
    longer equals its pin, or a live generated cell artifact that no longer equals its pin."""
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
    for cell in CELLS:
        for file in (DDL_FILE, AGE_FILE):
            name = cell.artifact(file)
            pin, live = root / FROZEN / name, wiring.generated_dir(root) / name
            if not live.exists() or not pin.exists() or pin.read_bytes() != live.read_bytes():
                problems.append(f"generated/{name}")
    return sorted(set(problems))
