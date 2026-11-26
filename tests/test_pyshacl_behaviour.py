"""Characterization tests for pySHACL behaviour that H6 depends on or works around.

If one of these fails after a pySHACL upgrade, the behaviour changed: revisit validate/shacl_side.py
and the findings in docs/h6/RESULTS.md.
"""

from __future__ import annotations

from monograph.validate.pyshacl_probes import dollar_matches_before_trailing_newline, focus_nodes_skip_qualified_shapes


def test_focus_nodes_mode_skips_qualified_value_shapes() -> None:
    assert focus_nodes_skip_qualified_shapes().observed


def test_pattern_dollar_matches_before_trailing_newline() -> None:
    assert dollar_matches_before_trailing_newline().observed
