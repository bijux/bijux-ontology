"""Checked OBOGraphs projection with complete selected assertion accounting."""

import json

from rdflib import BNode, URIRef

from bijux_ontology._json import owned_json
from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.annotations.graphs.reading import _physical
from bijux_ontology.vocabulary.annotations.graphs.types import AnnotationStatement

from .projection import (
    ExportProjection,
    UnsupportedProjection,
    literals,
    named,
    statement,
)
from .reading import _operation, _validate
from .types import OboGraphsExport


def write_obo_graphs(
    source: PreparedSource,
    *,
    graph_id: str,
    graph: URIRef | BNode | None | Absent = ABSENT,
    literal_predicates: tuple[URIRef, ...] = (),
    allow_lossy: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> OboGraphsExport:
    """Project one physical graph; omitted source assertions always remain visible.

    The claimed scope is supported axiom/annotation meaning rather than exact RDF
    term serialization. Unsupported axioms block output by default. Explicit
    lossy output retains its loss findings and never becomes verified preservation.
    Named nodes require their actual selected declarations; ontology identity is
    the selected graph_id's declaration, independent of the dataset graph name.
    """
    owned = _operation(limits, budget)
    issues = IssueCollector(owned)
    omitted: tuple[AnnotationStatement, ...] = ()
    children: tuple[Report, ...] = ()
    output = None
    checked = 0
    try:
        if not isinstance(source, PreparedSource) or type(allow_lossy) is not bool:
            raise InputError(
                "Export requires an owned RDF source and explicit loss policy"
            )
        metadata = SnapshotMetadata.capture(source, owned)
        if (
            metadata.literal_fidelity not in {"source_terms", "supplied_terms"}
            or metadata.graph_fidelity != "complete"
        ):
            raise UnsupportedProjection(
                "Checked export requires exact source "
                "literals and complete graph inventory"
            )
        bound_text(graph_id, owned)
        identifier = named(graph_id)
        selected_literals = literals(literal_predicates, owned)
        physical, selected = _physical(source, graph, owned)
        triples = []
        for triple in selected:
            owned.consume("work")
            owned.consume("states", 4)
            owned.consume("bytes", 128)
            for term in triple:
                bound_node(term, owned)
            triples.append(triple)
        projection = ExportProjection(
            source, physical, selected, owned, issues, selected_literals
        )
        document = projection.convert(identifier)
        children = tuple(projection.reports)
        owned.consume("work", len(triples))
        owned.consume("states", 2 * len(triples))
        omitted = tuple(
            statement(subject, predicate, value)
            for subject, predicate, value in triples
            if (subject, predicate, value) not in projection.represented
        )
        if omitted:
            issues.append(
                Issue(
                    "adapter.obographs_projection_loss",
                    "Selected source assertions are not "
                    "represented by the supported OBOGraphs projection",
                )
            )
        if not omitted or allow_lossy:
            _validate(document, owned)
            _, size = owned_json(document, owned)
            owned.consume("work", 8 * size)
            owned.consume("bytes", 8 * size)
            encoded = json.dumps(
                document, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            if len(encoded) > owned.limits.max_bytes:
                raise LimitExceeded("Produced OBOGraphs document exceeds max_bytes")
            current = SnapshotMetadata.capture(source, owned)
            if not metadata.same_scope(current):
                raise InputError("Source metadata changed during projection")
            owned.consume("work", 0)
            output = encoded
        checked = 1
    except (ModuleNotFoundError, FileNotFoundError):
        try:
            issues.append(
                Issue(
                    "adapter.obographs_unverified",
                    "Install "
                    "bijux-ontology[jsonschema] with its bundled OBOGraphs schema",
                    kind="unverified",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining the unavailable native validator")
    except UnsupportedProjection as error:
        try:
            issues.append(
                Issue("adapter.obographs_unverified", str(error), kind="unverified")
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining unsupported projection evidence")
    except (LimitExceeded, MemoryError, RecursionError) as error:
        issues.exhausted(str(error))
    except (InputError, ValueError, TypeError, UnicodeError):
        try:
            issues.append(
                Issue(
                    "adapter.obographs_input_error",
                    "Selected source identity or "
                    "structural metadata cannot be projected",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining projection failure")
    report = Report(
        "adapter.obo_graphs_export",
        checked,
        tuple(issues),
        requested=1,
        complete=checked == 1,
        children=children,
        regime="obographs-selected-class-axiom-meaning",
    )
    available = not any(issue.kind in {"input_error", "exhausted"} for issue in report)
    return OboGraphsExport(
        output if available and (report.verified or allow_lossy) else None,
        omitted,
        report,
    )
