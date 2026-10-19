"""Position-qualified references from one actual maintained SPARQL translation."""

from dataclasses import dataclass
from typing import Literal, TypeAlias

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report

AlgebraPosition: TypeAlias = tuple[str | int, ...]
QueryRole = Literal["class", "predicate", "datatype", "data", "function", "graph"]


@dataclass(frozen=True)
class QueryReference:
    """One syntactic occurrence; positions refer to algebra, never source offsets."""

    iri: str
    role: QueryRole
    position: AlgebraPosition
    location: str
    binding_position: AlgebraPosition | None = None
    reverse: bool = False


@dataclass(frozen=True)
class QueryDynamicScope:
    """A required dependency domain the supplied program cannot finitely establish."""

    role: str
    position: AlgebraPosition
    reason: str
    variable: str | None = None


@dataclass(frozen=True, init=False)
class QueryUsage:
    """Factory-owned immutable source identity, finite references and raw coverage."""

    source: str
    kind: Literal["query", "update"]
    text_sha256: str
    references: tuple[QueryReference, ...]
    dynamic: tuple[QueryDynamicScope, ...]
    report: Report

    def __init__(self) -> None:
        raise InputError("Use read_query_usage to qualify actual SPARQL input")

    @classmethod
    def _owned(
        cls,
        source: str,
        kind: Literal["query", "update"],
        text_sha256: str,
        references: tuple[QueryReference, ...],
        dynamic: tuple[QueryDynamicScope, ...],
        report: Report,
    ) -> "QueryUsage":
        result = object.__new__(cls)
        for name, value in (
            ("source", source),
            ("kind", kind),
            ("text_sha256", text_sha256),
            ("references", references),
            ("dynamic", dynamic),
            ("report", report),
        ):
            object.__setattr__(result, name, value)
        return result
