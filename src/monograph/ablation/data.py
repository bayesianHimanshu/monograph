"""The H2.1 dataset: the H2 frame plus the targeted high fan-out extension, built with identity on or off.

The extension's openFDA pages are cached under `data/raw/h2_1/` (not `data/raw/<endpoint>/`), so the H2
dataset build reads exactly what it read before. RxNav and SPL caches are shared: they are keyed by RxCUI,
NDC and set id, and a build only looks up what its records name.
"""

from __future__ import annotations

from pathlib import Path

from monograph import wiring
from monograph.graph.dataset import Dataset, RawInputs, _dedupe, read_raw

EXTENSION_DIR = "h2_1"


def extension_dir(root: Path) -> Path:
    return wiring.raw_dir(root) / EXTENSION_DIR


def read_cells_raw(root: Path) -> RawInputs:
    base = read_raw(wiring.raw_dir(root))
    ext_dir = extension_dir(root)
    if not ext_dir.exists():
        return base
    ext = read_raw(ext_dir)
    return RawInputs(
        ndc=_dedupe([*base.ndc, *ext.ndc], lambda r: f"{r.get('product_ndc', '')}"),
        label=base.label,
        event=base.event,
        enforcement=base.enforcement,
    )


def build_cells_dataset(root: Path, identity: bool) -> Dataset:
    from monograph.core.instances import Instantiator
    from monograph.graph.cli import _offline_clients
    from monograph.graph.dataset import DatasetBuilder
    from monograph.validate.pydantic_side import PydanticValidator

    _, _, dailymed, rxnav = _offline_clients(root)
    spec = wiring.build_spec()
    builder = DatasetBuilder(
        spec, Instantiator(spec), PydanticValidator(wiring.today()), rxnav, dailymed.spl, identity=identity
    )
    return builder.build(read_cells_raw(root))
