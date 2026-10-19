"""Finite membership conclusions from exact selected declaration kinds."""

from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget
from bijux_ontology.owl.encoding.roles import Regime, builtin_kinds, kind_matches


def membership_issue(
    term: TermDeclaration,
    declared: frozenset[str],
    *,
    include_builtins: bool,
    regime: Regime,
) -> Issue | None:
    """Compare one qualified occurrence without claiming ontology coverage.

    The caller owns source selection, term admission and exclusions. Local-only
    declaration reads exclude builtins; ordinary selected reads may add their
    supported declared roles. Custom RDF type identities remain unchanged.
    """
    kinds = declared | builtin_kinds(term.iri, regime) if include_builtins else declared
    if not kinds:
        return Issue(
            "vocabulary.term_missing",
            "Used term has no explicit declaration",
            term.location or term.iri,
            actual=term.iri,
        )
    if not kind_matches(kinds, term.kinds, regime):
        return Issue(
            "vocabulary.entity_kind_mismatch",
            "Term declaration has a different supported kind",
            term.location or term.iri,
            expected=", ".join(sorted(term.kinds)),
            actual=", ".join(sorted(kinds)),
        )
    return None


def read_graph_kinds(graph: Graph, iri: str, budget: Budget) -> frozenset[str]:
    """Capture named type facts from an already owned physical selection."""
    budget.consume("states")
    budget.reserve_storage(256)
    values: set[str] = set()
    for kind in graph.objects(URIRef(iri), RDF.type):
        budget.consume("work")
        if isinstance(kind, URIRef):
            budget.consume("states")
            budget.reserve_storage(128 + 4 * len(kind))
            budget.consume("work", len(kind))
            values.add(str(kind))
    budget.consume("states")
    budget.reserve_storage(256 + 32 * len(values))
    return frozenset(values)
