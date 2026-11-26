"""H4 scoring: per-label scores, aggregates, repair attribution, error patterns, bootstrap, verdict.

Definitions (pre-registered in docs/h4/PREREG.md; this module is the executable form):

* Ingredient tuple: (UNII, strength per one basis unit, numerator dimension, denominator unit), after
  converting mass to mg and volume to mL. Extracted names get their UNII from the deterministic resolver;
  an unresolved name yields no UNII and cannot match. Tuples are pooled per label as a multiset.
* Per-label F1 over those tuples (1.0 when gold and extraction are both empty). Primary metric: the mean
  of per-label F1. Secondary: pooled F1 over all tuples.
* Products are aligned gold to extracted, greedily by shared tuples then name similarity. Dosage form
  accuracy and route accuracy are over gold products (an unaligned gold product counts as wrong).
* Unresolvable reference: an extracted substance name that resolves to no single Substance, or an
  extracted NDC that matches no PackagedProduct when the registry lists the label's products.
* Hallucinated value: an extracted substance name, strength value, basis value or NDC that appears
  neither in the input text nor in gold. Enum fields (dosage form, route, units) are classifications,
  not copied text, and are not checked. Basis 1 with unit "1" is the implicit per-unit basis and is not
  checked.
* Exact match: tuples equal, every gold product aligned with the right dosage form and routes, the same
  number of products, and the extracted NDC set equal to the gold NDCs that the narrative prints.
* Usable configuration (headline test labels): mean per-label F1 >= 0.95, dosage form accuracy >= 0.97,
  route accuracy >= 0.97, unresolvable rate <= 0.01, hallucination rate <= 0.005.
"""

from __future__ import annotations

import difflib
import itertools
import random
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from monograph.extract.resolve import SubstanceResolver, normalize_name
from monograph.extract.sources.dailymed import GoldProduct, SplDocument, narrative_text

MASS_TO_MG: dict[str, Decimal] = {
    "kg": Decimal("1000000"),
    "g": Decimal("1000"),
    "mg": Decimal("1"),
    "ug": Decimal("0.001"),
    "ng": Decimal("0.000001"),
}
VOLUME_TO_ML: dict[str, Decimal] = {
    "L": Decimal("1000"),
    "dL": Decimal("100"),
    "mL": Decimal("1"),
    "uL": Decimal("0.001"),
}

THRESHOLD = {
    "mean_f1": 0.95,
    "dosage_form_accuracy": 0.97,
    "route_accuracy": 0.97,
    "unresolvable_rate": 0.01,
    "hallucination_rate": 0.005,
}

Tuple = tuple[str, str, str, str]


def _dec(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _normalize(value: Decimal, unit: str) -> tuple[Decimal, str]:
    if unit in MASS_TO_MG:
        return value * MASS_TO_MG[unit], "mg"
    if unit in VOLUME_TO_ML:
        return value * VOLUME_TO_ML[unit], "mL"
    return value, unit


def strength_key(num: Any, num_unit: Any, den: Any, den_unit: Any) -> tuple[str, str, str] | None:
    n, d = _dec(num), _dec(den)
    if n is None or d is None or d == 0 or not isinstance(num_unit, str) or not isinstance(den_unit, str):
        return None
    n, nu = _normalize(n, num_unit)
    d, du = _normalize(d, den_unit)
    ratio = n / d
    return (f"{float(ratio):.6g}", nu, du)  # six significant digits, no trailing zeros


def gold_tuples(product: GoldProduct) -> Counter[Tuple]:
    out: Counter[Tuple] = Counter()
    for i in product.active:
        key = strength_key(i.numerator_value, i.numerator_unit, i.denominator_value, i.denominator_unit)
        if key is not None:
            out[(i.unii, *key)] += 1
    return out


def _products(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
        return []
    return [p for p in payload["products"] if isinstance(p, dict)]


def _list_of(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _items(product: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = product.get(key)
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def extracted_tuples(product: Mapping[str, Any], resolver: SubstanceResolver) -> Counter[Tuple]:
    """`resolver` should be bound to the label (`SubstanceResolver.for_label`)."""
    out: Counter[Tuple] = Counter()
    for i in _items(product, "ingredients"):
        name = i.get("substance_name")
        resolution = resolver.resolve(name) if isinstance(name, str) else None
        unii = resolution.iri.rsplit("/", 1)[-1] if resolution and resolution.iri else ""
        key = strength_key(i.get("strength_value"), i.get("strength_unit"), i.get("basis_value"), i.get("basis_unit"))
        if key is not None and unii:
            out[(unii, *key)] += 1
        else:
            out[(unii or f"?{name}", *(key or ("?", "?", "?")))] += 1
    return out


def align(
    gold: Sequence[GoldProduct], extracted: Sequence[Mapping[str, Any]], resolver: SubstanceResolver
) -> dict[int, int]:
    """gold index -> extracted index, greedy by shared tuples then name similarity."""
    pairs = []
    for gi, g in enumerate(gold):
        gt = gold_tuples(g)
        for ei, e in enumerate(extracted):
            shared = sum((gt & extracted_tuples(e, resolver)).values())
            name = e.get("name") if isinstance(e.get("name"), str) else ""
            similarity = difflib.SequenceMatcher(None, g.name.upper(), str(name).upper()).ratio()
            pairs.append((shared, similarity, -gi, -ei, gi, ei))
    pairs.sort(reverse=True)
    out: dict[int, int] = {}
    used: set[int] = set()
    for *_, gi, ei in pairs:
        if gi not in out and ei not in used:
            out[gi] = ei
            used.add(ei)
    return out


_NUMBER = re.compile(r"(?<![\d.])\d+(?:[.,]\d+)?")


def _numbers(text: str) -> set[Decimal]:
    out = set()
    for token in _NUMBER.findall(text):
        d = _dec(token.replace(",", "") if token.count(",") == 1 and len(token.split(",")[1]) == 3 else token)
        if d is not None:
            out.add(d.normalize())
    return out


@dataclass
class LabelScore:
    set_id: str
    stratum: str
    h6_gap: bool
    headline: bool
    status: str
    ing_tp: int = 0
    ing_fp: int = 0
    ing_fn: int = 0
    form_correct: int = 0
    form_total: int = 0
    route_correct: int = 0
    route_total: int = 0
    products_extracted: int = 0
    ndc_tp: int = 0
    ndc_fp: int = 0
    ndc_fn: int = 0
    ndc_fn_mentioned: int = 0
    refs_total: int = 0
    refs_unresolved: int = 0
    values_total: int = 0
    values_hallucinated: int = 0
    exact_match: bool = False
    violation_free: bool = False
    repair_rounds: int = 0
    cost_usd: str = "0"
    latency_s: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)

    @property
    def f1(self) -> float:
        if self.ing_tp + self.ing_fp + self.ing_fn == 0:
            return 1.0
        return 2 * self.ing_tp / (2 * self.ing_tp + self.ing_fp + self.ing_fn)


class LabelScorer:
    def __init__(self, resolver: SubstanceResolver, label_known: Mapping[str, bool], ndc_known: set[str]) -> None:
        self._resolver = resolver
        self._label_known = label_known
        self._ndc_known = ndc_known

    def score(self, doc: SplDocument, row: Mapping[str, Any], record: Mapping[str, Any]) -> LabelScore:
        return _LabelScoring(self._resolver.for_label(doc.set_id), self._label_known, self._ndc_known).score(
            doc, row, record
        )


class _LabelScoring(LabelScorer):
    def score(self, doc: SplDocument, row: Mapping[str, Any], record: Mapping[str, Any]) -> LabelScore:
        details = record["details"]
        payload = details["final_payload"]
        gold = [p for p in doc.products if p.active]
        extracted = _products(payload)
        text = narrative_text(doc)
        s = LabelScore(
            doc.set_id,
            row["stratum"],
            row["h6_gap"],
            not row["stratum"].endswith("/allergenic"),
            record["value"],
            repair_rounds=details["repair_rounds"],
            cost_usd=details["cost_usd"],
            latency_s=details["latency_s"],
            usage=details["usage"],
        )
        s.violation_free = details["pydantic_valid"] and not details["context_issues"]
        self._ingredients(s, gold, extracted)
        self._products(s, gold, extracted)
        self._packages(s, doc, extracted, text)
        self._references_and_values(s, doc, extracted, text)
        s.exact_match = (
            s.status == "ok"
            and s.ing_fp == 0
            and s.ing_fn == 0
            and s.form_correct == s.form_total
            and s.route_correct == s.route_total
            and s.products_extracted == len(gold)
            and s.ndc_fp == 0
            and s.ndc_fn_mentioned == 0
        )
        if s.status in ("truncated", "refused", "invalid_json"):
            s.errors.insert(0, {"kind": f"response {s.status}", "detail": ""})
        return s

    def _ingredients(self, s: LabelScore, gold: Sequence[GoldProduct], extracted: Sequence[Mapping[str, Any]]) -> None:
        g: Counter[Tuple] = sum((gold_tuples(p) for p in gold), Counter())
        e: Counter[Tuple] = sum((extracted_tuples(p, self._resolver) for p in extracted), Counter())
        s.ing_tp = sum((g & e).values())
        s.ing_fp = sum((e - g).values())
        s.ing_fn = sum((g - e).values())
        gold_uniis = {t[0] for t in g}
        for t in (g - e).elements():
            same = [x for x in (e - g) if x[0] == t[0]]
            if same:
                s.errors.append({"kind": "ingredient strength mismatch", "detail": f"{t} vs {same[0]}"})
            else:
                s.errors.append({"kind": "ingredient missing or unresolved", "detail": str(t)})
        for t in (e - g).elements():
            if t[0].startswith("?"):
                s.errors.append({"kind": "substance name unresolved", "detail": t[0][1:]})
            elif t[0] not in gold_uniis:
                s.errors.append({"kind": "substance resolves to a UNII not in gold", "detail": str(t)})

    def _products(self, s: LabelScore, gold: Sequence[GoldProduct], extracted: Sequence[Mapping[str, Any]]) -> None:
        s.products_extracted = len(extracted)
        s.form_total = s.route_total = len(gold)
        mapping = align(gold, extracted, self._resolver)
        for gi, g in enumerate(gold):
            if gi not in mapping:
                s.errors.append({"kind": "product not extracted", "detail": g.name})
                continue
            e = extracted[mapping[gi]]
            if e.get("dosage_form") == g.dosage_form:
                s.form_correct += 1
            else:
                s.errors.append(
                    {"kind": "dosage form wrong", "detail": f"{e.get('dosage_form')} instead of {g.dosage_form}"}
                )
            routes = _list_of(e.get("routes"))
            if set(map(str, routes)) == set(g.routes):
                s.route_correct += 1
            else:
                s.errors.append(
                    {"kind": "route wrong", "detail": f"{sorted(map(str, routes))} instead of {list(g.routes)}"}
                )
        if len(extracted) > len(gold):
            s.errors.append({"kind": "extra product", "detail": f"{len(extracted)} extracted, {len(gold)} in gold"})

    def _packages(self, s: LabelScore, doc: SplDocument, extracted: Sequence[Mapping[str, Any]], text: str) -> None:
        gold = {n for p in doc.products for n in p.packages}
        digits = re.sub(r"\D", "", text)
        mentioned = {n for n in gold if n in text or n.replace("-", "") in digits}
        got = {str(k.get("ndc")) for p in extracted for k in _items(p, "packages")}
        s.ndc_tp, s.ndc_fp, s.ndc_fn = len(got & gold), len(got - gold), len(gold - got)
        s.ndc_fn_mentioned = len(mentioned - got)
        for n in sorted(mentioned - got):
            s.errors.append({"kind": "printed NDC not extracted", "detail": n})
        for n in sorted(got - gold):
            s.errors.append({"kind": "NDC not in gold", "detail": n})

    def _references_and_values(
        self, s: LabelScore, doc: SplDocument, extracted: Sequence[Mapping[str, Any]], text: str
    ) -> None:
        normalized_text = normalize_name(text)
        text_numbers = _numbers(text)
        gold_names = {normalize_name(i.name) for p in doc.products for i in p.active}
        gold_numbers = {
            d.normalize()
            for p in doc.products
            for i in p.active
            for d in (i.numerator_value, i.denominator_value)
            if d is not None
        }
        gold_ndcs = {n for p in doc.products for n in p.packages}
        digits = re.sub(r"\D", "", text)
        listed = self._label_known.get(doc.set_id, False)
        for p in extracted:
            for i in _items(p, "ingredients"):
                name = i.get("substance_name")
                if isinstance(name, str):
                    s.refs_total += 1
                    if self._resolver.resolve(name).iri is None:
                        s.refs_unresolved += 1
                    self._value(
                        s,
                        normalize_name(name) in normalized_text or normalize_name(name) in gold_names,
                        f"substance name {name!r}",
                    )
                for key in ("strength_value", "basis_value"):
                    value = _dec(i.get(key))
                    if value is None:
                        continue
                    if key == "basis_value" and value == 1 and i.get("basis_unit") == "1":
                        continue
                    self._value(
                        s, value.normalize() in text_numbers or value.normalize() in gold_numbers, f"{key} {value}"
                    )
            for k in _items(p, "packages"):
                ndc = str(k.get("ndc"))
                if listed:
                    s.refs_total += 1
                    if ndc not in self._ndc_known:
                        s.refs_unresolved += 1
                self._value(s, ndc in gold_ndcs or (bool(ndc) and ndc.replace("-", "") in digits), f"NDC {ndc}")

    @staticmethod
    def _value(s: LabelScore, grounded: bool, what: str) -> None:
        s.values_total += 1
        if not grounded:
            s.values_hallucinated += 1
            s.errors.append({"kind": "hallucinated value", "detail": what})


# ----------------------------------------------------------------------------------------- aggregate


def aggregate(scores: Sequence[LabelScore]) -> dict[str, Any]:
    if not scores:
        return {"labels": 0}
    tp, fp, fn = (sum(getattr(s, k) for s in scores) for k in ("ing_tp", "ing_fp", "ing_fn"))
    costs = [Decimal(s.cost_usd) for s in scores]
    latencies = sorted(s.latency_s for s in scores)
    exact = sum(s.exact_match for s in scores)
    total_cost = sum(costs, Decimal(0))

    def rate(num: int, den: int) -> float | None:
        return num / den if den else None

    usage = {
        k: sum(s.usage.get(k, 0) for s in scores)
        for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    }
    return {
        "labels": len(scores),
        "mean_f1": statistics.fmean(s.f1 for s in scores),
        "pooled_f1": rate(2 * tp, 2 * tp + fp + fn) if tp + fp + fn else 1.0,
        "dosage_form_accuracy": rate(sum(s.form_correct for s in scores), sum(s.form_total for s in scores)),
        "route_accuracy": rate(sum(s.route_correct for s in scores), sum(s.route_total for s in scores)),
        "unresolvable_rate": rate(sum(s.refs_unresolved for s in scores), sum(s.refs_total for s in scores)) or 0.0,
        "hallucination_rate": rate(sum(s.values_hallucinated for s in scores), sum(s.values_total for s in scores))
        or 0.0,
        "ndc_precision": rate(sum(s.ndc_tp for s in scores), sum(s.ndc_tp + s.ndc_fp for s in scores)),
        "ndc_recall_all_gold": rate(sum(s.ndc_tp for s in scores), sum(s.ndc_tp + s.ndc_fn for s in scores)),
        "exact_match_rate": exact / len(scores),
        "violation_free_rate": sum(s.violation_free for s in scores) / len(scores),
        "status": dict(Counter(s.status for s in scores)),
        "truncated": sum(s.status == "truncated" for s in scores),
        "mean_repair_rounds": statistics.fmean(s.repair_rounds for s in scores),
        "repair_rounds": dict(Counter(s.repair_rounds for s in scores)),
        "usage": usage,
        "cost_usd": str(total_cost),
        "cost_per_record_usd": str(total_cost / len(scores)),
        "cost_per_usable_record_usd": str(total_cost / exact) if exact else None,
        "latency_p50_s": latencies[len(latencies) // 2],
        "latency_p95_s": latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))],
    }


def usable(agg: Mapping[str, Any]) -> tuple[bool, dict[str, bool]]:
    checks = {
        "mean_f1": (agg.get("mean_f1") or 0) >= THRESHOLD["mean_f1"],
        "dosage_form_accuracy": (agg.get("dosage_form_accuracy") or 0) >= THRESHOLD["dosage_form_accuracy"],
        "route_accuracy": (agg.get("route_accuracy") or 0) >= THRESHOLD["route_accuracy"],
        "unresolvable_rate": agg.get("unresolvable_rate", 1) <= THRESHOLD["unresolvable_rate"],
        "hallucination_rate": agg.get("hallucination_rate", 1) <= THRESHOLD["hallucination_rate"],
    }
    return all(checks.values()), checks


def verdict(usable_by: Mapping[tuple[str, str], bool], models: Iterable[str]) -> str:
    """The brief's rule, with the pre-registered precedence for when both conditions are true.

    fails:        A1 or A2 is usable for every model (record-local repair is enough everywhere);
    holds:        for at least one model, A3 is usable and A1 is not;
    partly holds: otherwise.

    "fails" is evaluated first: if both hold, a model whose A1 is not usable but whose A2 is reached the
    threshold through deterministic repair, without graph context.
    """
    models = list(models)
    if all(usable_by.get((m, "A1"), False) or usable_by.get((m, "A2"), False) for m in models):
        return "fails"
    if any(usable_by.get((m, "A3"), False) and not usable_by.get((m, "A1"), False) for m in models):
        return "holds"
    return "partly holds"


def bootstrap_mean(values: Sequence[float], seed: int, n: int = 10_000) -> tuple[float, float]:
    rng = random.Random(seed)
    k = len(values)
    means = sorted(statistics.fmean(values[rng.randrange(k)] for _ in range(k)) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def bootstrap_difference(
    a: Mapping[str, float], b: Mapping[str, float], seed: int, n: int = 10_000
) -> dict[str, float]:
    """Paired bootstrap over labels of mean(a) - mean(b), on labels present in both."""
    keys = sorted(set(a) & set(b))
    diffs = [a[k] - b[k] for k in keys]
    if not diffs:
        return {"labels": 0}
    low, high = bootstrap_mean(diffs, seed, n)
    return {"labels": len(keys), "difference": statistics.fmean(diffs), "ci_low": low, "ci_high": high}


# --------------------------------------------------------------------------------------- attribution


def field_states(doc: SplDocument, payload: Any, resolver: SubstanceResolver) -> dict[str, bool]:
    """Correctness of every gold field in one payload: each gold tuple, each gold product's form and
    routes, each printed gold NDC."""
    resolver = resolver.for_label(doc.set_id)
    gold = [p for p in doc.products if p.active]
    extracted = _products(payload)
    out: dict[str, bool] = {}
    e: Counter[Tuple] = sum((extracted_tuples(p, resolver) for p in extracted), Counter())
    for t, count in sum((gold_tuples(p) for p in gold), Counter()).items():
        for n in range(count):
            out[f"ingredient {t[0]} {t[1]}{t[2]}/{t[3]} #{n}"] = e[t] > n
    mapping = align(gold, extracted, resolver)
    for gi, g in enumerate(gold):
        ext = extracted[mapping[gi]] if gi in mapping else {}
        out[f"form {gi} {g.dosage_form}"] = ext.get("dosage_form") == g.dosage_form
        routes = _list_of(ext.get("routes"))
        out[f"routes {gi} {','.join(g.routes)}"] = set(map(str, routes)) == set(g.routes)
    text = narrative_text(doc)
    digits = re.sub(r"\D", "", text)
    got = {str(k.get("ndc")) for p in extracted for k in _items(p, "packages")}
    for ndc in sorted({n for p in doc.products for n in p.packages}):
        if ndc in text or ndc.replace("-", "") in digits:
            out[f"ndc {ndc}"] = ndc in got
    return out


def attribute(doc: SplDocument, rounds: Sequence[Mapping[str, Any]], resolver: SubstanceResolver) -> dict[str, Any]:
    """Which transition fixed each error and which broke a correct field. Transitions: a model round
    (labelled by the feedback that preceded it: pydantic or graph) and a deterministic fix step."""
    states: list[tuple[str, int, str | None, dict[str, bool]]] = []
    for r in rounds:
        d = r["details"]
        label = d["phase"] if d["phase"] != "first" else "first"
        states.append((label, r["value"], d.get("feedback"), field_states(doc, d["raw_payload"], resolver)))
        if d["fixes"]:
            states.append(("deterministic", r["value"], None, field_states(doc, d["payload"], resolver)))
    names_by_unii: dict[str, set[str]] = defaultdict(set)
    for p in doc.products:
        for i in p.active:
            names_by_unii[i.unii].add(normalize_name(i.name))
    fixed: Counter[str] = Counter()
    regressions: Counter[str] = Counter()
    fixed_by_round: Counter[str] = Counter()
    exposed = 0
    for (_, _, _, before), (label, index, feedback, after) in itertools.pairwise(states):
        for name, ok in after.items():
            was = before.get(name, False)
            if ok and not was:
                fixed[label] += 1
                fixed_by_round[f"{label} round {index}"] += 1
                if label == "graph" and feedback and _gold_value_in(name, feedback, names_by_unii):
                    exposed += 1
            elif was and not ok:
                regressions[label] += 1
    first_errors = sum(not ok for ok in states[0][3].values()) if states else 0
    final_errors = sum(not ok for ok in states[-1][3].values()) if states else 0
    return {
        "first_round_errors": first_errors,
        "final_errors": final_errors,
        "fixed_by": dict(fixed),
        "fixed_by_round": dict(fixed_by_round),
        "regressions_by": dict(regressions),
        "graph_fixes_with_gold_in_feedback": exposed,
    }


def _gold_value_in(field_name: str, feedback: str, names_by_unii: Mapping[str, set[str]]) -> bool:
    """Did the feedback that preceded a fix contain the gold value itself (registry lookup, not extraction)?"""
    kind, _, rest = field_name.partition(" ")
    if kind == "ndc":
        return rest in feedback
    if kind in ("form", "routes"):
        value = rest.split(" ", 1)[1] if " " in rest else rest
        return bool(value) and value in feedback
    unii = rest.split(" ", 1)[0]
    text = normalize_name(feedback)
    return any(name and name in text for name in names_by_unii.get(unii, ()))


def error_patterns(scores: Sequence[LabelScore], top: int = 10) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    example: dict[str, dict[str, str]] = {}
    for s in scores:
        for e in s.errors:
            counts[e["kind"]] += 1
            example.setdefault(e["kind"], {"set_id": s.set_id, "detail": e["detail"]})
    return [{"pattern": k, "count": n, "example": example[k]} for k, n in counts.most_common(top)]


def as_dict(score: LabelScore) -> dict[str, Any]:
    data = asdict(score)
    data["f1"] = score.f1
    data["errors"] = score.errors[:10]
    return data


def group_scores(scores: Iterable[LabelScore]) -> dict[str, list[LabelScore]]:
    groups: dict[str, list[LabelScore]] = defaultdict(list)
    for s in scores:
        groups["all"].append(s)
        if s.headline:
            groups["headline"].append(s)
        if s.h6_gap:
            groups["h6_gap"].append(s)
        if not s.headline:
            groups["stratum:allergenic"].append(s)
        else:
            groups[f"stratum:{s.stratum}"].append(s)
    return dict(groups)


def gap_products_correct(
    doc: SplDocument, payload: Any, product_ndcs: Iterable[str], resolver: SubstanceResolver
) -> dict[str, bool]:
    """For each H6 gap product NDC in this label: are all of that product's gold ingredient tuples
    extracted, in the extracted product aligned to it, and nothing else?"""
    resolver = resolver.for_label(doc.set_id)
    gold = [p for p in doc.products if p.active]
    extracted = _products(payload)
    mapping = align(gold, extracted, resolver)
    out = {}
    for ndc in product_ndcs:
        index = next((i for i, g in enumerate(gold) if g.product_ndc == ndc), None)
        if index is None:
            continue
        if index not in mapping:
            out[ndc] = False
            continue
        out[ndc] = gold_tuples(gold[index]) == extracted_tuples(extracted[mapping[index]], resolver)
    return out
