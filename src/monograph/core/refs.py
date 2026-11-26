"""`Ref[T]`: a typed reference to another entity, held as an IRI string."""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import GetCoreSchemaHandler
from pydantic_core import PydanticCustomError, core_schema

# Absolute http(s) IRI made of unreserved, reserved and percent-encoded characters only.
# The same pattern is projected into SHACL (sh:pattern on reference properties) and into DDL.
IRI_PATTERN = r"^https?://[A-Za-z0-9._~:/?#@!$&'()*+,;=%\-]+$"

T = TypeVar("T")


class Ref(Generic[T]):
    """A reference to an instance of `T`, identified by its IRI.

    The type parameter is the target class. It is read by the generators (owl:ObjectProperty range,
    sh:class) and has no effect at runtime beyond documentation.
    """

    __slots__ = ("iri",)

    def __init__(self, iri: str) -> None:
        self.iri = iri

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Ref) and other.iri == self.iri

    def __hash__(self) -> int:
        return hash(("Ref", self.iri))

    def __repr__(self) -> str:
        return f"Ref({self.iri!r})"

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        def unwrap(value: Any) -> Any:
            if isinstance(value, Ref):
                return value.iri
            if isinstance(value, str):
                return value
            raise PydanticCustomError("ref_type", "a reference must be a Ref or an IRI string")

        def wrap(value: str) -> Ref[Any]:
            return Ref(value)

        iri = core_schema.str_schema(pattern=IRI_PATTERN, strict=True)
        return core_schema.json_or_python_schema(
            json_schema=core_schema.chain_schema([iri, core_schema.no_info_plain_validator_function(wrap)]),
            python_schema=core_schema.chain_schema(
                [
                    core_schema.no_info_plain_validator_function(unwrap),
                    iri,
                    core_schema.no_info_plain_validator_function(wrap),
                ]
            ),
            serialization=core_schema.plain_serializer_function_ser_schema(lambda r: r.iri),
        )
