from __future__ import annotations

import pytest

from monograph.core.refs import IRI_PATTERN
from monograph.core.regex import RegexSubsetError, analyze_pattern, check_pattern, safe
from monograph.core.spec import SchemaSpec
from monograph.gen.shacl import TZ_PATTERN


def test_every_model_pattern_is_in_the_subset(spec: SchemaSpec) -> None:
    patterns = [f.constraints.pattern for c in spec.classes for f in c.fields if f.constraints.pattern]
    assert len(patterns) >= 8
    for p in [*patterns, IRI_PATTERN, TZ_PATTERN]:
        check_pattern(p)


@pytest.mark.parametrize(
    "pattern",
    [r"^[0-9A-Z]{10}$", r"^(a|bc)+$", r"^[^ ]{1,255}$", r"^x\.y\-z$", "abc", r"^a{2,}$|^b$"],
)
def test_accepted(pattern: str) -> None:
    check_pattern(pattern)


@pytest.mark.parametrize(
    ("pattern", "reason"),
    [
        (r"^\D{10}$", "escape"),
        (r"^[\d-z]$", "range"),
        (r"^\w+$", "escape"),
        (r"^a.c$", "newlines"),
        (r"^(?=a)a$", "lookaround"),
        (r"^(?:a|b)$", "lookaround"),
        (r"^(?i)abc$", "inline flags"),
        (r"^(a)\1$", "escape"),
        (r"^a+?$", "lazy"),
        (r"^a{1,300}$", "Postgres"),
        (r"^[a-z-[aeiou]]$", "subtraction|literal"),
        (r"^[a-]$", "literal '-'"),
        ("^café$", "printable ASCII"),
        (r"^a\/b$", "escape"),
        (r"a^b", "start"),
        (r"^a$b", "end"),
        (r"^(a$)", "end"),
        (r"^(ab", "unbalanced"),
        (r"^[]$", "empty"),
        (r"^a**$", "stacked"),
    ],
)
def test_rejected(pattern: str, reason: str) -> None:
    with pytest.raises(RegexSubsetError, match=reason):
        check_pattern(pattern)


@pytest.mark.parametrize(
    ("pattern", "canonical"),
    [
        (r"^\d{10}$", r"^[0-9]{10}$"),
        (r"^[\dA-F]{2}$", r"^[0-9A-F]{2}$"),
        (r"^D-\d{3,4}-\d{4}$", r"^D-[0-9]{3,4}-[0-9]{4}$"),
        (r"^[0-9]+$", r"^[0-9]+$"),
    ],
)
def test_digit_escape_is_canonicalized(pattern: str, canonical: str) -> None:
    assert safe(pattern) == canonical
    check_pattern(canonical)


def test_raw_digit_escape_is_not_canonical() -> None:
    """A model must not hand Pydantic a raw \\d: Rust regex would read it as any Unicode digit."""
    with pytest.raises(RegexSubsetError, match="canonical"):
        check_pattern(r"^\d{10}$")


@pytest.mark.parametrize(
    ("pattern", "low", "high"),
    [
        (r"^[0-9A-Z]{10}$", 10, 10),
        (r"^([0-9]{4}-[0-9]{4}|[0-9]{5}-[0-9]{3})$", 9, 9),
        (r"^[0-9]{1,12}(-[0-9]{1,3})?$", 1, 16),
        (r"^[a-z]+$", 1, None),
        (r"^a$|b", 0, None),
        ("abc", 0, None),
    ],
)
def test_length_bounds(pattern: str, low: int, high: int | None) -> None:
    analysis = analyze_pattern(pattern)
    assert (analysis.min_length, analysis.max_length) == (low, high)
