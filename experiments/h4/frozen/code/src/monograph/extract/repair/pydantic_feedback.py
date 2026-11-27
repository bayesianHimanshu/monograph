"""Record-local feedback: Pydantic's verdict on the extraction, written for the model to act on."""

from __future__ import annotations

from collections.abc import Sequence

from monograph.validate.verdict import Violation


def pydantic_feedback(violations: Sequence[Violation], parse_error: str | None = None) -> str:
    lines = ["Your previous answer does not validate. Fix exactly these problems and answer again in full."]
    if parse_error is not None:
        lines.append(f"- the answer is not valid JSON: {parse_error}")
    for v in violations:
        lines.append(f"- at `{_location(v.path)}`: {v.message}")
    return "\n".join(lines)


def _location(path: str) -> str:
    """Pydantic loc `products.0.ingredients.1.strength_unit` -> `products[0].ingredients[1].strength_unit`."""
    out: list[str] = []
    for part in path.split("."):
        if part.isdigit() and out:
            out[-1] += f"[{part}]"
        else:
            out.append(part)
    return ".".join(out)
