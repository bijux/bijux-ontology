"""Exact selected source scope and coherent existential template witnesses."""

from dataclasses import dataclass

from rdflib import BNode

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.queries.protocol import QueryTerm
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource


@dataclass(frozen=True)
class TemplateBinding:
    """An actual template-owned blank variable and a selected data-owned RDF term."""

    variable: BNode
    value: QueryTerm


@dataclass(frozen=True)
class ContainmentCheck:
    """One complete simple-entailment answer, or explicitly unavailable proof.

    Variable and data blank references belong to the returned original sources.
    Different variables may have the same value. This is graph containment, not
    graph isomorphism, RDF datatype inference or an OWL entailment operation.
    """

    data: PreparedSource
    template: PreparedSource
    data_graph: GraphSelection | None
    template_graph: GraphSelection | None
    entailed: bool | None
    bindings: tuple[TemplateBinding, ...]
    report: Report
