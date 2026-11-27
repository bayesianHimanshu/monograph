"""The four H2.1 cells (identity layer on/off x schema descriptions on/off) and their generated artifacts.

Every cell's schema comes from the H6 `SchemaSpec`:

* identity off removes the identity-layer fields (`IDENTITY_FIELDS`) from the spec, so their tables, columns
  and edge labels do not exist in either store (rather than exist empty);
* descriptions off generates the DDL without COMMENTs and the projection with blank descriptions.

The artifacts are written flat into `generated/` as `h2_1.<cell>.<file>` by `monograph gen` (generators write
only top-level generated files) and drift-checked with the rest.
The baseline cell's artifacts equal H2's (`generated/monograph.ddl.sql`, `generated/monograph.age.json`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from pydantic import BaseModel

from monograph.core.spec import SchemaSpec

# (class, field): every mapping whose job is to say that two identifiers or names denote the same thing, or a
# canonical counterpart of it (docs/h2_1/PLAN.md, "The identity layer, exactly").
IDENTITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("Product", "clinical_drug"),  # Product to RxCUI crosswalk (openFDA, exactly one RxCUI)
    ("Product", "drug_mappings"),  # Product to RxCUI crosswalk (openFDA, RxNav)
    ("DrugInvolvement", "clinical_drug"),  # report drug to RxCUI (openFDA harmonization, exactly one)
    ("DrugInvolvement", "harmonized_clinical_drugs"),  # report drug to RxCUI (openFDA harmonization)
    ("PackagedProduct", "ndc11"),  # 11-digit NDC, derived
    ("Substance", "active_moiety"),  # salt to moiety (SPL)
    ("ClinicalDrug", "generic"),  # brand to generic (RxNav)
    ("Organization", "aliases"),  # firm aliases (NDC directory, RxNav, enforcement)
)

GENERATED_PREFIX = "h2_1"


@dataclass(frozen=True, slots=True)
class Cell:
    identity: bool
    descriptions: bool

    @property
    def name(self) -> str:
        return f"id-{'on' if self.identity else 'off'}_desc-{'on' if self.descriptions else 'off'}"

    @property
    def short(self) -> str:
        return f"i{int(self.identity)}d{int(self.descriptions)}"

    @property
    def database(self) -> str:
        """Each cell is its own database: comments are readable by any role in the same database."""
        return f"monograph_h2_1_{self.short}"

    @property
    def sql_role(self) -> str:
        return f"h21_{self.short}_sql"

    @property
    def graph_role(self) -> str:
        return f"h21_{self.short}_graph"

    def artifact(self, file: str) -> str:
        """The `generated/` file name of one of this cell's artifacts."""
        return f"{GENERATED_PREFIX}.{self.name}.{file}"


BASELINE = Cell(identity=True, descriptions=True)
HARDEST = Cell(identity=False, descriptions=False)
CELLS: tuple[Cell, ...] = (
    BASELINE,
    Cell(identity=True, descriptions=False),
    Cell(identity=False, descriptions=True),
    HARDEST,
)
CELL_BY_NAME = {c.name: c for c in CELLS}


def ablate_spec(spec: SchemaSpec, identity: bool) -> SchemaSpec:
    """The spec without the identity-layer fields (identity off), or the spec itself (identity on)."""
    if identity:
        return spec
    drop: dict[str, set[str]] = {}
    for cls_name, field_name in IDENTITY_FIELDS:
        drop.setdefault(cls_name, set()).add(field_name)
    classes = []
    for c in spec.classes:
        gone = drop.pop(c.cls.__name__, set())
        missing = gone - {f.name for f in c.fields}
        if missing:
            raise KeyError(f"{c.cls.__name__} has no identity field {sorted(missing)}")
        classes.append(replace(c, fields=tuple(f for f in c.fields if f.name not in gone)) if gone else c)
    if drop:
        raise KeyError(f"identity fields on classes not in the spec: {sorted(drop)}")
    return replace(spec, classes=tuple(classes), _by_cls={})


def cell_renderers(
    spec: SchemaSpec, check_pattern: Callable[[str], None], excluded: tuple[type[BaseModel], ...]
) -> dict[str, Callable[[], str]]:
    """`generated/`-relative file name -> renderer, for every cell's DDL and graph projection."""
    from monograph.gen.age import AgeProjectionGenerator
    from monograph.gen.artifacts import AGE_FILE, DDL_FILE
    from monograph.gen.ddl import DdlGenerator, DdlProfile

    out: dict[str, Callable[[], str]] = {}
    for cell in CELLS:
        ablated = ablate_spec(spec, cell.identity)
        ddl = DdlGenerator(ablated, check_pattern, DdlProfile.DEFAULT, descriptions=cell.descriptions)
        age = AgeProjectionGenerator(ablated, excluded, descriptions=cell.descriptions)
        out[cell.artifact(DDL_FILE)] = ddl.render
        out[cell.artifact(AGE_FILE)] = age.render
    return out
