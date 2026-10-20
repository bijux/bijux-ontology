"""Immutable scope, origin and fidelity receipts for checked RDF snapshots."""

from dataclasses import dataclass

from rdflib import BNode, Literal

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node


@dataclass(frozen=True)
class SnapshotMetadata:
    """Owned immutable scope, origin and fidelity at map creation."""

    is_dataset: bool
    document_iri: str | None
    effective_url: str | None
    literal_fidelity: str
    graph_fidelity: str
    graph_name: str | BNode | None
    union: bool

    @classmethod
    def capture(cls, source: PreparedSource, budget: Budget) -> "SnapshotMetadata":
        """Bound current public metadata before copying or comparing any fields."""
        if not isinstance(source, PreparedSource):
            raise InputError("RDF metadata requires an owned snapshot")
        budget.consume("states", 7)
        budget.consume("bytes", 112)
        is_dataset, document_iri, effective_url = (
            source.is_dataset,
            source.document_iri,
            source.effective_url,
        )
        literal_fidelity, graph_fidelity, graph, union = (
            source.literal_fidelity,
            source.graph_fidelity,
            source.graph_name,
            source.union,
        )
        if type(is_dataset) is not bool or type(union) is not bool:
            raise InputError("RDF snapshot scope flags must remain booleans")
        for text in (document_iri, effective_url, literal_fidelity, graph_fidelity):
            if text is not None:
                if not isinstance(text, str) or isinstance(text, BNode | Literal):
                    raise InputError(
                        "RDF snapshot metadata requires IRI or fidelity text"
                    )
                bound_text(text, budget)
        if literal_fidelity is None or graph_fidelity is None:
            raise InputError("RDF snapshot fidelity requires explicit text")
        if isinstance(graph, BNode):
            bound_node(graph, budget)
        elif graph is not None:
            if not isinstance(graph, str) or isinstance(graph, Literal):
                raise InputError(
                    "RDF snapshot graph scope requires IRI text or an owned blank"
                )
            bound_text(graph, budget)
            graph = str(graph)
        return cls(
            is_dataset,
            None if document_iri is None else str(document_iri),
            None if effective_url is None else str(effective_url),
            str(literal_fidelity),
            str(graph_fidelity),
            graph,
            union,
        )

    def same_scope(self, other: "SnapshotMetadata") -> bool:
        """Blank graph scope requires its actual object, not an equal label."""
        graph_equal = (
            self.graph_name is other.graph_name
            if isinstance(self.graph_name, BNode) or isinstance(other.graph_name, BNode)
            else self.graph_name == other.graph_name
        )
        return graph_equal and (
            self.is_dataset,
            self.document_iri,
            self.effective_url,
            self.literal_fidelity,
            self.graph_fidelity,
            self.union,
        ) == (
            other.is_dataset,
            other.document_iri,
            other.effective_url,
            other.literal_fidelity,
            other.graph_fidelity,
            other.union,
        )
