"""Closed project-check options and typed resource selections."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from bijux_ontology._json import known_keys, text_value
from bijux_ontology.errors import InputError

CheckKind = Literal[
    "vocabulary",
    "models",
    "context",
    "ontology",
    "data",
    "shacl",
    "skos",
    "query-rules",
]


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
    """Validate the selected operation and reject ignored check options lazily."""
    kind = text_value(job.get("kind"), location="kind")
    common = {"name", "kind"}
    if kind not in ("context", "shacl", "query-rules"):
        common |= {"source", "lookup", "graph_name", "union"}
    options = {
        "query-rules": {"source", "rules", "timeout", "max_rows", "allow_empty"},
        "vocabulary": {
            "inventory",
            "namespaces",
            "exclude",
            "coverage",
            "coverage_terms",
            "coverage_kinds",
            "coverage_scope",
            "regime",
            "references",
            "lint",
            "allow_empty",
        },
        "models": {"declarations", "regime", "rdf_direction"},
        "context": {
            "document",
            "expectations",
            "base",
            "allowed_drops",
            "documents",
        },
        "ontology": {
            "regime",
            "families",
            "include_imports",
            "include_lookup",
            "required_annotations",
            "languages",
            "used_terms",
            "replacement_predicates",
        },
        "data": {
            "document",
            "data_graph",
            "data_union",
            "exclude",
            "check_types",
            "check_literals",
        },
        "shacl": {
            "document",
            "shapes",
            "ontology",
            "data_graph",
            "data_union",
            "trusted_sparql",
            "focus_nodes",
            "use_shapes",
            "mode",
            "preflight",
            "timeout",
            "allow_empty",
        },
        "skos": {
            "profile",
            "hierarchy_basis",
            "scopes",
            "concepts",
            "schemes",
            "rules",
            "languages",
            "language_fallback",
            "definition_predicates",
            "closed_targets",
            "notation_collections",
            "replacement_predicates",
        },
    }
    if kind not in options:
        raise InputError("Unsupported project check kind", location=kind)
    known_keys(job, common | options[kind], location=str(job.get("name", kind)))
    return cast(CheckKind, kind)


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
