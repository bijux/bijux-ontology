"""Typed project options and explicit vocabulary/RDF resource selections."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.json.values import known_keys, text_value

from .descriptors import CheckKind, describe


def strings(value: object) -> tuple[str, ...]:
    """Decode an explicit string-array check option without coercion."""
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise InputError("Expected an array of strings")
    return tuple(cast(list[str], value))


def boolean(value: object) -> bool:
    """Decode an explicit boolean check option without integer coercion."""
    if type(value) is not bool:
        raise InputError("Expected a boolean")
    return value


def check_kind(job: Mapping[str, object]) -> CheckKind:
    """Validate only this selected job and reject options its executor would ignore."""
    kind = text_value(job.get("kind"), location="kind")
    known_keys(job, set(describe(kind).keys), location=str(job.get("name", kind)))
    return cast(CheckKind, kind)


def dependencies(job: Mapping[str, object]) -> tuple[str, ...]:
    """Decode explicit required earlier jobs, without inferring execution order."""
    selected = strings(job.get("depends_on", []))
    if len(set(selected)) != len(selected):
        raise InputError("Job dependencies must be unique")
    for name in selected:
        text_value(name, location="depends_on")
    return selected


@dataclass(frozen=True)
class GraphSelection:
    """A check's explicit graph name or union choice."""

    name: str | None
    union: bool

    @classmethod
    def from_job(
        cls,
        job: Mapping[str, object],
        *,
        name_key: str = "graph_name",
        union_key: str = "union",
    ) -> "GraphSelection":
        """Decode one graph selection for vocabulary, data or shapes checks."""
        name = job.get(name_key)
        if name is not None:
            name = text_value(name, location=name_key)
        return cls(name, boolean(job.get(union_key, False)))


@dataclass(frozen=True)
class VocabularySelection:
    """Named local and lookup resources with one explicit RDF graph scope."""

    source: str
    lookup: tuple[str, ...]
    graph: GraphSelection

    @classmethod
    def from_job(cls, job: Mapping[str, object]) -> "VocabularySelection":
        """Decode resource selection shared by vocabulary-backed check kinds."""
        graph = GraphSelection.from_job(job)
        return cls(
            text_value(job.get("source"), location="source"),
            strings(job.get("lookup", [])),
            graph,
        )
