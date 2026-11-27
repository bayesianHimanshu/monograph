"""SchemaSpec -> JSON Schema (draft 2020-12).

The per-class schemas come from Pydantic's own generator, so they are exactly what Pydantic
enforces for JSON input. This module adds what Pydantic does not emit: the RDF IRIs of classes and
properties (`x-rdf-class`, `x-rdf-property`) and the rule projections (`if`/`then` for RequiredWhen,
`contains`/`minContains` for AtLeast). `pydantic_only` validators have no projection here either.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

from monograph.core.rules import AtLeast, RequiredWhen, RuleValue
from monograph.core.spec import ClassSpec, FieldSpec, Kind, SchemaSpec
from monograph.core.vocab import Vocabulary
from monograph.gen.shacl import forbidden_class


class JsonSchemaGenerator:
    def __init__(self, spec: SchemaSpec, vocab: Vocabulary, schema_id: str) -> None:
        self._spec = spec
        self._vocab = vocab
        self._schema_id = schema_id

    def document(self) -> dict[str, Any]:
        concrete = self._spec.concrete()
        _, top = models_json_schema([(c.cls, "validation") for c in concrete], ref_template="#/$defs/{model}")
        defs: dict[str, Any] = top.get("$defs", {})
        for c in concrete:
            d = defs[c.cls.__name__]
            d["x-rdf-class"] = str(self._vocab.cls(c.cls))
            for f in c.fields:
                d["properties"][f.name]["x-rdf-property"] = str(self._vocab.prop(f))
                _apply_constraints(d["properties"][f.name], f)
            rules = [self._rule(c, r) for r in c.rules]
            if rules:
                d["allOf"] = rules
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": self._schema_id,
            "title": "MONOGRAPH drug product model",
            "x-schema-version": self._spec.version,
            "anyOf": [{"$ref": f"#/$defs/{c.cls.__name__}"} for c in self._spec.entities()],
            "$defs": defs,
        }

    def _rule(self, cs: ClassSpec, rule: RequiredWhen | AtLeast) -> dict[str, Any]:
        if isinstance(rule, RequiredWhen):
            then = cs.field(rule.then)
            requirement: dict[str, Any] = {"required": [rule.then]}
            if then.is_collection:
                requirement["properties"] = {rule.then: {"minItems": 1}}
            return {
                "$comment": f"rule {rule.id}: {rule.message}",
                "if": {
                    "properties": {c.field: {"enum": [_json(v) for v in c.values]} for c in rule.when},
                    "required": [c.field for c in rule.when],
                },
                "then": requirement,
            }
        return {
            "$comment": f"rule {rule.id}: {rule.message}",
            "properties": {
                rule.field: {
                    "contains": {
                        "properties": {rule.where: {"enum": [_json(v) for v in rule.values]}},
                        "required": [rule.where],
                    },
                    "minContains": rule.min_count,
                }
            },
        }

    def render(self) -> str:
        return json.dumps(self.document(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"

    def extraction_document(self, root: type[BaseModel], version: str) -> dict[str, Any]:
        """The schema an extractor (an LLM) fills: `root` and what it embeds, without derived fields.

        Derived fields (resolver links, document metadata) are filled by deterministic code, so asking a
        model for them would invite invention. `version` is the extraction target's own stamp, not the model
        version: the file is frozen by an experiment's PREREG, so a model version bump must not rewrite it.
        """
        _, top = models_json_schema([(root, "validation")], ref_template="#/$defs/{model}")
        defs: dict[str, Any] = top.get("$defs", {})
        for name, d in defs.items():
            if "properties" not in d:
                continue
            c = self._spec.by_name(name)
            for f in c.fields:
                if f.derived is not None:
                    d["properties"].pop(f.name, None)
                    if f.name in d.get("required", []):
                        d["required"].remove(f.name)
                elif f.name in d["properties"]:
                    d["properties"][f.name]["x-rdf-property"] = str(self._vocab.prop(f))
                    _apply_constraints(d["properties"][f.name], f)
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{self._schema_id.rsplit('/', 1)[0]}/{version}/extraction",
            "title": f"MONOGRAPH extraction target: {root.__name__}",
            "x-schema-version": version,
            "$ref": f"#/$defs/{root.__name__}",
            "$defs": defs,
        }

    def render_extraction(self, root: type[BaseModel], version: str) -> str:
        return json.dumps(self.extraction_document(root, version), indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def _json(value: RuleValue) -> bool | int | str:
    return value if isinstance(value, bool | int) else str(value)


def _apply_constraints(prop: dict[str, Any], f: FieldSpec) -> None:
    """Write the field's constraints from the SchemaSpec into its JSON Schema.

    Pydantic's own rendering is not trusted for this: when a str type carries a validator (SafeIdentifier,
    SafeText), Pydantic splits the field into a validator chain and renders only the first step, which
    silently drops `pattern`. The spec is the single source, so the generator states every constraint.
    """
    if f.kind is not Kind.SCALAR or f.is_collection:
        return
    target = prop
    if "anyOf" in prop:  # T | None
        target = next((branch for branch in prop["anyOf"] if branch.get("type") != "null"), prop)
    k = f.constraints
    for key, value in (
        ("pattern", k.pattern),
        ("minLength", k.min_length),
        ("maxLength", k.max_length),
    ):
        if value is not None:
            target[key] = value
    for key, bound in (("minimum", k.ge), ("maximum", k.le), ("exclusiveMinimum", k.gt), ("exclusiveMaximum", k.lt)):
        if bound is None:
            continue
        if f.scalar is int:
            target[key] = int(bound)
        elif f.scalar is Decimal:
            # Pydantic accepts a Decimal as a JSON number or a numeric string. Bounds can only be stated for the
            # number branch; a bound on a string-encoded number is a declared JSON Schema leak.
            for branch in target.get("anyOf", [target]):
                if branch.get("type") == "number":
                    branch[key] = float(bound) if bound != int(bound) else int(bound)
    if k.charset is not None:
        target["not"] = {"pattern": forbidden_class(k.charset)}
