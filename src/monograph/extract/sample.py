"""The H4 sampling frame, stratification and dev/test split. Deterministic given the seed and the cache."""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from monograph.extract.sources.dailymed import GoldProduct, SplDocument

DOCTYPES = {"rx": "34391-3", "otc": "34390-5"}  # LOINC document types on DailyMed

SOLID_ORAL_PREFIXES = (
    "TABLET",
    "CAPSULE",
    "LOZENGE",
    "TROCHE",
    "PILL",
    "WAFER",
    "BAR",
    "GUM",
    "GRANULE",
    "PELLET",
    "FILM",
)
LIQUID_PREFIXES = (
    "SOLUTION",
    "SUSPENSION",
    "SYRUP",
    "ELIXIR",
    "LIQUID",
    "EMULSION",
    "CONCENTRATE",
    "TINCTURE",
    "FOR SOLUTION",
    "FOR SUSPENSION",
    "SOLUTION/ DROPS",
    "SUSPENSION/ DROPS",
)
PARENTERAL_ROUTES = {"INTRAVENOUS", "INTRAMUSCULAR", "SUBCUTANEOUS", "INTRADERMAL", "INTRATHECAL", "EPIDURAL"}


@dataclass(frozen=True, slots=True)
class Stratum:
    ingredients: str  # single | multi
    form: str  # solid_oral | liquid | injectable | other
    regulatory: str  # rx | otc

    def key(self) -> str:
        return f"{self.ingredients}/{self.form}/{self.regulatory}"


@dataclass(frozen=True, slots=True)
class FrameEntry:
    set_id: str
    version: int | None
    stratum: Stratum
    h6_gap: bool
    exclusion: str | None = None  # why the label cannot be sampled, if it cannot


def primary_product(doc: SplDocument, allow_kits: bool = False) -> GoldProduct | None:
    return next((p for p in doc.products if p.active and (allow_kits or not p.is_kit)), None)


def form_class(product: GoldProduct) -> str:
    form = (product.dosage_form or "").upper()
    if form.startswith(("INJECTION", "INJECTABLE")) or (set(product.routes) & PARENTERAL_ROUTES):
        return "injectable"
    if "ORAL" in product.routes and form.startswith(SOLID_ORAL_PREFIXES):
        return "solid_oral"
    if "ORAL" in product.routes and form.startswith(LIQUID_PREFIXES):
        return "liquid"
    return "other"


def regulatory_class(doc: SplDocument) -> str | None:
    kind = doc.document_type.upper()
    if "PRESCRIPTION" in kind:
        return "rx"
    if "OTC" in kind:
        return "otc"
    if "ALLERGENIC" in kind:
        return "allergenic"  # admitted only for H6 gap labels
    return None


def classify(doc: SplDocument, h6_gap: bool) -> FrameEntry:
    def excluded(reason: str) -> FrameEntry:
        return FrameEntry(doc.set_id, doc.version, Stratum("?", "?", "?"), h6_gap, reason)

    if doc.legacy_schema:
        return excluded("pre-2009 SPL schema")
    # H6 gap labels are included whenever extraction is possible at all (the brief asks for every one
    # with an available SPL); kits and allergenic labels are admitted for them only, outside the cells.
    regulatory = regulatory_class(doc)
    if regulatory is None or (regulatory == "allergenic" and not h6_gap):
        return excluded(f"document type {doc.document_type!r} is neither Rx nor OTC")
    product = primary_product(doc, allow_kits=h6_gap)
    if product is None:
        return excluded(
            "no product with an active ingredient" if h6_gap else "no non-kit product with an active ingredient"
        )
    if not doc.sections:
        return excluded("no narrative section with text")
    form = "kit" if product.is_kit else form_class(product)
    stratum = Stratum("single" if len(product.active) == 1 else "multi", form, regulatory)
    if stratum.form == "other" and not h6_gap:
        return FrameEntry(doc.set_id, doc.version, stratum, h6_gap, "form outside solid oral, liquid, injectable")
    return FrameEntry(doc.set_id, doc.version, stratum, h6_gap)


@dataclass(frozen=True, slots=True)
class Sample:
    entries: tuple[FrameEntry, ...]
    quota: dict[str, int]
    shortfall: dict[str, int]


def stratified_sample(frame: Sequence[FrameEntry], total: int, seed: int) -> Sample:
    """Every eligible H6 gap label first, then equal quotas over the 12 cells, redistributing what a
    cell cannot fill to cells that still have candidates (round robin, in sorted cell order)."""
    eligible = sorted((e for e in frame if e.exclusion is None), key=lambda e: e.set_id)
    gap = [e for e in eligible if e.h6_gap]
    chosen: dict[str, FrameEntry] = {e.set_id: e for e in gap[:total]}
    pools: dict[str, list[FrameEntry]] = defaultdict(list)
    for e in eligible:
        if (
            e.set_id not in chosen
            and e.stratum.form in ("solid_oral", "liquid", "injectable")
            and e.stratum.regulatory in ("rx", "otc")
        ):
            pools[e.stratum.key()].append(e)
    rng = random.Random(seed)
    for key in sorted(pools):
        rng.shuffle(pools[key])
    cells = [
        f"{i}/{f}/{r}"
        for i in ("single", "multi")
        for f in ("solid_oral", "liquid", "injectable")
        for r in ("rx", "otc")
    ]
    remaining = total - len(chosen)
    target = {c: remaining // len(cells) + (1 if n < remaining % len(cells) else 0) for n, c in enumerate(cells)}
    taken = {c: 0 for c in cells}
    for c in cells:
        for e in pools.get(c, [])[: target[c]]:
            chosen[e.set_id] = e
            taken[c] += 1
    shortfall = {c: target[c] - taken[c] for c in cells if taken[c] < target[c]}
    missing = sum(shortfall.values())
    while missing > 0:
        progressed = False
        for c in cells:
            if missing == 0:
                break
            pool = pools.get(c, [])
            if taken[c] < len(pool):
                chosen[pool[taken[c]].set_id] = pool[taken[c]]
                taken[c] += 1
                missing -= 1
                progressed = True
        if not progressed:
            break
    entries = tuple(sorted(chosen.values(), key=lambda e: e.set_id))
    return Sample(entries, target, shortfall)


def split(entries: Iterable[FrameEntry], dev_size: int, seed: int) -> tuple[list[FrameEntry], list[FrameEntry]]:
    """Dev/test split stratified by cell (gap labels as their own group), largest-remainder allocation."""
    groups: dict[str, list[FrameEntry]] = defaultdict(list)
    for e in sorted(entries, key=lambda e: e.set_id):
        groups[("gap:" if e.h6_gap else "") + e.stratum.key()].append(e)
    total = sum(len(g) for g in groups.values())
    rng = random.Random(seed)
    for key in sorted(groups):
        rng.shuffle(groups[key])
    exact = {k: dev_size * len(g) / total for k, g in groups.items()}
    alloc = {k: int(v) for k, v in exact.items()}
    for k in sorted(exact, key=lambda k: (-(exact[k] - alloc[k]), k))[: dev_size - sum(alloc.values())]:
        alloc[k] += 1
    dev: list[FrameEntry] = []
    test: list[FrameEntry] = []
    for k in sorted(groups):
        dev += groups[k][: alloc[k]]
        test += groups[k][alloc[k] :]
    return sorted(dev, key=lambda e: e.set_id), sorted(test, key=lambda e: e.set_id)
