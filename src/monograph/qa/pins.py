"""H2's frozen artifacts, read from their pins under experiments/h2/frozen/ (docs/h2/DEVIATIONS.md, D1).

A rerun sends the pinned system prompts and tool definitions, returns the pinned schema descriptions and
loads the pinned question set, never the live paths. The code that executes still lives in src/, so a test
run is refused unless each frozen code file equals its pin, and unless the live DDL and projection equal
theirs: the database is loaded from the live model, so it must still be the store the pins describe.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

from monograph.agent.arms import ARMS, PROMPT_VERSIONS, Arm, sha256
from monograph.gen.age import Projection
from monograph.validate.prereg import PreregError, frozen_table, pinned_path, pins, sha256_file

EXPERIMENT = "h2"
QUESTIONS_REF = "fixtures/h2/questions.json"
DDL_REF = "generated/monograph.ddl.sql"
AGE_REF = "generated/monograph.age.json"
SCHEMA_REFS = {"sql": "schema description, SQL", "graph": "schema description, graph"}


def _prompt_ref(name: str, version: str) -> str:
    return f"src/monograph/agent/prompts/{name}_{version}.md"


def _tools_ref(arm: str) -> str:
    return f"tools as sent, {arm}"


def _read(root: Path, reference: str) -> str:
    return pinned_path(root, EXPERIMENT, reference).read_text(encoding="utf-8")


def system_prompt(root: Path, arm: str) -> str:
    """The same assembly as `monograph.agent.arms.system_prompt`, from the pinned prompt files."""
    base = _read(root, _prompt_ref("base", PROMPT_VERSIONS["base"]))
    addendum = _read(root, _prompt_ref(f"arm_{arm}", PROMPT_VERSIONS[arm]))
    return base.rstrip() + "\n\n" + addendum.rstrip()


def tools(root: Path, arm: str) -> tuple[dict[str, Any], ...]:
    return tuple(json.loads(_read(root, _tools_ref(arm))))


def schema_text(root: Path, store: str) -> str:
    return _read(root, SCHEMA_REFS[store])


def questions(root: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_read(root, QUESTIONS_REF))
    return data


def projection(root: Path) -> Projection:
    return Projection.parse(_read(root, AGE_REF))


def ddl(root: Path) -> str:
    return _read(root, DDL_REF)


def hashes(root: Path) -> dict[str, tuple[str, str]]:
    """arm -> (system prompt sha256, tools sha256), computed from the pins as the runner computes them."""
    out = {}
    for arm in ARMS:
        defs = tools(root, arm)
        out[arm] = (sha256(system_prompt(root, arm)), sha256(json.dumps(list(defs), sort_keys=True)))
    return out


def pin_arm(arm: Arm, root: Path) -> Arm:
    """`arm` with the pinned tool definitions, and the pinned schema description for describe_schema."""
    text = schema_text(root, arm.store)
    live = arm.execute

    def execute(tool: str, arguments: dict[str, Any]) -> str:
        if tool == "describe_schema" and arm.interface == "free":
            live(tool, arguments)  # same argument validation as before
            return text
        return live(tool, arguments)

    return dataclasses.replace(arm, tools=tools(root, arm.name), execute=execute)


def pin_problems(root: Path) -> list[str]:
    """Integrity of the pins alone (a CI test): each frozen-table entry has a pin hashing to the PREREG
    digest, each DEVIATIONS pin hashes to its row, and the pinned prompts and tools hash to the PREREG's
    per-arm digests. Live files are not compared, so the live model is free to evolve."""
    prereg = root / "docs" / EXPERIMENT / "PREREG.md"
    if not prereg.exists():
        return ["docs/h2/PREREG.md"]
    out = []
    for path, digest in frozen_table(root, EXPERIMENT).items():
        try:
            pin = pinned_path(root, EXPERIMENT, path)
        except PreregError:
            out.append(f"{path} (no pin)")
            continue
        if not pin.is_file() or sha256_file(pin) != digest:
            out.append(str(pin.relative_to(root)))
    for entry in pins(root, EXPERIMENT).values():
        target = root / entry.pinned
        if not target.is_file() or sha256_file(target) != entry.sha256:
            out.append(entry.pinned)
    if out:
        return sorted(set(out))
    text = prereg.read_text(encoding="utf-8")
    for arm, (system, tool_hash) in hashes(root).items():
        if not re.search(rf"^\| {re.escape(arm)} \| `{system}` \| `{tool_hash}` \|$", text, re.MULTILINE):
            out.append(f"{arm} prompt or tools hash")
    return out


def drift(root: Path) -> list[str]:
    """Why a test rerun must be refused: a pin problem, or a live file that executes (frozen code, and the
    DDL and projection the database is loaded from) no longer equal to its pin."""
    out = pin_problems(root)
    if out:
        return out
    for path, digest in frozen_table(root, EXPERIMENT).items():
        live = root / path
        executes = path.endswith(".py") or path in (DDL_REF, AGE_REF)
        if executes and (not live.is_file() or sha256_file(live) != digest):
            out.append(path)
    return out
