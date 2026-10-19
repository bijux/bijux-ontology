"""One closed description of project options, files and actual executor support."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, cast

from bijux_ontology.diagnostics.errors import InputError

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


@dataclass(frozen=True)
class JobDescriptor:
    """Describe the closed project job language and its supported executors."""

    keys: frozenset[str]
    direct_files: tuple[str, ...] = ()
    document_files: tuple[str, ...] = ()
    context_files: tuple[str, ...] = ()
    application_sides: tuple[str, ...] = ()
    executor: Literal["semantic", "application"] = "semantic"
    worker: Literal["isolated", "single-document"] | None = None


_COMMON = frozenset({"name", "kind", "depends_on"})
_RESOURCES = frozenset({"source", "lookup", "graph_name", "union"})

DESCRIPTORS: Mapping[CheckKind, JobDescriptor] = MappingProxyType(
    {
        "emission": JobDescriptor(
            _COMMON
            | _RESOURCES
            | {
                "allowed_drops",
                "base",
                "declarations",
                "document",
                "rdf_direction",
                "usages",
            },
            direct_files=("declarations", "document", "usages"),
            executor="application",
        ),
        "context-corpus": JobDescriptor(
            _COMMON
            | {
                "after_contexts",
                "before_contexts",
                "branches",
                "documents",
                "rdf_direction",
            },
            document_files=("before", "after", "expectations"),
            context_files=("before_contexts", "after_contexts"),
            executor="application",
        ),
        "upgrade": JobDescriptor(
            _COMMON | {"after", "before", "scopes"},
            application_sides=("before", "after"),
            executor="application",
        ),
        "query-rules": JobDescriptor(
            _COMMON | {"allow_empty", "max_rows", "rules", "source", "timeout"},
            direct_files=("rules",),
        ),
        "vocabulary": JobDescriptor(
            _COMMON
            | _RESOURCES
            | {
                "allow_empty",
                "coverage",
                "coverage_kinds",
                "coverage_scope",
                "coverage_terms",
                "exclude",
                "inventory",
                "lint",
                "namespaces",
                "references",
                "regime",
            },
            direct_files=("inventory",),
            worker="isolated",
        ),
        "models": JobDescriptor(
            _COMMON | _RESOURCES | {"declarations", "rdf_direction", "regime"},
            direct_files=("declarations",),
        ),
        "context": JobDescriptor(
            _COMMON
            | {"allowed_drops", "base", "document", "documents", "expectations"},
            direct_files=("document", "expectations"),
            document_files=("document", "expectations"),
            worker="single-document",
        ),
        "ontology": JobDescriptor(
            _COMMON
            | _RESOURCES
            | {
                "families",
                "include_imports",
                "include_lookup",
                "languages",
                "regime",
                "replacement_predicates",
                "required_annotations",
                "used_terms",
            }
        ),
        "data": JobDescriptor(
            _COMMON
            | _RESOURCES
            | {
                "check_literals",
                "check_types",
                "data_graph",
                "data_union",
                "document",
                "exclude",
            }
        ),
        "shacl": JobDescriptor(
            _COMMON
            | {
                "allow_empty",
                "data_graph",
                "data_union",
                "document",
                "focus_nodes",
                "mode",
                "ontology",
                "preflight",
                "shapes",
                "timeout",
                "trusted_sparql",
                "use_shapes",
            },
            worker="isolated",
        ),
        "skos": JobDescriptor(
            _COMMON
            | _RESOURCES
            | {
                "closed_targets",
                "concepts",
                "definition_predicates",
                "hierarchy_basis",
                "language_fallback",
                "languages",
                "notation_collections",
                "profile",
                "replacement_predicates",
                "rules",
                "schemes",
                "scopes",
            }
        ),
    }
)

APPLICATION_KINDS = frozenset(
    kind for kind, item in DESCRIPTORS.items() if item.executor == "application"
)


def describe(kind: str) -> JobDescriptor:
    """Resolve an existing kind without loading files or validating sibling jobs."""
    result = DESCRIPTORS.get(cast(CheckKind, kind))
    if result is None:
        raise InputError("Unsupported project check kind", location=kind)
    return result
