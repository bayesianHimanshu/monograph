# MONOGRAPH

Small builds that test our point of view on knowledge graphs, ontologies and GraphRAG, all over one
drug product model built from public FDA data. Each experiment tests one hypothesis and its results
update the PoV.

## H6: one schema keeps OWL, SHACL and Pydantic in sync

The model lives once, as Pydantic v2 classes in `src/monograph/core/models.py`. Everything in
`generated/` (OWL, SHACL, JSON Schema, Postgres DDL) is derived from it. CI breaks it on purpose to
check that every break is caught, and the leaks (what cannot be written once and projected
everywhere) are measured.

* Plan and design: `docs/h6/PLAN.md`, `docs/adr/0001-pydantic-first.md`
* Mapping rules: `docs/h6/MAPPING.md`
* Results (generated from `results/h6/*.jsonl`): `docs/h6/RESULTS.md`

## Commands

```
uv sync
make gen        # regenerate generated/ from the models
make check      # drift, breaking-change, validator lint, agreement
make test       # unit and property-based tests (offline)
make it         # Postgres 18 integration test (docker compose)
make mutate     # mutation suite M0 to M10, appends to results/h6
make report     # measurements, then docs/h6/RESULTS.md
uv run monograph ingest fetch   # refresh the openFDA sample in data/raw (network; OPENFDA_API_KEY optional)
uv run monograph release        # snapshot generated/ as generated/releases/<SCHEMA_VERSION>/
```
