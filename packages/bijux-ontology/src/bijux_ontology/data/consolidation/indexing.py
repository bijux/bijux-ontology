"""Bounded corpus capture using existing authorized RDF snapshot witnesses."""

from collections.abc import Sequence

from rdflib import BNode, URIRef

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.patches.planning import Captured, capture_source
from bijux_ontology.rdf.patches.types import (
    PatchBlank,
    PatchResource,
    PatchUnavailable,
    SnapshotWitness,
    bound_term,
    validate_witness,
)
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.reports import Issue, Report

from .types import (
    CorpusContribution,
    CorpusDocument,
    CorpusIndex,
    CorpusOrigin,
    CorpusSnapshot,
    EntityDescription,
)


def operation_for(limits: Limits | None, budget: Budget | None) -> Budget:
    """Reuse one caller operation without resetting its finite allowances."""
    if (
        budget is not None
        and not isinstance(budget, Budget)
        or limits is not None
        and not isinstance(limits, Limits)
    ):
        raise InputError("Expected explicit corpus Limits/Budget")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Corpus limits conflict with the shared budget")
    return budget if budget is not None else Budget(limits)


def qualify_snapshot(snapshot: CorpusSnapshot, budget: Budget) -> None:
    """Qualify all public snapshot metadata before grouping or materialization."""
    if not isinstance(snapshot, CorpusSnapshot) or not isinstance(
        snapshot.origin, CorpusOrigin
    ):
        raise InputError("Expected owned corpus snapshots and origins")
    origin = snapshot.origin
    bound_text(origin.identifier, budget)
    if not origin.identifier:
        raise InputError("Corpus document identifiers must be nonempty")
    for text in (origin.document_iri, origin.effective_url):
        if text is not None:
            bound_text(text, budget)
            absolute_iri(text)
    bound_text(snapshot.literal_fidelity, budget)
    if snapshot.literal_fidelity not in ("source_terms", "supplied_terms"):
        raise PatchUnavailable("Corpus consolidation requires exact source terms")
    validate_witness(snapshot.witness, budget)


def _narrow(
    document: CorpusDocument, captured: Captured, budget: Budget
) -> SnapshotWitness:
    """Select only exact authorized physical contexts, preserving empty names."""
    witness = captured.witness
    if document.graphs is None:
        return witness
    if type(document.graphs) is not tuple:
        raise InputError("Corpus graph selections require immutable tuples")
    budget.consume("states", len(document.graphs))
    budget.consume("bytes", 64 + 16 * len(document.graphs))
    selected: set[PatchResource | None] = set()
    budget.consume("states", len(witness.graphs))
    budget.consume("bytes", 64 + 16 * len(witness.graphs))
    available: set[PatchResource | None] = set(witness.graphs)
    if witness.kind != "named":
        available.add(None)
    for name in document.graphs:
        budget.consume("work")
        target: PatchResource | None = None
        if name is not None:
            if not isinstance(name, URIRef | BNode):
                raise InputError("Corpus graph selections require RDF resource terms")
            bound_node(name, budget)
            target = captured.reference(name) if isinstance(name, BNode) else name
        if target not in available:
            raise PatchUnavailable(
                "A selected corpus graph is outside its authorized source scope"
            )
        budget.consume("states")
        selected.add(target)
    budget.consume("work", len(witness.quads) + len(witness.graphs))
    budget.consume("states", len(witness.quads) + len(witness.graphs))
    budget.consume("bytes", 16 * (len(witness.quads) + len(witness.graphs)))
    quads = tuple(quad for quad in witness.quads if quad.graph in selected)
    graphs = tuple(graph for graph in witness.graphs if graph in selected)
    if witness.kind == "named" and witness.graph_selection not in selected:
        raise PatchUnavailable(
            "Selected corpus graphs omit the authorized named source"
        )
    return SnapshotWitness(witness.kind, witness.graph_selection, quads, graphs)


def describe_snapshots(
    snapshots: tuple[CorpusSnapshot, ...], budget: Budget, *, union: bool = False
) -> tuple[EntityDescription, ...]:
    """Group existing exact IRI identities; source-local blanks never cross-join."""
    groups: dict[
        tuple[PatchResource, PatchResource | None, str | None], list[CorpusContribution]
    ] = {}
    for snapshot in snapshots:
        qualify_snapshot(snapshot, budget)
        for quad in snapshot.witness.quads:
            budget.consume("work")
            bound_term(quad.subject, budget)
            if quad.graph is not None:
                bound_term(quad.graph, budget)
            key = (
                quad.subject,
                None if union else quad.graph,
                snapshot.origin.identifier
                if isinstance(quad.subject, PatchBlank)
                or not union
                and isinstance(quad.graph, PatchBlank)
                else None,
            )
            if key not in groups:
                budget.consume("states")
                budget.consume("bytes", 128)
                groups[key] = []
            budget.consume("states")
            budget.consume("bytes", 64)
            groups[key].append(CorpusContribution(snapshot.origin, quad))
    budget.consume("states", len(groups))
    budget.consume(
        "bytes", 64 * len(groups) + 16 * sum(len(values) for values in groups.values())
    )
    return tuple(
        EntityDescription(subject, graph, tuple(values))
        for (subject, graph, _), values in groups.items()
    )


def prepare_corpus(
    documents: Sequence[CorpusDocument],
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> CorpusIndex:
    """Capture exact supplied document/graph participation with no resource loading.

    Established IRIs already reflect the caller's preparation bases. Captures
    own literal fields and document-local blank tokens; subsequent caller graph,
    literal-metadata or PreparedSource metadata edits cannot change the index.
    Partial captures retain their report and cannot publish a consolidation.
    """
    operation = operation_for(limits, budget)
    issues = IssueCollector(operation)
    snapshots: list[CorpusSnapshot] = []
    entities: tuple[EntityDescription, ...] = ()
    count = 1
    try:
        selected = capture_items(documents, CorpusDocument, operation)
        count = len(selected)
        names: set[str] = set()
        for index, document in enumerate(selected):
            bound_text(document.identifier, operation)
            if not document.identifier or document.identifier in names:
                raise InputError(
                    "Corpus document identifiers must be distinct and nonempty"
                )
            operation.consume("states")
            names.add(document.identifier)
            source = document.source
            for text in (
                source.document_iri,
                source.effective_url,
                source.literal_fidelity,
                source.graph_fidelity,
                source.graph_name,
            ):
                if text is not None:
                    bound_text(text, operation)
            captured = capture_source(source, f"document{index}:", operation)
            origin = CorpusOrigin(
                str(document.identifier),
                str(source.document_iri) if source.document_iri is not None else None,
                str(source.effective_url) if source.effective_url is not None else None,
            )
            snapshot = CorpusSnapshot(
                origin,
                _narrow(document, captured, operation),
                str(source.literal_fidelity),
            )
            qualify_snapshot(snapshot, operation)
            operation.consume("states")
            operation.consume("bytes", 128)
            snapshots.append(snapshot)
        entities = describe_snapshots(tuple(snapshots), operation)
    except LimitExceeded:
        issues.exhausted("Corpus indexing exceeded its limits")
    except PatchUnavailable:
        try:
            issues.append(
                Issue(
                    "data.consolidation_unverified",
                    "Corpus input lacks exact authorized source scope",
                    kind="unverified",
                )
            )
        except LimitExceeded:
            issues.exhausted("Corpus indexing diagnostics exceeded their limits")
    except InputError:
        try:
            issues.append(
                Issue(
                    "data.consolidation_input_error",
                    "Invalid corpus document participation",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Corpus indexing diagnostics exceeded their limits")
    return CorpusIndex(
        tuple(snapshots),
        entities,
        Report(
            "corpus-index",
            len(snapshots),
            tuple(issues),
            requested=max(1, count),
            complete=len(snapshots) == count and bool(count),
        ),
    )
