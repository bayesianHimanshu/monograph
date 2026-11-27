"""PREREG.md freezes the prompt, schema, feedback, resolution and scoring code by sha256. A change to any of
them after pre-registration fails here and blocks `monograph h4 run --split test` (log it as a deviation)."""

from __future__ import annotations

import shutil
from pathlib import Path

from monograph.extract.cli import frozen_drift

from .conftest import ROOT

H4_SCHEMA_VERSION = "2.1.1"  # the schema version H4 was pre-registered and run on


def test_frozen_artifacts_match_prereg() -> None:
    """After H4, later experiments bump the schema version, which restamps the extraction schema's `$id` and
    `x-schema-version`. That is the only drift allowed: re-stamped with H4's version, the file must hash to
    the frozen digest exactly, and the as-sent schema and prompt hashes must not change. `frozen_drift`
    itself stays strict, so an H4 test rerun is still refused."""
    import hashlib
    import re

    from monograph.core.version import SCHEMA_VERSION

    drift = frozen_drift(ROOT)
    schema_file = "generated/monograph.extraction.schema.json"
    assert drift in ([], [schema_file])
    if drift:
        text = (
            (ROOT / schema_file)
            .read_text(encoding="utf-8")
            .replace(f'"x-schema-version": "{SCHEMA_VERSION}"', f'"x-schema-version": "{H4_SCHEMA_VERSION}"')
        )
        text = text.replace(f"/schema/{SCHEMA_VERSION}/extraction", f"/schema/{H4_SCHEMA_VERSION}/extraction")
        prereg = (ROOT / "docs" / "h4" / "PREREG.md").read_text(encoding="utf-8")
        frozen = re.search(rf"^\| `{re.escape(schema_file)}` \| `([0-9a-f]{{64}})` \|$", prereg, re.MULTILINE)
        assert frozen is not None
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == frozen.group(1)


def test_a_changed_frozen_file_is_reported(tmp_path: Path) -> None:
    for part in ("docs", "src", "generated", "fixtures", "data"):
        if (ROOT / part).exists():
            (tmp_path / part).symlink_to(ROOT / part) if part == "data" else shutil.copytree(
                ROOT / part, tmp_path / part
            )
    score = tmp_path / "src" / "monograph" / "extract" / "score.py"
    score.write_text(score.read_text() + "\n# edited\n")
    assert "src/monograph/extract/score.py" in frozen_drift(tmp_path)
