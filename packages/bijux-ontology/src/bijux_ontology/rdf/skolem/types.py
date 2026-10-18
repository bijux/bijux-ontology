"""Snapshot-bound evidence for explicit RDF identity replacement."""

from dataclasses import dataclass

from rdflib import BNode, Literal, URIRef

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, bound_text
from bijux_ontology.reports import Report


@dataclass(frozen=True)
class SkolemBinding:
    """One original owned blank node and its explicitly generated named identity."""

    original: BNode
    iri: URIRef

    def __post_init__(self) -> None:
        """Keep actual RDF object witnesses instead of lexical substitutes."""
        if not isinstance(self.original, BNode) or not isinstance(self.iri, URIRef):
            raise InputError("Skolem bindings require an actual BNode and URIRef")


@dataclass(frozen=True)
class _SnapshotMetadata:
    """Owned immutable scope, origin and fidelity at map creation."""

    is_dataset: bool
    document_iri: str | None
    effective_url: str | None
    literal_fidelity: str
    graph_fidelity: str
    graph_name: str | BNode | None
    union: bool

    @classmethod
    def capture(cls, source: PreparedSource, budget: Budget) -> "_SnapshotMetadata":
        """Bound current public metadata before copying or comparing any fields."""
        if not isinstance(source, PreparedSource):
            raise InputError("Skolem map metadata requires an owned snapshot")
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
            raise InputError("Skolem snapshot scope flags must remain booleans")
        for text in (document_iri, effective_url, literal_fidelity, graph_fidelity):
            if text is not None:
                if not isinstance(text, str) or isinstance(text, BNode | Literal):
                    raise InputError(
                        "Skolem snapshot metadata requires IRI or fidelity text"
                    )
                bound_text(text, budget)
        if literal_fidelity is None or graph_fidelity is None:
            raise InputError("Skolem snapshot fidelity requires explicit text")
        if isinstance(graph, BNode):
            bound_node(graph, budget)
        elif graph is not None:
            if not isinstance(graph, str) or isinstance(graph, Literal):
                raise InputError(
                    "Skolem snapshot graph scope requires IRI text or an owned blank"
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

    def same_scope(self, other: "_SnapshotMetadata") -> bool:
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


@dataclass(frozen=True, init=False)
class SkolemMap:
    """Factory-owned correspondence between two exact in-memory snapshots.

    A serialized map, equal blank labels, or a cloned output does not establish
    this ownership. Shared operation maps require explicit owned BlankNodePairs.
    """

    original: PreparedSource
    output: PreparedSource
    base: str
    operation: str
    bindings: tuple[SkolemBinding, ...]
    _original_metadata: _SnapshotMetadata
    _output_metadata: _SnapshotMetadata

    def __init__(self) -> None:
        """Prevent public construction or dataclass replacement of ownership."""
        raise InputError("Skolem maps are created only by skolemize")

    @classmethod
    def _from_owned(
        cls,
        original: PreparedSource,
        output: PreparedSource,
        base: str,
        operation: str,
        bindings: tuple[SkolemBinding, ...],
        budget: Budget,
    ) -> "SkolemMap":
        """Retain already qualified immutable correspondence and snapshot owners."""
        original_metadata = _SnapshotMetadata.capture(original, budget)
        output_metadata = _SnapshotMetadata.capture(output, budget)
        result = object.__new__(cls)
        for name, value in (
            ("original", original),
            ("output", output),
            ("base", base),
            ("operation", operation),
            ("bindings", bindings),
            ("_original_metadata", original_metadata),
            ("_output_metadata", output_metadata),
        ):
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class SkolemResult:
    """An atomic scoped candidate; named replacements change RDF identity."""

    source: PreparedSource | None
    mapping: SkolemMap | None
    report: Report
