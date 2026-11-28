"""Pin what H2.1 agents are sent under experiments/h2_1/frozen/ and render docs/h2_1/PREREG.md.

The PREREG names only pinned copies (the prereg gate's reference rule), each with its sha256; it is written
once (`monograph h2_1 prereg` refuses to overwrite it) and the prereg gate keeps it immutable afterwards.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from monograph import wiring
from monograph.ablation import analysis, pins
from monograph.ablation.cells import BASELINE, CELLS, HARDEST, IDENTITY_FIELDS
from monograph.ablation.questions import (
    FANOUT_MAX,
    FANOUT_MIN,
    MAX_GOLD,
    STEP_RELATIONS,
    TEMPLATES,
    TYPES,
)
from monograph.ablation.run import (
    CROSSWALK,
    HAIKU,
    PROMPT_VERSIONS,
    SCHEMA_DESCRIPTION,
    SETTINGS,
    SONNET,
)
from monograph.agent.arms import ARMS, free_arm, tools_arm
from monograph.gen.artifacts import AGE_FILE, DDL_FILE
from monograph.qa.score import FAILURES
from monograph.tools.freeform import CypherRunner, SqlRunner, graph_schema_text, sql_schema_text
from monograph.tools.registry import RESOLVE_EXACT_DESCRIPTION, ROW_CAP, TOOLS
from monograph.tools.service import ToolService

FROZEN = pins.FROZEN


def _definitions(arm: str, identity: bool, root: Path) -> tuple[dict[str, Any], ...]:
    """The tool definitions an arm is sent, built exactly as a run builds them (no store is touched)."""
    cell = next(c for c in CELLS if c.identity is identity)
    if arm in ("S-tools", "G-tools"):
        return tools_arm(arm, ToolService(None, identity=identity)).tools  # type: ignore[arg-type]
    if arm == "S-free":
        return free_arm(arm, SqlRunner(None, pins.ddl(root, cell)), SCHEMA_DESCRIPTION).tools  # type: ignore[arg-type]
    return free_arm(arm, CypherRunner(None, pins.projection(root, cell)), SCHEMA_DESCRIPTION).tools  # type: ignore[arg-type]


def write_pins(root: Path) -> dict[str, str]:
    """Copy every artifact the agents are sent, and the code that runs, into experiments/h2_1/frozen/.
    Returns pinned path -> sha256 (code pins excluded: they are compared byte for byte with the live files)."""
    frozen = root / FROZEN
    if frozen.exists():
        raise FileExistsError(f"{FROZEN} exists; pins are written once")
    frozen.mkdir(parents=True)
    written: dict[str, bytes] = {}
    for name, live in pins.prompt_files().items():
        written[name] = (root / live).read_bytes()
    written["questions.json"] = (root / pins.QUESTIONS).read_bytes()
    for cell in CELLS:
        ddl = (wiring.generated_dir(root) / cell.artifact(DDL_FILE)).read_bytes()
        age = (wiring.generated_dir(root) / cell.artifact(AGE_FILE)).read_bytes()
        written[cell.artifact(DDL_FILE)] = ddl
        written[cell.artifact(AGE_FILE)] = age
        written[pins.schema_file(cell, "sql")] = sql_schema_text(ddl.decode("utf-8")).encode("utf-8")
        projection = pins.projection(root, cell)
        written[pins.schema_file(cell, "graph")] = graph_schema_text(projection).encode("utf-8")
    for arm in ARMS:
        for identity in (True, False):
            written[pins.tools_file(arm, identity)] = pins.render_tools(_definitions(arm, identity, root))
    for name, data in written.items():
        (frozen / name).write_bytes(data)
    for rel in pins.CODE:
        target = frozen / "code" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / rel, target)
    return {f"{FROZEN}/{name}": pins.sha256(data) for name, data in sorted(written.items())}


def _table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(x) for x in row) + " |" for row in rows]
    return out


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def render(
    root: Path,
    hashes: Mapping[str, str],
    datasets: Mapping[str, Mapping[str, Any]],
    loads: Mapping[str, Any],
    extension: Mapping[str, Any],
    counts: Mapping[str, Mapping[str, int]],
    dev: analysis.Table,
    dev_log: Sequence[Mapping[str, Any]],
    cost: Mapping[str, Any],
    limitations: Sequence[str],
    gaps: Sequence[str],
) -> str:
    base = (root / FROZEN / f"base_{PROMPT_VERSIONS['base']}.md").read_text(encoding="utf-8").rstrip()
    lines: list[str] = [
        "# H2.1 pre-registration",
        "",
        "Committed before the first test run. `monograph h2_1 run --split test` refuses to start from a dirty"
        " tree, if any file in the frozen table below no longer matches its sha256, if code that executes no"
        " longer equals its pinned copy, or if a generated cell artifact no longer equals its pin. Every file the"
        " agents are sent is read from these pins. Any change after this commit is a logged deviation in"
        " `docs/h2_1/DEVIATIONS.md`, and both results are reported.",
        "",
        "## Question and design",
        "",
        "H2 failed at a ceiling (every arm 0.97 to 1.00 on multi-hop and identity questions). Its report inferred"
        " that the identity layer, not the store, did the work. H2.1 measures that inference and gives the graph"
        " store a fair chance on deep and variable-length paths. Two ablation factors are crossed with H2's four"
        " arms (S-free text-to-SQL, S-tools, G-free text-to-Cypher, G-tools):",
        "",
        *_table(
            ["Factor", "On", "Off"],
            [
                [
                    "Identity layer",
                    "as in H2",
                    "data as ingested: no derived cross-source mapping in either store (the tables, columns and"
                    " edge labels do not exist)",
                ],
                [
                    "Schema descriptions",
                    "as in H2: DDL column comments, node/edge property descriptions",
                    "names and types only, in both stores",
                ],
            ],
        ),
        "",
        "Cells: "
        + ", ".join(f"`{c.name}`" for c in CELLS)
        + f" (baseline `{BASELINE.name}`, hardest `{HARDEST.name}`).",
        "",
        "Configurations (22): `claude-haiku-5-5` runs the full factorial, 4 cells x 4 arms; `claude-sonnet-5-5`"
        " runs S-free, G-free and G-tools in the baseline and the hardest cell. **Deviation from the brief,"
        " decided by the user on 2026-11-28 before any run:** the brief's Sonnet set was S-free and G-tools only;"
        " G-free was added because P3 and the refined verdict compare G-free with S-free for each model."
        " **Sonnet's test runs cover T3, T9, T10 and T11 only, decided by the user on 2026-11-28** after the dev"
        " cost estimate for the full set (USD 52.34) exceeded the USD 40 stop, and before this PREREG. Dev ran"
        " every type for every configuration; T1, T4 and T6 were at the ceiling there. P3 and the refined verdict"
        " (T9+T10) are covered for both models; P1 and P2 are judged on Haiku, as specified.",
        "",
        "### The identity layer",
        "",
        "Removed with identity off, from the data and from the generated schema of both stores:",
        "",
        *_table(["Class", "Field"], [[c, f] for c, f in IDENTITY_FIELDS]),
        "",
        "Firms with identity off: a labeler is identified as the NDC directory identifies it, by labeler code (the"
        " same organizations and IRIs as with identity on, without aliases); a recalling firm is identified as an"
        " enforcement record identifies it, by name (one organization per slug of the name within enforcement"
        " records), never merged with a labeler, under `organization/recalling-firm-<slug>`. Relations that"
        " the source records carry are kept in every cell: a recall names products, a label covers products, a"
        " package belongs to a product and is labeled by a firm, a product has active ingredients.",
        "",
        "With identity off, `resolve_entity` matches canonical identifiers and names exactly as written; its"
        " description is the only tool definition that differs between cells:",
        "",
        "```",
        RESOLVE_EXACT_DESCRIPTION,
        "```",
        "",
        "### Isolation",
        "",
        "Each cell is its own database with its own two read-only roles (refused by the other cells' databases),"
        " so no comment is readable from a descriptions-off cell. After loading, both stores of every cell were"
        " compared in full (every vertex and edge with its properties against the rows it comes from): identical"
        " in every cell. Responses are cached with the cell and arm in the key, so no cell replays another's"
        " samples.",
        "",
        "## Data",
        "",
        "The H2 frame plus a targeted extension for high fan-out questions: NDC directory records for the"
        f" {len(extension.get('substance_anchors', []))} frame substances in the most frame products and the"
        f" {len(extension.get('labeler_anchors', []))} frame labeler codes on the most frame products (one page of"
        f" 100 each; {extension.get('records')} records, {extension.get('new_products')} new products), with"
        " RxNav for the new products. Multi-ingredient extension products without a cached SPL are rejected by"
        " H2's pairing rule.",
        "",
        *_table(
            ["Identity", *sorted(next(iter(datasets.values()))["instances"])],
            [
                [name, *(v["instances"][k] for k in sorted(v["instances"]))]
                for name, v in sorted(datasets.items(), reverse=True)
            ],
        ),
        "",
        *_table(
            ["Cell", "rows", "vertices", "edges", "stores identical"],
            [
                [c, sum(v["rows"].values()), sum(v["vertices"].values()), sum(v["edges"].values()), "yes"]
                for c, v in loads.items()
            ],
        ),
        "",
        "## Models and settings",
        "",
        "`claude-haiku-5-5` and `claude-sonnet-5-5`; the same settings for every arm, model and cell:",
        "",
        *_table(
            ["Setting", "Value"],
            [
                ["max_tokens per response", SETTINGS.max_tokens],
                ["effort", SETTINGS.effort],
                ["thinking", SETTINGS.thinking],
                ["data tool calls per question", f"at most {SETTINGS.max_tool_calls} (submit_answer does not count)"],
                ["model turns per question", f"at most {SETTINGS.max_turns}"],
                ["output tokens per question", f"at most {SETTINGS.max_output_tokens}"],
                ["tool result text sent to the model", f"at most {SETTINGS.result_char_cap} characters"],
                ["rows per query or tool result", f"at most {ROW_CAP}"],
                ["statement timeout", "10 s (each cell's read-only roles, each seeing only its own store)"],
                ["refusal fallbacks", "none: a refusal is recorded"],
            ],
        ),
        "",
        "## Frozen prompts and tools",
        "",
        "One base prompt for every arm and cell; the arm's addendum names its tools. Prompt versions: "
        + ", ".join(f"{k} {v}" for k, v in PROMPT_VERSIONS.items())
        + ".",
        "",
        *_table(
            ["Arm", "system prompt sha256", "tools sha256, identity on", "tools sha256, identity off"],
            [
                [
                    arm,
                    f"`{pins.sha256(pins.system_prompt(root, arm).encode('utf-8'))}`",
                    f"`{hashes[f'{FROZEN}/{pins.tools_file(arm, True)}']}`",
                    f"`{hashes[f'{FROZEN}/{pins.tools_file(arm, False)}']}`",
                ]
                for arm in ARMS
            ],
        ),
        "",
        f"Base prompt ({PROMPT_VERSIONS['base']}):",
        "",
        "```",
        base,
        "```",
        "",
    ]
    for arm in ARMS:
        text = (root / FROZEN / f"arm_{arm}_{PROMPT_VERSIONS[arm]}.md").read_text(encoding="utf-8").rstrip()
        lines += [f"{arm} addendum ({PROMPT_VERSIONS[arm]}):", "", "```", text, "```", ""]
    lines += [
        "Tools arms: H2's registry, identical names, inputs, outputs and descriptions in S-tools and G-tools: "
        + ", ".join(f"`{t.name}`" for t in TOOLS)
        + ". Free-form arms: `describe_schema` ("
        + SCHEMA_DESCRIPTION
        + ") and `run_sql` or `run_cypher`, as in H2. Each cell's schema text is pinned per store.",
        "",
        "## Questions",
        "",
        f"{sum(sum(v.values()) for v in counts.values())} questions, split "
        + json.dumps({k: counts[k] for k in TYPES if k in counts})
        + f", committed in `{FROZEN}/questions.json`. Gold is computed by code over the validated instances of"
        " each identity level, never by an LLM; phrasings are fixed strings written by hand. The identity"
        " reference of a T3 or T4 question is resolved with the identity layer (the recorded pair defines what the"
        " question refers to); every other hop follows the cell's own data. T1, T9, T10, T11 and T6 use only"
        " relations that are the same in every cell, and their gold is the same in every cell (checked at"
        f" generation). A step (T9, T10) is one of: {', '.join(STEP_RELATIONS)}, in either direction. Answer sets"
        f" have 1 to {MAX_GOLD} ids, T11 {FANOUT_MIN} to {FANOUT_MAX}. Every question's tool plan reproduced its"
        " gold on the SQL store of every cell with no truncated result and no result over the character cap, and"
        " was re-checked on every cell's Cypher store (0 disagreements).",
        "",
        *_table(
            ["Template", "Type", "Phrasings"],
            [[f"`{t.id}`" + (f" ({t.note})" if t.note else ""), t.type, " / ".join(t.phrasings)] for t in TEMPLATES],
        ),
        "",
        "## Metrics",
        "",
        "Per model x cell x arm x type, on test, each question run once:",
        "",
        "* **Primary:** accuracy (exact set match for ids, ids compared as canonical IRIs with the part after"
        " the base accepted; exact value for counts; `none` or an empty set for T6); for T11, set F1, with"
        " exact match secondary.",
        "* Failure categories for wrong answers, first that applies: "
        + "; ".join(
            [
                *FAILURES[:2],
                CROSSWALK + " (identity off only: a call names an identity-layer table,"
                " column, edge label or option the cell lacks)",
                *FAILURES[2:],
            ]
        )
        + ".",
        "* Tool calls, tokens (input, cache read, cache write, output), USD per correct answer, agent latency"
        " p50/p95, backend execution latency per tool and backend (SQL vs Cypher, p50/p95) on T9 to T11, and"
        " AGE limitations hit per kind (H2's classifier).",
        f"* 95% percentile bootstrap intervals ({analysis.RESAMPLES} resamples, seed {analysis.SEED}), paired"
        " over questions for every difference.",
        "",
        "## Predictions and decision rules",
        "",
        f"* **P1, identity effect** ({HAIKU}, descriptions on): combined T3+T4 accuracy, identity on minus"
        f" identity off, per arm. **Confirmed** if the drop is at least {analysis.P1_CONFIRMED_DROP:.2f} in every"
        f" arm; **refuted** if it is under {analysis.P1_REFUTED_BELOW:.2f} in any arm (checked second); otherwise"
        " partly confirmed.",
        f"* **P2, description effect** ({HAIKU}, identity on): over every type but T6, per question, (drop for"
        " S-free plus drop for G-free)/2 minus (drop for S-tools plus drop for G-tools)/2, where a drop is the score"
        f" with descriptions on minus off. **Holds** if the mean is at least {analysis.P2_MARGIN:.2f}. The"
        " identity-off interaction is reported as secondary.",
        f"* **P3, store effect on hard paths** (baseline cell): G-free minus S-free on combined T9+T10, excluding"
        f" non-informative types. **Holds** if, for at least one model, it is at least {analysis.P3_MARGIN:.2f} with"
        " the 95% interval excluding 0. Tool-arm store effects are identical by construction and not judged.",
        "* **P1 sensitivity check** (reported, does not change the verdict): P1 recomputed without the moiety"
        " questions whose gold the recorded moiety links may understate (a substance in some product whose name"
        " starts with the moiety's name has no recorded link to it). Sensitivity set: "
        + (", ".join(f"`{g}`" for g in gaps) or "none")
        + ".",
        "* **Refined H2** (the graph store helps free-form agents on deep and variable-length paths): **holds** if"
        f" P3 holds; **fails** if G-free is within {analysis.REFINED_FAILS_WITHIN:.2f} of S-free on combined T9+T10"
        " for both models; otherwise **partly holds**.",
        f"* **Ceiling rule:** a type on which every arm of both models scores at least {analysis.CEILING:.2f} in"
        " the baseline cell on dev is non-informative and excluded from P3.",
        "",
        "### Ceiling marks (dev, baseline cell)",
        "",
    ]
    ceiling = analysis.ceiling(dev)
    columns = [f"{m} {a}" for m in (HAIKU, SONNET) for a in ARMS if any(f"{m} {a}" in r for r in ceiling.values())]
    lines += _table(
        ["Type", *columns, "non-informative"],
        [
            [q, *(_fmt(ceiling[q].get(c)) for c in columns), "yes" if q in analysis.non_informative(dev) else "no"]
            for q in TYPES
            if q in ceiling
        ],
    )
    excluded = [q for q in analysis.non_informative(dev) if q in analysis.PATH_TYPES]
    lines += [
        "",
        "Non-informative types: "
        + (", ".join(analysis.non_informative(dev)) or "none")
        + ". Excluded from P3: "
        + (", ".join(excluded) or "none")
        + ".",
        "",
        "## Dev iteration (logged per arm)",
        "",
        "Prompts could be tuned on the dev questions only. Runs per configuration and prompt version:",
        "",
        *_table(
            ["Model", "Cell", "Arm", "Prompt versions", "Dev runs", "Dev score"],
            [[d["model"], d["cell"], d["arm"], d["versions"], d["runs"], _fmt(d["score"])] for d in dev_log],
        ),
        "",
        "## Cost",
        "",
        f"Dev spend recorded: USD {cost['dev_usd']:.2f}. Test estimate from dev (USD per dev question, per"
        " configuration and type, times the test questions it covers): USD"
        f" {cost['test_estimate_usd']:.2f}. The user approved this set on 2026-11-28 at an estimate of USD 41.32,"
        " against the USD 40 stop.",
        "",
        "## Known limitations",
        "",
        *(f"* {x}" for x in limitations),
        "",
        "## Frozen files",
        "",
        *_table(["File", "sha256"], [[f"`{p}`", f"`{h}`"] for p, h in sorted(hashes.items())]),
        "",
        "Code that executes in a run is pinned under `experiments/h2_1/frozen/code/` and must equal the live"
        " files byte for byte: " + ", ".join(Path(p).name for p in pins.CODE) + ".",
        "",
    ]
    return "\n".join(lines)
