from __future__ import annotations

import pytest

from monograph.core.refs import IRI_PATTERN
from monograph.core.spec import SchemaSpec
from monograph.gen.regex import RegexSubsetError, check_pattern
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
        (r"^\d{10}$", "escape"),
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
