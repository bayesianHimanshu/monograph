"""`monograph h2 ...` commands."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer

from monograph import wiring

h2_app = typer.Typer(no_args_is_help=True, help="H2: graph vs relational, free-form vs tools.")
Root = Annotated[Path, typer.Option("--root", help="Repository root.")]

FRAME_FILE = Path("fixtures/h2/frame.json")


def _clients(root: Path) -> tuple[Any, Any, Any, Any]:
    import os

    import httpx

    from monograph.extract.sources.dailymed import DailyMedClient, SplCache
    from monograph.ingest.openfda import OpenFdaClient, RawCache
    from monograph.ingest.rxnav import RxNavCache, RxNavClient

    raw = RawCache(wiring.raw_dir(root))
    http = httpx.Client(timeout=60.0)
    openfda = OpenFdaClient(http, raw, os.environ.get("OPENFDA_API_KEY"), 0.3, time.sleep, time.monotonic)
    dailymed = DailyMedClient(http, SplCache(wiring.raw_dir(root) / "spl"), 0.3, time.sleep, time.monotonic)
    rxnav = RxNavClient(http, RxNavCache(wiring.raw_dir(root) / "rxnav"), 0.1, time.sleep, time.monotonic)
    return raw, openfda, dailymed, rxnav


@h2_app.command("fetch")
def fetch(root: Root = Path()) -> None:
    """Fetch the H2 frame (cached under data/raw/; only missing responses hit the network)."""
    from monograph.graph.frame import FrameConfig, FrameFetcher

    raw, openfda, dailymed, rxnav = _clients(root)
    base = [
        *raw.all("ndc"),
        *(
            r
            for p in sorted((wiring.raw_dir(root) / "ndc" / "search").glob("*.json"))
            for r in json.loads(p.read_text())
        ),
    ]
    config = FrameConfig()
    report = FrameFetcher(openfda, raw, dailymed.spl, rxnav, config, typer.echo).fetch(base)
    path = root / FRAME_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    out = {"config": asdict(config), **asdict(report)}
    out["network_calls"] = {
        "openfda": openfda.network_calls,
        "dailymed": dailymed.network_calls,
        "rxnav": rxnav.network_calls,
    }
    path.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    typer.echo(f"frame written to {FRAME_FILE}")


AGE_DSN = "postgresql://monograph:monograph@localhost:55433/monograph"
ROLE_PASSWORDS = {"h2_sql": "h2_sql", "h2_graph": "h2_graph"}  # local, read-only roles on a local container


def admin_dsn() -> str:
    import os

    return os.environ.get("MONOGRAPH_AGE_DSN", AGE_DSN)


def role_dsn(role: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(admin_dsn())
    host = parts.hostname or "localhost"
    netloc = f"{role}:{ROLE_PASSWORDS[role]}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def build_dataset(root: Path) -> Any:
    from monograph.core.instances import Instantiator
    from monograph.graph.dataset import DatasetBuilder, read_raw
    from monograph.validate.pydantic_side import PydanticValidator

    _, _, dailymed, rxnav = _offline_clients(root)
    spec = wiring.build_spec()
    builder = DatasetBuilder(spec, Instantiator(spec), PydanticValidator(wiring.today()), rxnav, dailymed.spl)
    return builder.build(read_raw(wiring.raw_dir(root)))


class _Offline:
    """An HTTP client that refuses: dataset building reads caches only."""

    def get(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"cache miss during an offline build: {args[0] if args else ''}")


def _offline_clients(root: Path) -> tuple[Any, Any, Any, Any]:
    from monograph.extract.sources.dailymed import DailyMedClient, SplCache
    from monograph.ingest.openfda import OpenFdaClient, RawCache
    from monograph.ingest.rxnav import RxNavCache, RxNavClient

    http: Any = _Offline()
    raw = RawCache(wiring.raw_dir(root))
    openfda = OpenFdaClient(http, raw, None, 0.0, time.sleep, time.monotonic)
    dailymed = DailyMedClient(http, SplCache(wiring.raw_dir(root) / "spl"), 0.0, time.sleep, time.monotonic)
    rxnav = RxNavClient(http, RxNavCache(wiring.raw_dir(root) / "rxnav"), 0.0, time.sleep, time.monotonic)
    return raw, openfda, _CachedSpl(dailymed, root), rxnav


class _CachedSpl:
    def __init__(self, client: Any, root: Path) -> None:
        self._dir = wiring.raw_dir(root) / "spl"

    def spl(self, set_id: str) -> bytes | None:
        path = self._dir / f"{set_id}.xml"
        return path.read_bytes() if path.exists() else None


@h2_app.command("load")
def load(root: Root = Path()) -> None:
    """Build the dataset from the caches, load it into Postgres (schema h2) and project it into AGE."""
    import psycopg

    from monograph.core.vocab import Vocabulary
    from monograph.gen.age import Projection
    from monograph.gen.artifacts import AGE_FILE, DDL_FILE
    from monograph.graph.loader import GraphLoader, RelationalLoader, create_roles

    ds = build_dataset(root)
    summary = ds.summary()
    typer.echo(json.dumps(summary["instances"]))
    spec = wiring.build_spec()
    ddl = (wiring.generated_dir(root) / DDL_FILE).read_text(encoding="utf-8")
    projection = Projection.parse((wiring.generated_dir(root) / AGE_FILE).read_text(encoding="utf-8"))
    with psycopg.connect(admin_dsn(), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS age")
        rows = RelationalLoader(conn, spec, wiring.mapper(spec, Vocabulary()), ddl).load(ds.instances, typer.echo)
        vertices, edges = GraphLoader(conn, projection).load(typer.echo)
        create_roles(conn, projection.graph, ROLE_PASSWORDS)
    writer = wiring.results_writer(root, "H2")
    writer.write("h2.dataset", sum(summary["instances"].values()), summary)
    writer.write("h2.load", sum(rows.values()), {"rows": rows, "vertices": vertices, "edges": edges})
    typer.echo(f"loaded: {sum(rows.values())} rows, {sum(vertices.values())} vertices, {sum(edges.values())} edges")
