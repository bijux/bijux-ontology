"""Bounded selected RDF input; trusted construction follows all required checks."""

from collections.abc import Callable, Sequence
from threading import Event
from typing import TypeVar

from rdflib import BNode, Graph, URIRef
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.models.emission.matching import identity_matches
from bijux_ontology.models.emission.rdf import _traits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.path_traversal import (
    PathIdentityUnavailable,
    finite_path_steps,
    path_values,
)
from bijux_ontology.rdf.paths import capture_path
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.structure import iter_rdf_list
from bijux_ontology.rdf.terms import bound_node, bound_text, term_key
from bijux_ontology.reports import Issue, Report

from .decoding import decode_term
from .types import (
    FieldState,
    ProjectedField,
    ProjectedRecord,
    ProjectionRead,
    ReadBinding,
    ReadField,
    ReferenceHandle,
)

_T = TypeVar("_T")


def _reserve(operation: Budget, count: int = 1) -> None:
    """Prepay immutable result/reference storage before retained allocation."""
    operation.consume("states", count)
    operation.consume("bytes", 64 * count)


def _field(
    field: ReadField,
    subject: URIRef | BNode,
    graph: Graph,
    source: PreparedSource,
    graph_name: URIRef | BNode | None,
    selected: set[tuple[str | None, ...]],
    operation: Budget,
    issues: IssueCollector,
    where: str,
) -> ProjectedField:
    """Return exact field evidence; failed conversion never chooses a value."""
    for value in (field.name, field.mode, field.decoder, field.references):
        bound_text(value, operation)
    for count in (
        field.minimum,
        field.maximum,
        field.minimum_members,
        field.maximum_members,
    ):
        if count is not None:
            bound_text(str(count), operation)
    captured = capture_path(field.path, budget=operation)
    steps = finite_path_steps(captured, operation)
    if steps is None:
        issues.append(
            Issue(
                "projection.path_unverified",
                "Path is outside the finite sequence/inverse profile",
                where,
                kind="unverified",
            )
        )
        return ProjectedField(field.name, "unverified", (), (), ())
    try:
        endpoints = path_values(
            graph,
            subject,
            steps,
            operation,
            literal_identity=source.literal_fidelity
            in ("source_terms", "supplied_terms"),
        )
    except PathIdentityUnavailable:
        issues.append(
            Issue(
                "projection.term_unverified",
                "Inverse path requires exact literal join identity",
                where,
                kind="unverified",
            )
        )
        return ProjectedField(field.name, "unverified", (), (), ())
    count = len(endpoints)
    if field.mode != "many" and count > 1:
        issues.append(
            Issue(
                "projection.ambiguous",
                "Scalar/list field has multiple distinct RDF endpoints",
                where,
            )
        )
        return ProjectedField(field.name, "ambiguous", endpoints, endpoints, ())
    if count < field.minimum or field.maximum is not None and count > field.maximum:
        issues.append(
            Issue(
                "projection.cardinality",
                "RDF endpoint count violates the read contract",
                where,
            )
        )
        return ProjectedField(
            field.name, "missing" if not count else "invalid", endpoints, endpoints, ()
        )
    if not count:
        return ProjectedField(field.name, "missing", (), (), ())
    terms: list[Node] = []
    state: FieldState = "raw" if field.decoder == "raw" else "decoded"
    if field.mode == "list":
        try:
            for term in iter_rdf_list(
                graph,
                endpoints[0],
                max_items=operation.limits.max_states,
                budget=operation,
            ):
                operation.consume("triples", 2)
                bound_node(term, operation)
                _reserve(operation)
                terms.append(term)
        except InputError:
            issues.append(
                Issue(
                    "projection.list_invalid",
                    "RDF list is malformed, branched or cyclic",
                    where,
                )
            )
            return ProjectedField(field.name, "invalid", endpoints, tuple(terms), ())
        count = len(terms)
        if (
            count < field.minimum_members
            or field.maximum_members is not None
            and count > field.maximum_members
        ):
            issues.append(
                Issue(
                    "projection.cardinality",
                    "RDF list member count violates the read contract",
                    where,
                )
            )
            return ProjectedField(field.name, "invalid", endpoints, tuple(terms), ())
    else:
        _reserve(operation, 3 * len(endpoints))
        size = sum(
            sum(len(part or "") for part in term_key(term)) for term in endpoints
        )
        operation.consume(
            "work", (size + len(endpoints)) * max(1, len(endpoints).bit_length())
        )
        terms.extend(
            sorted(
                endpoints, key=lambda term: tuple(part or "" for part in term_key(term))
            )
        )
    values = []
    for index, term in enumerate(terms):
        _reserve(operation)
        operation.consume("bytes", len(where) + 32)
        decoded, status = decode_term(
            term,
            field,
            source=source,
            graph=graph_name,
            selected=selected,
            budget=operation,
            issues=issues,
            where=where + "/" + str(index),
        )
        values.append(decoded)
        if status in ("invalid", "unverified"):
            state = status if state != "invalid" else state
    _reserve(operation, len(terms) + len(values) + 1)
    return ProjectedField(field.name, state, endpoints, tuple(terms), tuple(values))


def project_records(
    source: PreparedSource | Graph,
    bindings: Sequence[ReadBinding],
    *,
    graph_name: GraphSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> ProjectionRead:
    """Project explicitly bound records in one physical graph, retaining RDF terms.

    An ordinary Graph uses its existing default selection; datasets require a
    concrete graph or a pinned source. Unions are unsupported. No inference,
    normalization, automatic child selection or serializer reversal occurs.
    Raw retention is the default and makes no datatype-validity claim. String
    conversion retains lexical text; language metadata remains on the term.
    Integer, decimal and boolean conversions use narrow checked XSD families.
    Exact source terms remain separate from their interpreted Python values.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    records: list[ProjectedRecord] = []
    complete = False
    requested = 1
    try:
        if graph_name is not None and type(graph_name) not in (str, URIRef, BNode):
            raise InputError(
                "Graph selection requires explicit IRI text or owned blank graph"
            )
        if not isinstance(source, PreparedSource | Graph):
            raise InputError("Projection requires owned RDF or an ordinary Graph")
        prepared = (
            source
            if isinstance(source, PreparedSource)
            else prepare_rdf(source, limits=operation.limits, budget=operation)
        )
        for value in (prepared.literal_fidelity, prepared.graph_fidelity):
            bound_text(value, operation)
        if prepared.union:
            issues.append(
                Issue(
                    "projection.graph_unverified",
                    "A union does not retain one physical reference graph",
                    kind="unverified",
                )
            )
        else:
            if prepared.graph_fidelity != "complete":
                issues.append(
                    Issue(
                        "projection.graph_unverified",
                        "Complete graph identity was not retained",
                        kind="unverified",
                    )
                )
            if isinstance(graph_name, BNode) and not any(
                name is graph_name for name in prepared._graph_names(budget=operation)
            ):
                raise InputError(
                    "Blank graph selection requires exact snapshot ownership"
                )
            contexts = prepared._contexts(graph_name, budget=operation)
            _, name, graph = next(contexts)
            if next(contexts, None) is not None:
                raise InputError("Projection requires one physical graph")
            operation.consume("triples", len(graph))
            present: set[Node] = set()
            blanks: set[int] = set()
            for s, p, o in graph:
                operation.consume("work")
                _reserve(operation, 3)
                for term in (s, p, o):
                    bound_node(term, operation)
                present.update((s, o))
                blanks.update(id(term) for term in (s, o) if isinstance(term, BNode))
            captured = capture_items(bindings, ReadBinding, operation)
            requested = max(1, len(captured))
            if not captured:
                raise InputError(
                    "Projection requires explicit nonempty record bindings"
                )
            selected: set[tuple[str | None, ...]] = set()
            names: set[str] = set()
            for binding in captured:
                bound_text(binding.name, operation)
                if not binding.name.strip() or binding.name in names:
                    raise InputError("Read binding names must be nonempty and distinct")
                bound_node(binding.subject, operation)
                if (
                    isinstance(binding.subject, BNode)
                    and id(binding.subject) not in blanks
                ):
                    raise InputError(
                        "Blank subject requires exact selected snapshot ownership"
                    )
                _reserve(operation, 2)
                names.add(binding.name)
                key = term_key(binding.subject)
                operation.consume("work", sum(len(part or "") for part in key) + 1)
                selected.add(key)
            for binding in captured:
                where = binding.name
                _reserve(operation, len(graph))
                traits = _traits(graph, binding.subject, operation)
                if binding.identity.expected_iri is not None:
                    bound_text(binding.identity.expected_iri, operation)
                if not identity_matches(binding.identity, traits):
                    issues.append(
                        Issue(
                            "projection.identity",
                            "Subject identity violates the read contract",
                            where,
                        )
                    )
                if binding.subject not in present:
                    issues.append(
                        Issue(
                            "projection.subject_unverified",
                            "Selected subject has no occurrence in this graph",
                            where,
                            kind="unverified",
                        )
                    )
                for required in binding.types:
                    if type(required) not in (str, URIRef):
                        raise InputError("Required RDF types must be explicit IRI text")
                    bound_text(required, operation)
                    absolute_iri(required)
                    if required not in traits.types:
                        issues.append(
                            Issue(
                                "projection.type_missing",
                                "Selected record lacks an explicitly required RDF type",
                                where,
                            )
                        )
                fields = []
                field_names: set[str] = set()
                _reserve(operation, 2 * len(binding.fields) + 1)
                for field in binding.fields:
                    bound_text(field.name, operation)
                    if field.name in field_names:
                        raise InputError(
                            "Read field names must be distinct in a record"
                        )
                    field_names.add(field.name)
                    operation.consume("bytes", 4 * (len(where) + len(field.name) + 1))
                    location = where + "/" + field.name
                    fields.append(
                        _field(
                            field,
                            binding.subject,
                            graph,
                            prepared,
                            name,
                            selected,
                            operation,
                            issues,
                            location,
                        )
                    )
                records.append(
                    ProjectedRecord(
                        binding.name,
                        ReferenceHandle(prepared, name, binding.subject),
                        tuple(fields),
                    )
                )
            operation.consume("work", 0)
            complete = True
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue("projection.invalid_input", str(error), kind="input_error")
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return ProjectionRead._owned(
        tuple(records),
        Report(
            "data.projection",
            len(records),
            tuple(issues),
            requested=requested,
            complete=complete,
            regime="explicit-selected-rdf-read",
        ),
    )


def construct_projected(
    projection: ProjectionRead, factory: Callable[[tuple[ProjectedRecord, ...]], _T]
) -> _T:
    """Invoke one trusted caller callback only after all required checks succeed.

    Callback runtime, side effects and output belong to the caller. This function
    makes no preemption, output-immutability or serializer-reversibility promise.
    """
    if not isinstance(projection, ProjectionRead) or not projection.report.verified:
        raise InputError("Construction requires a complete verified owned projection")
    if not callable(factory):
        raise InputError("Construction requires an explicitly trusted callable")
    return factory(projection.records)
