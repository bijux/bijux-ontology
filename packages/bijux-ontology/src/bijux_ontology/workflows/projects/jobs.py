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
    "emission",
    "context-corpus",
    "upgrade",
]

APPLICATION_KINDS = frozenset(("emission", "context-corpus", "upgrade"))


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
    common = {"name", "kind", "depends_on"}
    if kind not in ("context", "shacl", "query-rules", "context-corpus", "upgrade"):
        common |= {"source", "lookup", "graph_name", "union"}
    options = {
        "emission": {
            "declarations",
            "document",
            "usages",
            "base",
            "allowed_drops",
            "rdf_direction",
        },
        "context-corpus": {
            "documents",
            "branches",
            "before_contexts",
            "after_contexts",
            "rdf_direction",
        },
        "upgrade": {"before", "after", "scopes"},
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


def dependencies(job: Mapping[str, object]) -> tuple[str, ...]:
    """Decode explicit required earlier jobs, without inferring execution order."""
    selected = strings(job.get("depends_on", []))
    if len(set(selected)) != len(selected):
        raise InputError("Job dependencies must be unique")
    for name in selected:
        text_value(name, location="depends_on")
    return selected


def input_paths(
    job: Mapping[str, object],
) -> tuple[tuple[tuple[str | int, ...], object], ...]:
    """Locate direct configured filenames without opening any selected input.

    Application files contain owned inline outputs/resources, never further
    filenames. Unusable slots cannot be opened by their runner and are left to
    that runner's per-job diagnostics; discovery must not erase sibling results.
    """
    kind = job.get("kind")
    paths: list[tuple[tuple[str | int, ...], object]] = []
    keys = {
        "vocabulary": ("inventory",),
        "models": ("declarations",),
        "context": ("document", "expectations"),
        "query-rules": ("rules",),
        "emission": ("declarations", "document", "usages"),
    }
    for key in keys.get(str(kind), ()):
        if key in job:
            paths.append(((key,), job[key]))
    if kind in ("context", "context-corpus"):
        members = job.get("documents", [])
        if not isinstance(members, list):
            members = []
        selected_keys = (
            ("document", "expectations")
            if kind == "context"
            else ("before", "after", "expectations")
        )
        for index, raw in enumerate(members):
            if not isinstance(raw, dict):
                continue
            member = raw
            for key in selected_keys:
                if key in member:
                    paths.append((("documents", index, key), member[key]))
    if kind == "context-corpus":
        for role in ("before_contexts", "after_contexts"):
            contexts = job.get(role, {})
            if not isinstance(contexts, dict):
                continue
            for iri, value in contexts.items():
                paths.append(((role, iri), value))
    if kind == "upgrade":
        for side in ("before", "after"):
            if side in job:
                side_entry = job[side]
                if not isinstance(side_entry, dict):
                    continue
                if "application" in side_entry:
                    paths.append(((side, "application"), side_entry["application"]))
    return tuple(
        (path, value)
        for path, value in paths
        if isinstance(value, str) and value.strip()
    )


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
