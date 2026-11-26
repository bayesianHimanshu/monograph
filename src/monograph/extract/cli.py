"""`monograph h4 ...` commands."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from monograph import wiring

h4_app = typer.Typer(no_args_is_help=True, help="H4: LLM extraction with record-local and graph-context repair.")
Root = Annotated[Path, typer.Option("--root", help="Repository root.")]

SEED = 20261126
FRAME_FILE = Path("fixtures/h4/frame.json")
SPLIT_FILE = Path("fixtures/h4/split.json")


def _dailymed(root: Path) -> Any:
    import httpx

    from monograph.extract.sources.dailymed import DailyMedClient, SplCache

    cache = SplCache(root / "data" / "raw" / "spl")
    return DailyMedClient(httpx.Client(timeout=60.0), cache, 0.5, time.sleep, time.monotonic), cache


@h4_app.command("fetch")
def fetch(
    root: Root = Path(),
    pages: Annotated[int, typer.Option(help="Listing pages sampled per document type.")] = 4,
    per_cell: Annotated[int, typer.Option(help="Stop fetching once every cell has this many candidates.")] = 40,
) -> None:
    """Fetch SPLs from DailyMed (cached in data/raw/spl/) and write the sampling frame."""
    from monograph.extract.fetch import FrameBuilder
    from monograph.extract.gap import gap_ndcs, h6_gap_products
    from monograph.ingest.openfda import RawCache
    from monograph.validate.pydantic_side import PydanticValidator

    spec = wiring.build_spec()
    gap = h6_gap_products(spec, RawCache(wiring.raw_dir(root)).all("ndc"), PydanticValidator(wiring.today()))
    client, cache = _dailymed(root)
    builder = FrameBuilder(client, cache, SEED, typer.echo)
    report = builder.build(gap_ndcs(gap), pages, per_cell)
    targeted = _targeted_set_ids(root)
    typer.echo(f"top-up: {len(targeted)} set IDs from targeted openFDA label searches")
    builder.top_up(targeted, report)
    frame = {
        "seed": SEED,
        "h6_gap_products": len(gap),
        "h6_gap_lookup": report.gap_lookup,
        "not_on_dailymed": sorted(set(report.not_on_dailymed) - set(client.server_errors)),
        "dailymed_server_errors": sorted(client.server_errors),
        "entries": [
            {
                "set_id": e.set_id,
                "version": e.version,
                "stratum": e.stratum.key(),
                "h6_gap": e.h6_gap,
                "exclusion": e.exclusion,
            }
            for e in sorted(report.entries, key=lambda e: e.set_id)
        ],
    }
    path = root / FRAME_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(frame, indent=1, sort_keys=True) + "\n")
    typer.echo(f"frame: {len(report.entries)} SPLs parsed, {sum(e.exclusion is None for e in report.entries)} eligible")
    for cell, n in sorted(report.eligible_by_cell().items()):
        typer.echo(f"  {cell}: {n}")
    typer.echo(f"network calls: {client.network_calls}; frame written to {path}")
    split_path = root / SPLIT_FILE
    if split_path.exists():
        _fetch_registry(root, client, cache, json.loads(split_path.read_text()))
    else:
        typer.echo("no split yet: run `monograph h4 split`, then `monograph h4 fetch` again for the registry data")


def _sampled_set_ids(split_data: dict[str, Any]) -> list[str]:
    return sorted(r["set_id"] for r in split_data["dev"] + split_data["test"])


def _fetch_registry(root: Path, dailymed: Any, cache: Any, split_data: dict[str, Any]) -> None:
    import os

    import httpx

    from monograph.extract.registry import RegistryFetcher
    from monograph.ingest.openfda import OpenFdaClient, RawCache

    with httpx.Client(timeout=60.0) as http:
        openfda = OpenFdaClient(
            http, RawCache(wiring.raw_dir(root)), os.environ.get("OPENFDA_API_KEY"), 0.5, time.sleep, time.monotonic
        )
        RegistryFetcher(openfda, dailymed, cache).fetch(_sampled_set_ids(split_data), typer.echo)
        typer.echo(f"registry: {openfda.network_calls} openFDA calls")


# Targeted openFDA label searches that fill the cells random DailyMed pages leave thin. Fixed queries, cached.
TOP_UP_QUERIES: tuple[str, ...] = tuple(
    f'openfda.product_type:"{ptype}" AND {clause}'
    for ptype in ("HUMAN PRESCRIPTION DRUG", "HUMAN OTC DRUG")
    for clause in (
        'openfda.route:("INTRAVENOUS" OR "INTRAMUSCULAR" OR "SUBCUTANEOUS")',
        'openfda.route:"ORAL" AND spl_product_data_elements:("SOLUTION" OR "SUSPENSION" OR "SYRUP" OR "ELIXIR")',
    )
)


def _targeted_set_ids(root: Path) -> list[str]:
    import os

    import httpx

    from monograph.ingest.openfda import OpenFdaClient, RawCache

    out: list[str] = []
    with httpx.Client(timeout=60.0) as http:
        client = OpenFdaClient(
            http, RawCache(wiring.raw_dir(root)), os.environ.get("OPENFDA_API_KEY"), 0.5, time.sleep, time.monotonic
        )
        for query in TOP_UP_QUERIES:
            for skip in (0, 100):
                out += [str(r["set_id"]) for r in client.page("label", skip, 100, search=query) if r.get("set_id")]
    return out


@h4_app.command("split")
def split_cmd(
    root: Root = Path(),
    total: Annotated[int, typer.Option(help="Labels in the sample.")] = 200,
    dev: Annotated[int, typer.Option(help="Labels in the dev split.")] = 40,
    force: Annotated[bool, typer.Option(help="Replace an existing split (never after PREREG).")] = False,
) -> None:
    """Draw the stratified sample and the dev/test split from the frame, and write fixtures/h4/split.json."""
    from monograph.extract.sample import FrameEntry, Stratum, split, stratified_sample

    target = root / SPLIT_FILE
    if target.exists() and not force:
        raise typer.Exit(code=_fail(f"{target} exists; the split is frozen (use --force only before PREREG)"))
    frame = json.loads((root / FRAME_FILE).read_text())
    entries = [
        FrameEntry(e["set_id"], e["version"], Stratum(*e["stratum"].split("/")), e["h6_gap"], e["exclusion"])
        for e in frame["entries"]
    ]
    sample = stratified_sample(entries, total, frame["seed"])
    dev_entries, test_entries = split(sample.entries, dev, frame["seed"])

    def rows(items: list[FrameEntry]) -> list[dict[str, Any]]:
        return [
            {"set_id": e.set_id, "version": e.version, "stratum": e.stratum.key(), "h6_gap": e.h6_gap} for e in items
        ]

    data = {
        "seed": frame["seed"],
        "total": len(sample.entries),
        "quota": sample.quota,
        "shortfall": sample.shortfall,
        "dev": rows(dev_entries),
        "test": rows(test_entries),
    }
    target.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    typer.echo(
        f"sample {len(sample.entries)} (gap {sum(e.h6_gap for e in sample.entries)}), "
        f"dev {len(dev_entries)}, test {len(test_entries)}; shortfall {sample.shortfall or 'none'}"
    )


def _fail(message: str) -> int:
    typer.echo(f"FAIL: {message}", err=True)
    return 1


def _prereg_committed(root: Path) -> bool:
    import subprocess

    path = "docs/h4/PREREG.md"
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", path], cwd=root, capture_output=True, check=False)
    clean = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", path], cwd=root, check=False)
    return tracked.returncode == 0 and clean.returncode == 0


def _existing_records(root: Path, split: str) -> list[dict[str, Any]]:
    from monograph.harness.results import read_results

    return [
        r
        for r in read_results(root / "results" / "h4")
        if r["metric"] == "h4.record" and r["details"]["split"] == split
    ]


@h4_app.command("run")
def run(
    arm: Annotated[str, typer.Option(help="A0, A1, A2, A3 or all.")],
    model: Annotated[str, typer.Option(help="A pinned model ID or all.")],
    split: Annotated[str, typer.Option(help="dev or test.")],
    root: Root = Path(),
    approve_cost: Annotated[float, typer.Option(help="USD you approve for a test run above the threshold.")] = 0.0,
    deviation: Annotated[str | None, typer.Option(help="Reason for re-running test after PREREG.")] = None,
    limit: Annotated[int | None, typer.Option(help="Only the first N labels (dev iteration).")] = None,
) -> None:
    """Run arms over a split. Every round and record is appended to results/h4."""
    from decimal import Decimal

    import anthropic

    from monograph.extract.config import ARMS, COST_APPROVAL_THRESHOLD_USD, MODELS
    from monograph.extract.llm import AnthropicClient, CachedClient, ResponseCache
    from monograph.extract.runner import SplitRunner
    from monograph.extract.setup import build_context, extractor

    arms = list(ARMS) if arm == "all" else [arm]
    models = list(MODELS) if model == "all" else [model]
    if any(a not in ARMS for a in arms) or any(m not in MODELS for m in models) or split not in ("dev", "test"):
        raise typer.Exit(code=_fail(f"unknown arm, model or split; arms {ARMS}, models {MODELS}, splits dev/test"))
    if split == "test":
        if not _prereg_committed(root):
            raise typer.Exit(code=_fail("docs/h4/PREREG.md must be committed and unchanged before a test run"))
        done = {(r["details"]["model"], r["details"]["arm"]) for r in _existing_records(root, "test")}
        repeat = [(m, a) for m in models for a in arms if (m, a) in done]
        if repeat and deviation is None:
            raise typer.Exit(code=_fail(f"test already scored for {repeat}; pass --deviation with the reason"))
        estimate = _estimate(root, models, arms)
        typer.echo(f"cost estimate for this test run: ${estimate:.2f} (from dev runs, shared rounds counted per arm)")
        if estimate > COST_APPROVAL_THRESHOLD_USD and Decimal(str(approve_cost)) < estimate:
            raise typer.Exit(code=_fail(f"estimate exceeds ${COST_APPROVAL_THRESHOLD_USD}; rerun with --approve-cost"))
    ctx = build_context(root)
    docs = ctx.documents(split)[:limit] if limit else ctx.documents(split)
    writer = wiring.results_writer(root, "H4")
    client = CachedClient(
        AnthropicClient(anthropic.Anthropic(max_retries=4)), ResponseCache(root / "data" / "h4" / "responses")
    )
    if deviation is not None:
        writer.write("h4.deviation", deviation, {"split": split, "models": models, "arms": arms})
    for m in models:
        for a in arms:
            SplitRunner(extractor(ctx, client, m), writer, ctx.prompts, _settings()).run(docs, a, split, typer.echo)
    typer.echo(f"network calls: {client.network_calls}")


def _settings() -> Any:
    from monograph.extract.config import SETTINGS

    return SETTINGS


def _estimate(root: Path, models: list[str], arms: list[str]) -> Any:
    from collections import defaultdict
    from decimal import Decimal

    costs: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
    for r in _existing_records(root, "dev"):
        costs[(r["details"]["model"], r["details"]["arm"])].append(Decimal(r["details"]["cost_usd"]))
    n_test = len(json.loads((root / SPLIT_FILE).read_text())["test"])
    total = Decimal(0)
    for m in models:
        for a in arms:
            seen = costs.get((m, a))
            if not seen:
                raise typer.Exit(code=_fail(f"no dev runs for {m} {a}; run dev first so the cost can be estimated"))
            total += sum(seen, Decimal(0)) / len(seen) * n_test
    return total


@h4_app.command("score")
def score(
    split: Annotated[str, typer.Option(help="dev or test.")],
    root: Root = Path(),
) -> None:
    """Score the latest records of a split against SPL gold and append the scores to results/h4."""
    from monograph.extract.scoring_run import ScoreRun
    from monograph.extract.setup import build_context
    from monograph.harness.results import read_results

    ctx = build_context(root)
    frame = json.loads((root / FRAME_FILE).read_text())
    writer = wiring.results_writer(root, "H4")
    ScoreRun(ctx, writer, typer.echo).run(list(read_results(root / "results" / "h4")), split, frame["h6_gap_lookup"])
