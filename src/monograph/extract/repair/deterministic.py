"""Deterministic repair for mechanical issues only, with no LLM call (arm A2).

Exactly three kinds of fix, each recorded:

* unit spelling to UCUM (for example "MG" or "mgs" to "mg", "mcg" or "µg" to "ug", "IU" to "[iU]");
* whitespace (leading, trailing and repeated spaces in strings);
* NDC hyphen format: an NDC written with spaces, without hyphens or with an "NDC" prefix becomes the
  hyphenated form when exactly one 4-4-2, 5-3-2 or 5-4-1 reading fits the digits as written.

Nothing else is changed: no value is inferred, no field is added or removed.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from monograph.core.models import UcumUnit

_UCUM = {u.value for u in UcumUnit}
# Spellings seen in labels for UCUM units, matched case-insensitively. Case-sensitive UCUM codes (mL, [iU])
# are the values.
UNIT_SPELLINGS: dict[str, str] = {
    "mg": "mg",
    "mgs": "mg",
    "milligram": "mg",
    "milligrams": "mg",
    "g": "g",
    "gm": "g",
    "gram": "g",
    "grams": "g",
    "kg": "kg",
    "kilogram": "kg",
    "mcg": "ug",
    "ug": "ug",
    "microgram": "ug",
    "micrograms": "ug",
    "ng": "ng",
    "nanogram": "ng",
    "ml": "mL",
    "milliliter": "mL",
    "millilitre": "mL",
    "milliliters": "mL",
    "cc": "mL",
    "l": "L",
    "liter": "L",
    "litre": "L",
    "dl": "dL",
    "ul": "uL",
    "meq": "meq",
    "mmol": "mmol",
    "mol": "mol",
    "umol": "umol",
    "nmol": "nmol",
    "iu": "[iU]",
    "[iu]": "[iU]",
    "international units": "[iU]",
    "intl units": "[iU]",
    "usp units": "[USP'U]",
    "usp unit": "[USP'U]",
    "unit": "1",
    "units of presentation": "1",
    "each": "1",
}
for _micro in (chr(0xB5), chr(0x3BC)):  # micro sign and Greek small mu, both used for "micro"
    UNIT_SPELLINGS[_micro + "g"] = "ug"
    UNIT_SPELLINGS[_micro + "l"] = "uL"
_NDC_SHAPES = ((4, 4, 2), (5, 3, 2), (5, 4, 1))


@dataclass(frozen=True, slots=True)
class Fix:
    path: str
    kind: str  # unit | whitespace | ndc
    before: str
    after: str


def repair(payload: Any) -> tuple[Any, list[Fix]]:
    """Return a repaired copy of the extraction payload and the fixes applied."""
    fixes: list[Fix] = []
    out = copy.deepcopy(payload)
    _walk(out, "", fixes)
    return out, fixes


def _walk(node: Any, path: str, fixes: list[Fix]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if isinstance(value, str):
                node[key] = _fix_string(key, value, here, fixes)
            else:
                _walk(value, here, fixes)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            here = f"{path}[{i}]"
            if isinstance(value, str):
                node[i] = _fix_string("", value, here, fixes)
            else:
                _walk(value, here, fixes)


def _fix_string(key: str, value: str, path: str, fixes: list[Fix]) -> str:
    fixed = re.sub(r"\s+", " ", value).strip()
    if fixed != value:
        fixes.append(Fix(path, "whitespace", value, fixed))
    if key in ("strength_unit", "basis_unit") and fixed not in _UCUM:
        ucum = UNIT_SPELLINGS.get(fixed.lower())
        if ucum is not None:
            fixes.append(Fix(path, "unit", fixed, ucum))
            return ucum
    if key == "ndc":
        ndc = normalize_ndc(fixed)
        if ndc is not None and ndc != fixed:
            fixes.append(Fix(path, "ndc", fixed, ndc))
            return ndc
    return fixed


def normalize_ndc(text: str) -> str | None:
    """The hyphenated NDC for `text`, or None when the digits do not fit exactly one reading."""
    body = re.sub(r"^\s*NDC[\s:#]*", "", text, flags=re.IGNORECASE).strip()
    groups = re.findall(r"\d+", body)
    if not groups or re.search(r"[^\d\s\-]", body):
        return None
    if tuple(len(g) for g in groups) in _NDC_SHAPES:
        return "-".join(groups)
    return None  # includes ten bare digits, which fit all three shapes and so are ambiguous
