"""SchemaSpec -> JSON Schema (draft 2020-12).

The per-class schemas come from Pydantic's own generator, so they are exactly what Pydantic
enforces for JSON input. This module adds what Pydantic does not emit: the RDF IRIs of classes and
properties (`x-rdf-class`, `x-rdf-property`) and the rule projections (`if`/`then` for RequiredWhen,
`contains`/`minContains` for AtLeast). `pydantic_only` validators have no projection here either.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic.json_schema import models_json_schema

from monograph.core.rules import AtLeast, RequiredWhen, RuleValue
from monograph.core.spec import ClassSpec, SchemaSpec
from monograph.core.vocab import Vocabulary


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

    def _rule(self, c: ClassSpec, rule: RequiredWhen | AtLeast) -> dict[str, Any]:
        if isinstance(rule, RequiredWhen):
            then = c.field(rule.then)
            requirement: dict[str, Any] = {"required": [rule.then]}
            if then.is_collection:
                requirement["properties"] = {rule.then: {"minItems": 1}}
            return {
                "$comment": f"rule {rule.id}: {rule.message}",
                "if": {"properties": {rule.when: {"const": _json(rule.equals)}}, "required": [rule.when]},
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


def _json(value: RuleValue) -> bool | int | str:
    return value if isinstance(value, bool | int) else str(value)
