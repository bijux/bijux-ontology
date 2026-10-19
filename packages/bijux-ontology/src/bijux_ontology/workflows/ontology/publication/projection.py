"""Bounded SKOS dependency projection through ordinary checked migration plans."""

import json
import threading
import time
from collections import deque
from typing import Literal, cast

from rdflib import BNode, Dataset, Graph, URIRef
from rdflib.namespace import RDF, RDFS, SKOS
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.migrations.candidates import MigrationCandidate, MigrationCheck
from bijux_ontology.rdf.patches.correspondence import Operation, _json_bound
from bijux_ontology.rdf.patches.planning import capture_source, plan_rdf_patch
from bijux_ontology.rdf.patches.types import BlankNodePair, PatchUnavailable
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.rdf.transforms import Transformation, _bind
from bijux_ontology.reports import Issue, Report
from bijux_ontology.skos.structure import _MAPPINGS, _RELATIONS
from bijux_ontology.skos.subsets.selection import select_concepts
from bijux_ontology.skos.xl import ROLES, XL, describe_xl_label
from bijux_ontology.vocabulary.collections.expansion import expand_collection
from bijux_ontology.vocabulary.inspection import VocabularyView, inspect_vocabulary
from bijux_ontology.vocabulary.loading import load_vocabulary
from bijux_ontology.vocabulary.results import Edge
from bijux_ontology.workflows.upgrades.plans.execution import execute_migration_plan
from bijux_ontology.workflows.upgrades.plans.types import (
    MigrationPlanResult,
    MigrationStep,
)

from .types import SkosPublication, _iris

_METADATA = frozenset(
    (
        RDFS.label,
        RDFS.comment,
        SKOS.prefLabel,
        SKOS.altLabel,
        SKOS.hiddenLabel,
        SKOS.notation,
        SKOS.note,
        SKOS.changeNote,
        SKOS.definition,
        SKOS.editorialNote,
        SKOS.example,
        SKOS.historyNote,
        SKOS.scopeNote,
    )
)
_Mode = Literal["owner", "label", "collection", "list", "blank"]


def _remaining(operation: Operation) -> float:
    """Use one absolute operation deadline for each maintained child worker."""
    operation.consume("work", 0)
    remaining = operation.deadline - time.monotonic()
    if remaining <= 0:
        raise LimitExceeded("Publication exceeded its wall-clock deadline")
    return remaining


def _scope(source: PreparedSource) -> Report:
    """Qualify the finite physical input profile without inventing a graph union."""
    supported = (
        not source.union
        and not isinstance(source.graph_name, BNode)
        and (not source.is_dataset or source.graph_name is not None)
    )
    return Report(
        "skos.publication_scope",
        int(supported),
        ()
        if supported
        else (
            Issue(
                "skos.publication.scope_unverified",
                "Publication requires a plain graph or explicit single graph selection",
                kind="unverified",
            ),
        ),
        requested=1,
        complete=supported,
    )


class _Projection:
    """One operation-local dependency worklist over the existing owned view."""

    def __init__(
        self, source: PreparedSource, specification: SkosPublication, budget: Operation
    ) -> None:
        self.source = source
        self.specification = specification
        self.budget = budget
        self.issues = IssueCollector(budget)
        self.children: list[Report] = []
        self.exclusions: list[str] = []
        self.view: VocabularyView
        budget.consume("states", 2)
        budget.consume("bytes", 256)
        self.graph = Graph(bind_namespaces="none")
        self.concepts: set[Node] = set()
        self.schemes: set[Node] = set()
        self.seen: set[tuple[Node, _Mode]] = set()
        self.pending: deque[tuple[Node, _Mode, int]] = deque()
        self.metadata = _METADATA
        self.mapping_seen: set[Edge] = set()
        self.collection_concepts: list[frozenset[Node]] = []

    def adopt(self, report: Report) -> None:
        """Preserve required child truth under the same cumulative allowance."""
        retain_report(report, self.budget, self.issues)
        self.budget.consume("states")
        self.children.append(report)

    def enqueue(self, node: Node, mode: _Mode, depth: int = 0) -> None:
        """Bound worklist references before allocation; identities stay RDF-owned."""
        bound_node(node, self.budget)
        if depth > self.budget.limits.max_depth:
            raise LimitExceeded("Publication metadata exceeds max_depth")
        self.budget.consume("states")
        self.budget.consume("bytes", 32)
        self.pending.append((node, mode, depth))

    def statements(
        self, node: Node, *, direction: str = "outgoing"
    ) -> tuple[Edge, ...]:
        """Use the prepared statement indexes; incomplete lookups cannot close scope."""
        if not isinstance(node, URIRef | BNode):
            raise PatchUnavailable("A selected dependency is not a resource")
        selected = self.view.statements(
            node,
            direction=cast(Literal["incoming", "outgoing", "both"], direction),
            budget=self.budget,
        )
        self.adopt(selected.report)
        if not selected.report.complete:
            raise PatchUnavailable("Publication dependency lookup did not complete")
        return selected.items

    def copy(self, edge: Edge) -> None:
        """Copy an original exact statement without lexical or blank-node rewriting."""
        triple = edge.subject, edge.predicate, edge.object
        for term in triple:
            bound_node(term, self.budget)
        self.budget.consume("work")
        if triple not in self.graph:
            self.budget.consume("triples")
            self.budget.consume("states")
            self.graph.add(triple)

    def boundary(self, edge: Edge, action: str) -> None:
        """Retain exact original assertions excluded by the explicit boundary policy."""
        data = {
            "action": action,
            "statement": [
                term_key(node) for node in (edge.subject, edge.predicate, edge.object)
            ],
        }
        size = _json_bound(data, self.budget)
        if size > self.budget.limits.max_bytes:
            raise LimitExceeded("Publication boundary evidence exceeds max_bytes")
        self.budget.consume("bytes", size)
        self.budget.consume("states")
        self.exclusions.append(
            json.dumps(data, ensure_ascii=True, separators=(",", ":"))
        )

    def select(self) -> None:
        """Keep explicit subsets within the actual finite scheme Concept inventory."""
        specification = self.specification
        for values in (
            (specification.scheme,),
            specification.concepts or (),
            specification.collections,
            specification.annotation_predicates,
        ):
            _iris(values, self.budget)
        self.budget.consume("states", len(specification.annotation_predicates))
        self.metadata = _METADATA | frozenset(
            URIRef(value) for value in specification.annotation_predicates
        )
        self.view = inspect_vocabulary(
            load_vocabulary(self.source, budget=self.budget), budget=self.budget
        )
        subset = select_concepts(
            self.view,
            scheme=specification.scheme,
            closed=True,
            deprecation="allow",
            budget=self.budget,
        )
        self.adopt(subset.report)
        if not subset.report.verified:
            raise PatchUnavailable("Required scheme Concept inventory is incomplete")
        self.budget.consume("states", len(subset.values))
        available = set(subset.values)
        chosen = (
            subset.values
            if specification.concepts is None
            else tuple(URIRef(value) for value in specification.concepts)
        )
        for concept in chosen:
            self.budget.consume("work")
            if concept not in available:
                raise PatchUnavailable(
                    "An explicit Concept lacks selected-scheme membership"
                )
            self.budget.consume("states")
            self.concepts.add(concept)
        if not self.concepts:
            raise PatchUnavailable("Publication selected no actual Concepts")
        self.budget.consume("states")
        self.schemes.add(URIRef(specification.scheme))

    def mapping(self, edge: Edge, anchor: Node) -> None:
        """Apply the same boundary to every encountered original mapping assertion."""
        if edge in self.mapping_seen:
            return
        self.budget.consume("states")
        self.mapping_seen.add(edge)
        other = edge.object if edge.subject == anchor else edge.subject
        if not isinstance(other, URIRef):
            raise PatchUnavailable("Mapping publication needs a named endpoint")
        if other not in self.concepts:
            policy = self.specification.mapping_boundary
            if policy == "omit":
                self.boundary(edge, "omitted-mapping")
                return
            if policy == "retain-reference":
                self.boundary(edge, "external-mapping-reference")
            else:
                selected = self.statements(other, direction="both")
                if not any(
                    assertion.subject == other
                    and assertion.predicate == RDF.type
                    and assertion.object == SKOS.Concept
                    for assertion in selected
                ):
                    raise PatchUnavailable(
                        "Included target lacks an asserted Concept description"
                    )
                self.budget.consume("states")
                self.concepts.add(other)
                self.enqueue(other, "owner")
                for assertion in selected:
                    self.budget.consume("work")
                    scheme = (
                        assertion.object
                        if assertion.subject == other
                        and assertion.predicate
                        in (
                            SKOS.inScheme,
                            SKOS.topConceptOf,
                        )
                        else assertion.subject
                        if assertion.object == other
                        and assertion.predicate == SKOS.hasTopConcept
                        else None
                    )
                    if scheme is not None:
                        if not isinstance(scheme, URIRef):
                            raise PatchUnavailable(
                                "Target membership needs a named scheme"
                            )
                        self.copy(assertion)
                        if scheme not in self.schemes:
                            self.budget.consume("states")
                            self.schemes.add(scheme)
                            self.enqueue(scheme, "owner")
        self.copy(edge)

    def require_members(self, concepts: frozenset[Node]) -> None:
        """Never shorten a selected collection to hide an excluded Concept."""
        for concept in concepts:
            self.budget.consume("work")
            if concept not in self.concepts:
                self.issues.append(
                    Issue(
                        "skos.publication.collection_member_excluded",
                        "Selected collection contains an excluded Concept",
                    )
                )

    def collections(self) -> None:
        """Qualify nested/list dependencies with the existing collection reader."""
        for collection in self.specification.collections:
            expanded = expand_collection(self.view, collection, budget=self.budget)
            self.adopt(expanded.report)
            if not expanded.report.verified:
                raise PatchUnavailable(
                    "Selected collection dependencies are incomplete"
                )
            if self.specification.mapping_boundary == "include-target":
                self.budget.consume("states")
                self.budget.consume("bytes", 16)
                self.collection_concepts.append(expanded.concepts)
            else:
                self.require_members(expanded.concepts)
            self.enqueue(URIRef(collection), "collection")

    def dependencies(self) -> None:
        """Follow representation dependencies and selected metadata only."""
        for node in self.concepts:
            self.enqueue(node, "owner")
        for node in self.schemes:
            self.enqueue(node, "owner")
        while self.pending:
            node, mode, depth = self.pending.popleft()
            self.budget.consume("work")
            if (node, mode) in self.seen:
                continue
            self.budget.consume("states")
            self.seen.add((node, mode))
            if mode == "owner" and node in self.concepts:
                for edge in self.statements(node, direction="both"):
                    self.budget.consume("work")
                    if edge.predicate in _MAPPINGS:
                        self.mapping(edge, node)
            if mode == "label":
                if not isinstance(node, URIRef | BNode):
                    raise PatchUnavailable("An XL label is not a resource")
                described = describe_xl_label(self.view, node, budget=self.budget)
                self.adopt(described.report)
                if not described.report.verified:
                    raise PatchUnavailable("Required XL label form is incomplete")
                assertions = described.items[0].metadata
            else:
                assertions = self.statements(node)
            for edge in assertions:
                self.budget.consume("work")
                predicate, value = edge.predicate, edge.object
                if predicate in _MAPPINGS:
                    self.mapping(edge, node)
                    continue
                selected = (
                    predicate in self.metadata
                    or predicate == RDF.type
                    or predicate in ROLES
                )
                follow: _Mode | None = "label" if predicate in ROLES else None
                if mode == "blank":
                    selected = True
                elif mode == "label":
                    selected |= predicate in (XL.literalForm, XL.labelRelation)
                    if predicate == XL.labelRelation:
                        follow = "label"
                elif mode == "collection":
                    selected |= predicate in (SKOS.member, SKOS.memberList)
                    if predicate == SKOS.memberList:
                        follow = "list"
                    elif predicate == SKOS.member and value not in self.concepts:
                        follow = "collection"
                elif mode == "list":
                    selected |= predicate in (RDF.first, RDF.rest)
                    if predicate == RDF.rest and value != RDF.nil:
                        follow = "list"
                    elif predicate == RDF.first and value not in self.concepts:
                        follow = "collection"
                else:
                    if predicate in (SKOS.inScheme, SKOS.topConceptOf):
                        selected = value in self.schemes
                    elif predicate == SKOS.hasTopConcept:
                        selected = value in self.concepts
                    elif predicate in _RELATIONS and predicate not in _MAPPINGS:
                        selected = True
                        if value not in self.concepts:
                            if not isinstance(value, URIRef):
                                raise PatchUnavailable(
                                    "External relation needs a named endpoint"
                                )
                            self.boundary(edge, "external-semantic-reference")
                if not selected:
                    continue
                self.copy(edge)
                if follow is not None:
                    if value != RDF.nil:
                        self.enqueue(
                            value, follow, depth if follow == "list" else depth + 1
                        )
                elif isinstance(value, BNode):
                    self.enqueue(value, "blank", depth + 1)

    def run(self) -> Transformation:
        """Publish no approved partial graph after incomplete required dependencies."""
        candidate = None
        try:
            self.select()
            self.collections()
            self.dependencies()
            for concepts in self.collection_concepts:
                self.require_members(concepts)
            dataset = Dataset()
            _bind(dataset, (self.source._read(budget=self.budget),), self.budget)
            contexts = tuple(self.source._contexts(budget=self.budget))
            if len(contexts) != 1:
                raise PatchUnavailable("Publication needs exactly one physical graph")
            _, name, _ = contexts[0]
            target = dataset.default_graph if name is None else dataset.graph(name)
            for triple in self.graph:
                self.budget.consume("work")
                self.budget.consume("triples")
                target.add(triple)
            candidate = PreparedSource._from_owned(
                dataset,
                is_dataset=self.source.is_dataset,
                document_iri=self.source.document_iri,
                effective_url=self.source.effective_url,
                literal_fidelity=self.source.literal_fidelity,
                graph_fidelity=self.source.graph_fidelity,
                graph_name=self.source.graph_name,
                max_triples=self.budget.limits.max_triples,
            )
        except PatchUnavailable as error:
            try:
                self.issues.append(
                    Issue(
                        "skos.publication.dependencies_unverified",
                        str(error),
                        kind="unverified",
                    )
                )
            except LimitExceeded as exhausted:
                self.issues.exhausted(str(exhausted))
        except LimitExceeded as error:
            self.issues.exhausted(str(error))
        report = Report(
            "skos.publication_dependencies",
            len(self.graph),
            tuple(self.issues),
            requested=max(1, len(self.graph)),
            children=tuple(self.children),
            complete=not any(
                issue.kind in ("unverified", "exhausted") for issue in self.issues
            ),
            exclusions=tuple(self.exclusions),
            regime="selected-skos-publication",
        )
        return Transformation(candidate if report.verified else None, report)


def _candidate(
    source: PreparedSource, specification: SkosPublication, budget: Operation
) -> MigrationCandidate:
    """Use native typed patches and exact original blank ownership for all edits."""
    transformed = _Projection(source, specification, budget).run()
    patch = None
    metadata = None
    children = [transformed.report]
    issues = IssueCollector(budget)
    try:
        retain_report(transformed.report, budget, issues)
        if transformed.source is not None:
            before = capture_source(source, "before:", budget)
            after = capture_source(transformed.source, "after:", budget)
            pairs = []
            removed = []
            for identity, (node, _) in before.owned.items():
                budget.consume("work")
                budget.consume("states")
                budget.consume("bytes", 64)
                if identity in after.owned:
                    pairs.append(BlankNodePair(node, after.owned[identity][0]))
                else:
                    removed.append(node)
            planned = plan_rdf_patch(
                source,
                transformed.source,
                blank_pairs=pairs,
                removed_blanks=removed,
                budget=budget,
                timeout=_remaining(budget),
                cancel=budget.cancel,
            )
            retain_report(planned.report, budget, issues)
            children.append(planned.report)
            patch = planned.patch
            metadata = SnapshotMetadata.capture(transformed.source, budget)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    report = Report(
        "skos.publication_candidate",
        len(children),
        tuple(issues),
        requested=len(children),
        children=tuple(children),
    )
    return MigrationCandidate(
        transformed.source if report.verified else None,
        patch,
        (),
        (),
        report,
        _metadata=metadata,
        _checked_source=transformed.source,
    )


def compile_skos_publication(
    source: PreparedSource,
    specification: SkosPublication,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> MigrationPlanResult:
    """Compile a plain/pinned graph as one checked transactional publication step.

    All direct and XL labels are retained; required consumer checks define such
    policies as language coverage and roots. Their own Python/backend execution
    boundaries remain those of the supplied callbacks. Mapping omission/reference
    policies and external semantic references explicitly scope completeness.
    There is no remote dereference, inferred assertion or ontology-equivalence
    claim. Empty consumer checks cannot authorize a final artifact. Accept/export
    through the ordinary migration-plan gate; execution never writes a file.
    """
    if not isinstance(source, PreparedSource) or not isinstance(
        specification, SkosPublication
    ):
        raise InputError("Publication requires an owned source and typed selection")
    operation = Operation(limits, budget, timeout, cancel)

    def validate(candidate: PreparedSource) -> Report:
        """Requalify actual final dependencies using the same finite projection."""
        return _Projection(candidate, specification, operation).run().report

    def transform(candidate: PreparedSource, shared: Budget) -> MigrationCandidate:
        """Use the plan-owned operation budget for the exact native patch."""
        assert isinstance(shared, Operation)
        return _candidate(candidate, specification, shared)

    required = specification.checks or (
        MigrationCheck(
            "publication consumer requirements",
            lambda _: Report(
                "skos.publication_requirements",
                0,
                (
                    Issue(
                        "skos.publication.requirements_unverified",
                        "Required consumer publication checks were not supplied",
                        kind="unverified",
                    ),
                ),
                requested=1,
                complete=False,
            ),
        ),
    )
    checks = (MigrationCheck("publication dependencies", validate), *required)
    return execute_migration_plan(
        source,
        (
            MigrationStep(
                "SKOS publication",
                transform,
                (MigrationCheck("publication source scope", _scope),),
                checks,
            ),
        ),
        final_checks=checks,
        budget=operation,
        timeout=timeout,
        cancel=cancel,
    )
