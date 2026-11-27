"""Render docs/h2/PREREG.md from the code as it is: prompts and their hashes, tool definitions, templates, the
frozen-file table and the rules. `monograph h2 run --split test` refuses to start if a frozen file changes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from monograph.agent.arms import ARMS, PROMPT_VERSIONS, load_prompt
from monograph.agent.llm import AGENT_SETTINGS
from monograph.qa.templates import TEMPLATES
from monograph.tools.freeform import CYPHER_DESCRIPTION, SCHEMA_DESCRIPTION, SQL_DESCRIPTION
from monograph.tools.registry import ROW_CAP, TOOLS, tool_schema

FROZEN: tuple[str, ...] = (
    "src/monograph/agent/prompts/base_v{base}.md",
    "src/monograph/agent/prompts/arm_S-free_v{S-free}.md",
    "src/monograph/agent/prompts/arm_S-tools_v{S-tools}.md",
    "src/monograph/agent/prompts/arm_G-free_v{G-free}.md",
    "src/monograph/agent/prompts/arm_G-tools_v{G-tools}.md",
    "src/monograph/agent/arms.py",
    "src/monograph/agent/loop.py",
    "src/monograph/agent/llm.py",
    "src/monograph/tools/registry.py",
    "src/monograph/tools/service.py",
    "src/monograph/tools/sql_backend.py",
    "src/monograph/tools/cypher_backend.py",
    "src/monograph/tools/freeform.py",
    "src/monograph/qa/score.py",
    "src/monograph/qa/templates.py",
    "fixtures/h2/questions.json",
    "generated/monograph.age.json",
    "generated/monograph.ddl.sql",
)


def frozen_paths() -> list[str]:
    out = []
    for template in FROZEN:
        path = template
        for k, v in PROMPT_VERSIONS.items():
            path = path.replace("{" + k + "}", v.removeprefix("v"))
        out.append(path)
    return out


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render_prereg(
    root: Path,
    hashes: Mapping[str, tuple[str, str]],
    dev_log: Sequence[Mapping[str, Any]],
    dataset: Mapping[str, Any],
    counts: Mapping[str, Mapping[str, int]],
    limitations: Sequence[str],
) -> str:
    s = AGENT_SETTINGS
    lines = [
        "# H2 pre-registration",
        "",
        "Committed before the first test run. `monograph h2 run --split test` refuses to start if any file in the",
        "frozen table below no longer matches its sha256. Any change after this commit is a logged deviation, and",
        "both results are reported.",
        "",
        "## Hypothesis and design",
        "",
        "H2: graph tools win on multi-hop and identity questions. Tested as a 2 x 2 of store (Postgres tables vs",
        "an Apache AGE graph in the same Postgres) and interface (free-form queries vs one shared tool registry):",
        "S-free (text-to-SQL), S-tools (the tools in SQL), G-free (text-to-Cypher), G-tools (the tools in Cypher).",
        "",
        "## Data",
        "",
        f"Loaded once into both stores from the same validated instances: {json.dumps(dataset, sort_keys=True)}.",
        "The graph is built from the tables following the generated projection, and every vertex label and edge",
        "source is counted on both sides at load time. The tool backends return identical results on the mini",
        "fixture (41 calls over every tool), and every question's gold was reproduced through the tools on both",
        "stores at generation time.",
        "",
        "## Models and settings",
        "",
        "`claude-sonnet-5-5` and `claude-haiku-5-5`, the same for every arm:",
        "",
        "| Setting | Value |",
        "|---|---|",
        f"| max_tokens per response | {s.max_tokens} |",
        f"| effort | {s.effort} |",
        f"| thinking | {s.thinking} |",
        f"| data tool calls per question | at most {s.max_tool_calls} (submit_answer does not count) |",
        f"| model turns per question | at most {s.max_turns} |",
        f"| output tokens per question | at most {s.max_output_tokens} |",
        f"| tool result text sent to the model | at most {s.result_char_cap} characters |",
        f"| rows per query or tool result | at most {ROW_CAP} |",
        "| statement timeout | 10 s (read-only roles h2_sql and h2_graph, each seeing only its own store) |",
        "| refusal fallbacks | none: a refusal is recorded |",
        "",
        "These models reject non-default sampling parameters, so fixed sampling means the fixed effort, thinking",
        "mode and output budget above. Responses are cached content-addressed, with the arm in the key: S-tools",
        "and G-tools send byte-identical prompts and receive identical tool results by design, and the arm tag",
        "keeps their samples independent instead of replaying one arm's responses for the other.",
        "",
        "## Frozen prompts",
        "",
        "Every arm gets the same base prompt; the arm's addendum names its tools.",
        "",
        "| Arm | system prompt sha256 | tools sha256 |",
        "|---|---|---|",
    ]
    for arm in ARMS:
        lines.append(f"| {arm} | `{hashes[arm][0]}` | `{hashes[arm][1]}` |")
    lines += [
        "",
        f"Base prompt ({PROMPT_VERSIONS['base']}):",
        "",
        "```",
        load_prompt("base", PROMPT_VERSIONS["base"]).rstrip(),
        "```",
        "",
    ]
    for arm in ARMS:
        lines += [
            f"{arm} addendum ({PROMPT_VERSIONS[arm]}):",
            "",
            "```",
            load_prompt(f"arm_{arm}", PROMPT_VERSIONS[arm]).rstrip(),
            "```",
            "",
        ]
    lines += [
        "## Tools",
        "",
        "Tools arms (identical names, inputs, outputs and descriptions in S-tools and G-tools):",
        "",
    ]
    for t in TOOLS:
        lines += [f"* `{t.name}`: {t.description}", f"  Input schema: `{json.dumps(tool_schema(t), sort_keys=True)}`"]
    lines += [
        "",
        "Free-form arms: `describe_schema` (" + SCHEMA_DESCRIPTION + ") and `run_sql` (" + SQL_DESCRIPTION + ") or",
        "`run_cypher` (" + CYPHER_DESCRIPTION + "). The SQL schema description is the generated DDL with its",
        "column comments; the graph description lists the generated labels, properties and edges with the same",
        "descriptions (a test checks every description appears in both).",
        "",
        "All arms: `submit_answer` with `kind` (ids, number, none), `ids`, `number`, `evidence`.",
        "",
        "## Questions",
        "",
        f"300 questions, 50 per type, split {json.dumps(counts, sort_keys=True)}, committed in",
        "`fixtures/h2/questions.json`. Gold is computed by code over the validated instances, never by an LLM.",
        "Phrasings are fixed strings written by hand. Templates:",
        "",
        "| Template | Type | Phrasings |",
        "|---|---|---|",
    ]
    for tpl in TEMPLATES:
        note = f" ({tpl.note})" if tpl.note else ""
        lines.append(f"| `{tpl.id}`{note} | {tpl.type} | " + " / ".join(tpl.phrasings) + " |")
    lines += [
        "",
        "## Metrics",
        "",
        "Per model x arm x question type, on test, each question run once:",
        "",
        "* **Primary: accuracy**, exact set match for id answers (ids compared as canonical IRIs; the part after",
        "  the base is accepted), exact value for counts, `none` (or an empty id set) for T6.",
        "* Secondary: set F1.",
        "* Failure categories for wrong answers, first that applies: refusal; budget exhausted (no answer for any",
        "  reason: tool, turn or token budget, truncation, stopping without answering); hallucinated id (an id not",
        "  in the database); wrong entity resolution (no tool input, result preview or result id mentions the",
        "  question's anchor entity by id or key); query error or timeout (a call failed and the answer is wrong);",
        "  missing hop (answer ids of a different kind than the gold, or a number for ids, or the reverse); wrong",
        "  set.",
        "* Cost: tool calls per question, tokens (input, cache read, cache write, output), USD per correct",
        "  answer, p50 and p95 latency (model plus tool time).",
        "* 95% percentile bootstrap intervals (2,000 resamples, seed 20261127) on accuracy and, paired over",
        "  questions, on every arm difference.",
        "",
        "## Decision rule",
        "",
        "Checked in this order:",
        "",
        "1. **H2 fails** if S-free is within 0.05 of G-tools on T2+T3+T4 for both models.",
        "2. **H2 holds** if, for at least one model, G-tools beats S-free by at least 0.10 accuracy on T2+T3+T4",
        "   with the interval excluding 0, and G-tools is no more than 0.05 below S-free on T1.",
        "3. Otherwise **H2 partly holds**.",
        "",
        "Reported regardless: the store effect (G-tools minus S-tools, G-free minus S-free) and the interface",
        "effect (S-tools minus S-free, G-tools minus G-free). If a store effect's interval includes 0 while an",
        "interface effect's does not, the report says plainly that the win comes from the tools, not the graph.",
        "",
        "## Dev iteration (logged per arm)",
        "",
        "Prompts could be tuned on the 60 dev questions only; dev was at the ceiling, so none was. Runs per arm"
        " and prompt version (including the rerun after the gold fix):",
        "",
        "| Arm | Model | Prompt version | Dev runs | Dev accuracy |",
        "|---|---|---|---|---|",
    ]
    for row in dev_log:
        lines.append(f"| {row['arm']} | {row['model']} | {row['version']} | {row['n']} | {row['accuracy']:.2f} |")
    lines += [
        "",
        "## Known limitations",
        "",
        *[f"* {x}" for x in limitations],
        "",
        "## Frozen files",
        "",
        "| File | sha256 |",
        "|---|---|",
    ]
    for p in frozen_paths():
        lines.append(f"| `{p}` | `{_sha(root / p)}` |")
    return "\n".join(lines) + "\n"
