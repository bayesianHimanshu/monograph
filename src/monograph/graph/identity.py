"""Deterministic identity rules for H2. No similarity matching: names are tied to a firm by an identifier."""

from __future__ import annotations

import re
import unicodedata

_NDC10 = re.compile(r"^(\d{4,5})-(\d{3,4})(?:-(\d{1,2}))?$")

# Corporate suffixes removed by firm-name normalization. A fixed list; nothing else is folded.
FIRM_SUFFIXES = (
    "INCORPORATED",
    "INC",
    "LLC",
    "L L C",
    "LTD",
    "LIMITED",
    "CORPORATION",
    "CORP",
    "COMPANY",
    "CO",
    "LP",
    "L P",
    "PLC",
    "GMBH",
    "AG",
    "SA",
    "S A",
    "SPA",
    "S P A",
    "BV",
    "B V",
    "PVT",
    "PRIVATE",
)
_SUFFIX = re.compile(r"(?:\s+(?:" + "|".join(re.escape(s) for s in FIRM_SUFFIXES) + r"))+$")


def ndc11(ndc: str) -> str | None:
    """The 11-digit HIPAA (5-4-2) form of a 10-digit hyphenated package NDC: a leading zero is added to the
    short segment (4-4-2 -> 0+4-4-2, 5-3-2 -> 5-0+3-2, 5-4-1 -> 5-4-0+1). None for anything else."""
    m = _NDC10.fullmatch(ndc)
    if m is None or m.group(3) is None:
        return None
    a, b, c = m.groups()
    if (len(a), len(b), len(c)) == (4, 4, 2):
        return "0" + a + b + c
    if (len(a), len(b), len(c)) == (5, 3, 2):
        return a + "0" + b + c
    if (len(a), len(b), len(c)) == (5, 4, 1):
        return a + b + "0" + c
    return None


def labeler_code(ndc: str) -> str:
    """The first NDC segment: FDA assigns it to one labeler."""
    return ndc.split("-", 1)[0]


def normalize_firm(name: str) -> str:
    """Case, width, punctuation, whitespace and the fixed list of corporate suffixes."""
    text = unicodedata.normalize("NFKC", name).upper()
    text = re.sub(r"[^A-Z0-9 ]+", " ", text.replace("&", " AND "))
    text = re.sub(r"\s+", " ", text).strip()
    return _SUFFIX.sub("", text).strip()
