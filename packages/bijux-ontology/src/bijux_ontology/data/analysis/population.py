"""One bounded deduplicated entity population over selected typed hierarchy edges."""

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from threading import Event
from types import MappingProxyType
from typing import Literal, TypeVar, cast
from weakref import ReferenceType, ref

from rdflib import Literal as RdfLiteral
from rdflib import URIRef
from rdflib.namespace import OWL

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Edge
from bijux_ontology.vocabulary.relation_paths.automaton import Automaton, named
from bijux_ontology.vocabulary.relation_paths.edges import (
    _edge,
    _Inventory,
    _projection,
)
from bijux_ontology.vocabulary.relation_paths.types import RelationInverse, RelationStep
from bijux_ontology.vocabulary.restrictions.types import RestrictionProjection

from .types import (
    AnnotationPopulation,
    EntityAnnotation,
    PopulationIdentity,
    PopulationRead,
)

_T = TypeVar("_T")
_S = TypeVar("_S", bound=str)


def reserve(budget: Budget, count: int = 1) -> None:
    """Prepay retained containers, references and their traversal."""
    budget.consume("work", count)
    budget.consume("states", count)
    budget.consume("bytes", 96 * count)


def capture(values: Sequence[_T], kind: type[_T], budget: Budget) -> tuple[_T, ...]:
    """Capture a finite typed inventory without trusting caller mutation."""
    return capture_items(values, kind, budget)


def text(value: str, budget: Budget) -> str:
    """Require exact ordinary application text before hashing or comparison."""
    if type(value) is not str:
        raise InputError("Annotation identities require ordinary text")
    bound_text(value, budget)
    if not value:
        raise InputError("Annotation identities require nonempty text")
    return value


def ordered(values: set[_S] | frozenset[_S], budget: Budget) -> tuple[_S, ...]:
    """Charge comparison work before deterministic ordering of qualified scalars."""
    reserve(budget, len(values))
    size = sum(len(value) for value in values)
    budget.consume("work", (size + 1) * max(1, len(values).bit_length()))
    return tuple(sorted(values))


def fingerprint(rows: Sequence[Sequence[str]], budget: Budget) -> str:
    """Length-delimited incremental hashing avoids ambiguous joins and bulk JSON."""
    digest = hashlib.sha256()
    for row in rows:
        budget.consume("work")
        digest.update(b"[")
        for value in row:
            bound_text(value, budget)
            budget.consume("bytes", 4 * len(value) + 32)
            payload = value.encode("utf-8")
            digest.update(str(len(payload)).encode("ascii"))
            digest.update(b":")
            digest.update(payload)
        digest.update(b"]")
    reserve(budget)
    return digest.hexdigest()


def _qualify_evidence(
    population: AnnotationPopulation, budget: Budget, issues: IssueCollector
) -> None:
    """Qualify immutable retained evidence once when admitting its read boundary."""
    if type(population) is not AnnotationPopulation or not population.report.verified:
        raise InputError("Analysis requires an owned qualified annotation population")
    reserve(budget)
    for value in vars(population.identity).values():
        text(value, budget)
    for entity in population.background:
        text(entity, budget)
        reserve(budget)
    for term in population.terms:
        named(term, budget)
        reserve(
            budget, 2 + len(population.ancestors[term]) + len(population.members[term])
        )
        for ancestor in population.ancestors[term]:
            named(ancestor, budget)
        for entity in population.members[term]:
            text(entity, budget)
    for row in population.annotations:
        text(row.entity, budget)
        named(row.term, budget)
        reserve(budget)
    for witness in population.witnesses:
        named(witness.source, budget)
        named(witness.target, budget)
        named(witness.relation.predicate, budget)
        text(witness.relation.role, budget)
        if witness.relation.role == "existential":
            _projection(cast(RestrictionProjection, witness.evidence), budget)
        else:
            _edge(cast(Edge, witness.evidence), budget)
        reserve(budget)
    retain_report(population.report, budget, issues)


@dataclass(frozen=True)
class _PopulationAdmission:
    """Exact owned field identities and a prepaid retained-evidence footprint."""

    owner: ReferenceType[AnnotationPopulation]
    fields: tuple[tuple[str, object], ...]
    identity_values: tuple[str, ...]
    states: int
    bytes: int
    scalar_bytes: int


def _scalars(population: AnnotationPopulation) -> Iterator[str]:
    """Visit already qualified scalar ownership once, not once per pair query."""
    yield from vars(population.identity).values()
    yield from population.background
    yield from population.terms
    for row in population.annotations:
        yield row.entity
        yield row.term
    for witness in population.witnesses:
        yield witness.source
        yield witness.target
        yield witness.relation.predicate
        yield witness.relation.role
        evidence = witness.evidence
        if isinstance(evidence, RestrictionProjection):
            for value in (
                evidence.owner,
                evidence.authored_owner,
                evidence.expression,
                evidence.property,
                evidence.quantifier,
                evidence.filler,
            ):
                if value is not None:
                    yield cast(str, value)
            yield evidence.status
            edges = (evidence.axiom, *evidence.inheritance, *evidence.structure)
        else:
            edges = (evidence,)
        for edge in edges:
            yield edge.scope
            for node in (edge.subject, edge.predicate, edge.object):
                yield cast(str, node)
                if isinstance(node, RdfLiteral):
                    yield node.datatype or ""
                    yield node.language or ""


def _admit_population(population: AnnotationPopulation, budget: Budget) -> None:
    """Seal actual prepared evidence and its smaller-reader admission capacity."""
    before_states, before_bytes = budget.remaining("states"), budget.remaining("bytes")
    _qualify_evidence(population, budget, IssueCollector(budget))
    states = before_states - budget.remaining("states")
    size = before_bytes - budget.remaining("bytes")
    maximum = 0
    for scalar in _scalars(population):
        before = budget.remaining("bytes")
        bound_text(scalar, budget)
        maximum = max(maximum, before - budget.remaining("bytes"))
    reserve(budget, 16)
    admission = _PopulationAdmission(
        ref(population),
        tuple(vars(population).items()),
        tuple(vars(population.identity).values()),
        states,
        size,
        maximum,
    )
    object.__setattr__(population, "_admission", admission)


def qualify(
    population: AnnotationPopulation, budget: Budget, issues: IssueCollector
) -> None:
    """Admit owned immutable evidence in constant work, charging retained capacity.

    Every read pays the complete retained states/byte footprint under its own
    allowance, then pays actual accessed sets. Scalar-size limits remain required.
    No process-global cache, caller-built object or altered identity authorises a read.
    """
    admission = getattr(population, "_admission", None)
    reserve(budget)
    if (
        type(population) is not AnnotationPopulation
        or not isinstance(admission, _PopulationAdmission)
        or admission.owner() is not population
        or any(
            getattr(population, name, None) is not value
            for name, value in admission.fields
        )
        or tuple(vars(population.identity).values()) != admission.identity_values
    ):
        raise InputError("Analysis requires an unchanged owned annotation population")
    budget.consume("work", len(admission.fields) + len(admission.identity_values))
    if admission.scalar_bytes > budget.limits.max_bytes:
        raise LimitExceeded("Annotation population scalars exceed max_bytes")
    budget.consume("states", admission.states)
    budget.consume("bytes", admission.bytes)
    for issue in population.report.issues:
        issues.append(issue)


def prepare_annotation_population(
    view: VocabularyView,
    annotations: Sequence[EntityAnnotation],
    *,
    terms: Sequence[URIRef],
    background: Sequence[str],
    propagation: Sequence[RelationStep | RelationInverse],
    corpus_id: str,
    annotation_snapshot_id: str,
    hierarchy_regime: str = "asserted",
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> PopulationRead:
    """Qualify a supplied finite positive corpus without shrinking its universe.

    The chosen typed edge direction is child-to-parent. Counts concern distinct
    eligible entities under reflexive selected ancestors, never OWL negation or
    completeness of biological annotation. Unannotated eligible entities stay in N.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    reports: list[Report] = []
    unknown: list[EntityAnnotation] = []
    unannotated: tuple[str, ...] = ()
    values: dict[str, object] = {}
    complete = False
    try:
        if not isinstance(view, VocabularyView):
            raise InputError(
                "Annotation preparation requires a selected VocabularyView"
            )
        text(corpus_id, operation)
        text(annotation_snapshot_id, operation)
        text(hierarchy_regime, operation)
        if hierarchy_regime not in ("asserted", "caller-inferred"):
            raise InputError("Declare asserted or caller-inferred hierarchy provenance")
        captured_terms = capture(terms, URIRef, operation)
        captured_background = capture(background, str, operation)
        rows = capture(annotations, EntityAnnotation, operation)
        specs = capture(propagation, object, operation)
        if not captured_terms or not captured_background or not specs:
            raise InputError(
                "Select nonempty terms, eligible background and propagation"
            )
        selected = set()
        for term in captured_terms:
            named(term, operation)
            reserve(operation)
            selected.add(term)
            operation.consume("work", len(term) + 1)
            if view.lookup(str(term)).status == "absent":
                issues.append(
                    Issue(
                        "analysis.term_unknown",
                        "Selected term is absent from the owned vocabulary",
                        str(term),
                        kind="unverified",
                    )
                )
        eligible = set()
        for entity in captured_background:
            text(entity, operation)
            reserve(operation)
            eligible.add(entity)
        term_order = ordered(selected, operation)
        entity_order = ordered(eligible, operation)
        oriented = []
        for spec in specs:
            if not isinstance(spec, RelationStep | RelationInverse):
                raise InputError("Population propagation requires typed relation steps")
            inverse = isinstance(spec, RelationInverse)
            step = spec.path if isinstance(spec, RelationInverse) else spec
            if not isinstance(step, RelationStep):
                raise InputError(
                    "Population propagation requires a step or its explicit inverse"
                )
            Automaton(spec, operation)
            reserve(operation)
            oriented.append((step, inverse))
        memberships = set()
        annotated = set()
        for index, row in enumerate(rows):
            text(row.entity, operation)
            if type(row.term) is not URIRef:
                raise InputError("Annotation terms require named URIRef identities")
            named(row.term, operation)
            reserve(operation, 2)
            if row.entity not in eligible:
                raise InputError(
                    "Annotation entity lies outside the declared background"
                )
            if row.term not in selected:
                unknown.append(row)
                issues.append(
                    Issue(
                        "analysis.annotation_unknown",
                        "Annotation term lies outside the selected hierarchy",
                        str(index),
                        kind="unverified",
                    )
                )
            else:
                memberships.add((row.entity, row.term))
                annotated.add(row.entity)
        reserve(operation, len(eligible))
        unannotated = ordered(eligible - annotated, operation)
        inventory = _Inventory(view, operation, issues, reports)
        reserve(operation, len(selected) * 2)
        parents: dict[URIRef, set[URIRef]] = {term: set() for term in term_order}
        witnesses = []
        inverse_coverage: set[str] = set()
        for term in term_order:
            for step, inverse in oriented:
                for witness in inventory.neighbours(term, step, inverse):
                    reserve(operation, 2)
                    witnesses.append(witness)
                    if witness.target not in selected:
                        issues.append(
                            Issue(
                                "analysis.parent_unknown",
                                "Required parent lies outside the declared hierarchy",
                                str(witness.target),
                                kind="unverified",
                            )
                        )
                    else:
                        parents[term].add(witness.target)
                if step.role == "existential":
                    coverage: tuple[Sequence[RestrictionProjection], ...]
                    if inverse:
                        if step.predicate in inverse_coverage:
                            continue
                        inventory.inverse_projections()
                        inverse_coverage.add(step.predicate)
                        coverage = (
                            inventory.unsupported.get(URIRef(step.predicate), ()),
                        )
                    else:
                        coverage = (inventory.projections(term),)
                    for records in coverage:
                        for record in records:
                            operation.consume("work")
                            if record.property == URIRef(step.predicate) and (
                                record.status != "supported"
                                or record.quantifier != OWL.someValuesFrom
                                or not isinstance(record.filler, URIRef)
                            ):
                                issues.append(
                                    Issue(
                                        "analysis.projection_unknown",
                                        "Required projection is outside the supported "
                                        "named existential profile",
                                        str(record.owner),
                                        kind="unverified",
                                    )
                                )
        reserve(operation, len(selected) + sum(map(len, parents.values())))
        order = tuple(TopologicalSorter(parents).static_order())
        ancestors: dict[URIRef, frozenset[URIRef]] = {}
        for term in order:
            reserve(operation)
            closure = {term}
            for parent in parents[term]:
                reserve(operation, len(ancestors[parent]))
                closure.update(ancestors[parent])
            reserve(operation, len(closure))
            ancestors[term] = frozenset(closure)
        reserve(operation, len(selected))
        members: dict[URIRef, set[str]] = {term: set() for term in term_order}
        for entity, term in memberships:
            for ancestor in ancestors[term]:
                operation.consume("work")
                if entity not in members[ancestor]:
                    reserve(operation)
                    members[ancestor].add(entity)
        reserve(operation, len(memberships))
        operation.consume(
            "work",
            (sum(len(e) + len(t) for e, t in memberships) + 1)
            * max(1, len(memberships).bit_length()),
        )
        annotation_rows = tuple(sorted(memberships))
        hierarchy_rows: list[tuple[str, ...]] = []
        for scope, graph in view._scopes:
            text(scope, operation)
            bound_node(graph.identifier, operation)
            reserve(operation)
            hierarchy_rows.append(
                (
                    "scope",
                    scope,
                    *tuple(part or "" for part in term_key(graph.identifier)),
                )
            )
        for term in term_order:
            reserve(operation)
            hierarchy_rows.append(("term", str(term)))
        for step, inverse in oriented:
            reserve(operation)
            hierarchy_rows.append(("relation", step.predicate, step.role, str(inverse)))
        for witness in witnesses:
            evidence = witness.evidence
            edges = (
                (evidence.axiom, *evidence.inheritance, *evidence.structure)
                if isinstance(evidence, RestrictionProjection)
                else (evidence,)
            )
            for edge in edges:
                reserve(operation)
                hierarchy_rows.append(
                    (
                        "edge",
                        str(witness.source),
                        str(witness.target),
                        witness.relation.predicate,
                        witness.relation.role,
                        str(witness.inverse),
                        edge.scope,
                        *(
                            part or ""
                            for node in (edge.subject, edge.predicate, edge.object)
                            for part in term_key(node)
                        ),
                    )
                )
        reserve(operation, len(hierarchy_rows))
        operation.consume(
            "work",
            (sum(sum(map(len, row)) for row in hierarchy_rows) + 1)
            * max(1, len(hierarchy_rows).bit_length()),
        )
        identity = PopulationIdentity(
            corpus_id,
            annotation_snapshot_id,
            fingerprint(tuple((e,) for e in entity_order), operation),
            fingerprint(annotation_rows, operation),
            fingerprint(tuple(sorted(set(hierarchy_rows))), operation),
            cast(Literal["asserted", "caller-inferred"], hierarchy_regime),
        )
        reserve(operation, sum(map(len, members.values())) + len(selected) * 2)
        values = {
            "identity": identity,
            "terms": term_order,
            "background": entity_order,
            "annotations": rows,
            "duplicate_annotations": len(rows) - len(memberships),
            "witnesses": tuple(witnesses),
            "ancestors": MappingProxyType(ancestors),
            "members": MappingProxyType(
                {term: frozenset(items) for term, items in members.items()}
            ),
            "unannotated": unannotated,
        }
        complete = inventory.complete
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (CycleError, InputError) as error:
        try:
            issues.append(
                Issue(
                    "analysis.hierarchy_cycle"
                    if isinstance(error, CycleError)
                    else "analysis.input",
                    "Selected analytical hierarchy is cyclic"
                    if isinstance(error, CycleError)
                    else str(error),
                    kind="unverified"
                    if isinstance(error, CycleError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))

    report = Report(
        "analysis.population",
        checked=int(complete),
        requested=1,
        complete=complete,
        issues=tuple(issues),
        children=tuple(reports),
    )
    population = (
        AnnotationPopulation._owned(**values, report=report)
        if report.verified
        else None
    )
    if population is not None:
        try:
            _admit_population(population, operation)
        except LimitExceeded as error:
            issues.exhausted(str(error))
            population = None
            report = Report(
                "analysis.population",
                requested=1,
                complete=False,
                issues=tuple(issues),
                children=tuple(reports),
            )
    return PopulationRead(population, report, tuple(unknown), unannotated)
