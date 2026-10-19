"""Reconstruct authored grouped RDF while retaining the complete original backing."""

from rdflib import BNode, Dataset
from rdflib.graph import DATASET_DEFAULT_GRAPH_ID
from rdflib.namespace import RDF
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.rdf.patches.applying import _materialize
from bijux_ontology.rdf.patches.planning import capture_source
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node

from .reading import reserve
from .types import XKOS as X
from .types import Correspondence, CorrespondenceExport, Statement


def qualify(table: Correspondence, budget: Budget) -> None:
    """Check original public scope receipts before adopting reusable interpretation."""
    if (
        type(table) is not Correspondence
        or table.source is None
        or table._metadata is None
    ):
        raise InputError("Use an owned correspondence with its original source")
    if not table._metadata.same_scope(SnapshotMetadata.capture(table.source, budget)):
        raise ResourceError(
            "Original correspondence scope/origin/fidelity metadata changed"
        )
    for release in table._releases:
        if not release.metadata.same_scope(
            SnapshotMetadata.capture(release.source, budget)
        ):
            raise ResourceError("A selected classification release scope changed")
    if not table.report.verified or len(table._releases) != 2:
        raise ResourceError("Correspondence interpretation is not completely qualified")


def statements(table: Correspondence, budget: Budget) -> set[Statement]:
    """Serialize intact typed groups without pairwise mapping assertions."""
    result: set[Statement] = set()

    def add(triple: Statement) -> None:
        """Bound every term and reference before native statement insertion."""
        budget.consume("work")
        for node in triple:
            bound_node(node, budget)
        reserve(budget)
        result.add(triple)

    for kind in table.types:
        add((table.identifier, RDF.type, kind))
    for scheme in table.compares:
        add((table.identifier, X.compares, scheme))
    for link in table.links:
        add((table.identifier, X.madeOf, link))
    for group in table.associations:
        for kind in group.types:
            add((group.identifier, RDF.type, kind))
        for category in group.sources:
            add((group.identifier, X.sourceConcept, category))
        for category in group.targets:
            add((group.identifier, X.targetConcept, category))
    return result


def write_correspondence(
    table: Correspondence, *, limits: Limits | None = None, budget: Budget | None = None
) -> CorrespondenceExport:
    """Render the typed selected model and every retained unmodeled original quad.

    Full backing graphs, empty graphs, original source metadata and pin remain
    intact. Native patch materialization supplies one fresh global blank mapping,
    including the actual fresh anonymous graph when the source was pinned to it.
    """
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("XKOS export requires Limits")
    current = operation(limits, budget)
    issues = IssueCollector(current)
    children: tuple[Report, ...] = ()
    result = None
    try:
        if type(table) is not Correspondence:
            raise InputError("Export requires an owned Correspondence")
        retain_report(table.report, current, issues)
        children = (table.report,)
        qualify(table, current)
        assert table.source is not None and table._metadata is not None
        modeled = statements(table, current)
        reserve(current, len(table.associations) + 1)
        roles: dict[Node, frozenset[Node]] = {
            table.identifier: frozenset((RDF.type, X.compares, X.madeOf))
        }
        for group in table.associations:
            roles[group.identifier] = roles.get(
                group.identifier, frozenset()
            ) | frozenset((RDF.type, X.sourceConcept, X.targetConcept))
        output = Dataset()
        for original in table.source._owned_contexts(current):
            current.consume("resources")
            name = (
                None
                if original.identifier == DATASET_DEFAULT_GRAPH_ID
                else original.identifier
            )
            selected = (
                name is table.graph
                if isinstance(name, BNode) or isinstance(table.graph, BNode)
                else name == table.graph
            )
            target = output.default_graph if name is None else output.graph(name)
            for triple in original:
                current.consume("work")
                current.consume("triples")
                for node in triple:
                    bound_node(node, current)
                reserve(current)
                if not selected or triple[1] not in roles.get(triple[0], frozenset()):
                    target.add(triple)
        selected_output = (
            output.default_graph if table.graph is None else output.graph(table.graph)
        )
        for triple in modeled:
            current.consume("work")
            current.consume("triples")
            reserve(current)
            selected_output.add(triple)
        metadata = table._metadata
        carrier = PreparedSource._from_owned(
            output,
            is_dataset=metadata.is_dataset,
            document_iri=metadata.document_iri,
            effective_url=metadata.effective_url,
            literal_fidelity=metadata.literal_fidelity,
            graph_fidelity="complete",
            max_triples=current.limits.max_triples,
        )
        captured = capture_source(carrier, "correspondence:", current)
        blanks: dict[str, BNode] = {}
        candidate = _materialize(
            captured.witness, table.source, current, bindings=blanks
        )
        pin = metadata.graph_name
        if isinstance(pin, BNode):
            pin = blanks[captured.reference(pin).token]
        candidate.graph_name = pin
        qualify(table, current)
        result = candidate
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "xkos.export_unverified"
                    if isinstance(error, ResourceError)
                    else "xkos.input_invalid",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return CorrespondenceExport(
        result,
        Report(
            "xkos.correspondence-export",
            int(result is not None),
            tuple(issues),
            requested=1,
            complete=result is not None,
            children=children,
            regime="source-retaining-grouped-rdf",
        ),
    )
