"""Selected asserted edges and exact native restriction projection evidence."""

from collections.abc import Iterator

from rdflib import BNode, URIRef
from rdflib.namespace import OWL, RDFS

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, IssueCollector, bound_text, retain_report
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Edge, Selection
from bijux_ontology.vocabulary.restrictions.projection import project_restrictions
from bijux_ontology.vocabulary.restrictions.types import RestrictionProjection

from .automaton import named
from .types import RelationStep, RelationWitness


def _edge(edge: Edge, budget: Budget) -> None:
    """Bound actual assertion metadata before comparisons or retention."""
    if not isinstance(edge, Edge):
        raise InputError("Projection evidence requires original typed edges")
    bound_text(edge.scope, budget)
    for node in (edge.subject, edge.predicate, edge.object):
        bound_node(node, budget)


def _projection(record: RestrictionProjection, budget: Budget) -> None:
    """Qualify every supplied field before matching original projection evidence."""
    if not isinstance(record, RestrictionProjection):
        raise InputError("Caller projections require RestrictionProjection records")
    for node in (
        record.owner,
        record.authored_owner,
        record.expression,
        record.property,
        record.quantifier,
        record.filler,
    ):
        if node is not None:
            bound_node(node, budget)
    bound_text(record.status, budget)
    for edges in ((record.axiom,), record.inheritance, record.structure):
        if type(edges) is not tuple:
            raise InputError("Projection evidence must be immutable")
        budget.consume("states", len(edges))
        budget.consume("bytes", 64 * len(edges))
        for edge in edges:
            _edge(edge, budget)


def _same(
    left: RestrictionProjection, right: RestrictionProjection, budget: Budget
) -> bool:
    """Require exact original blank objects, never same-label correspondence."""
    budget.consume("work", 8 + len(left.inheritance) + len(left.structure))
    _projection(right, budget)
    if left != right:
        return False
    for first, second in (
        (left.expression, right.expression),
        (left.filler, right.filler),
    ):
        if isinstance(first, BNode) and first is not second:
            return False
    for first_edge, second_edge in zip(
        (left.axiom, *left.inheritance, *left.structure),
        (right.axiom, *right.inheritance, *right.structure),
        strict=True,
    ):
        for a, b in (
            (first_edge.subject, second_edge.subject),
            (first_edge.object, second_edge.object),
        ):
            if isinstance(a, BNode) and a is not b:
                return False
    return True


class _Inventory:
    """Reuse existing scoped reads and projection, with one operation's allowance."""

    def __init__(
        self,
        view: VocabularyView,
        budget: Budget,
        issues: IssueCollector,
        reports: list[Report],
    ) -> None:
        self.view, self.budget, self.issues, self.reports = (
            view,
            budget,
            issues,
            reports,
        )
        self.complete = True
        self.projected: dict[
            tuple[URIRef, bool], tuple[RestrictionProjection, ...]
        ] = {}
        self.supplied: tuple[RestrictionProjection, ...] | None = None
        self.owners: tuple[URIRef, ...] | None = None

    def adopt(self, report: Report, *, owned: bool = True) -> None:
        """Keep paid native reports; qualify supplied metadata before publication."""
        if owned:
            self.reports.append(report)
        retain_report(report, self.budget, self.issues)
        if not owned:
            self.reports.append(report)
        self.complete = self.complete and report.complete

    def reserve(self) -> None:
        """Own one result/report reference before invoking a selected read."""
        self.budget.consume("states", 2)
        self.budget.consume("bytes", 128)

    def projections(
        self, owner: URIRef, inherited: bool = False
    ) -> tuple[RestrictionProjection, ...]:
        """Interpret restrictions only through the existing native projection."""
        key = owner, inherited
        if key not in self.projected:
            self.reserve()
            result = project_restrictions(
                self.view, str(owner), inherited=inherited, budget=self.budget
            )
            self.projected[key] = result.items
            self.adopt(result.report)
            for record in result.items:
                _projection(record, self.budget)
        return self.projected[key]

    def supplied_view(self, selected: Selection[RestrictionProjection]) -> None:
        """Keep caller view coverage required and requalify its original records."""
        if (
            not isinstance(selected, Selection)
            or not isinstance(selected.report, Report)
            or type(selected.items) is not tuple
        ):
            raise InputError(
                "Caller projection views require typed selection and report"
            )
        self.reserve()
        self.adopt(selected.report, owned=False)
        self.budget.consume("states", len(selected.items))
        self.budget.consume("bytes", 64 * len(selected.items))
        kept = []
        for record in selected.items:
            _projection(record, self.budget)
            owner = named(record.owner, self.budget)
            actual = self.projections(owner, bool(record.inheritance))
            if not any(_same(record, candidate, self.budget) for candidate in actual):
                self.issues.append(
                    Issue(
                        "terms.relation_projection_unverified",
                        "Projection is not an original record in the selected view",
                        kind="unverified",
                    )
                )
                self.complete = False
            else:
                kept.append(record)
        self.supplied = tuple(kept)

    def all_owners(self) -> tuple[URIRef, ...]:
        """Inventory actual named axiom owners only for inverse projections."""
        if self.owners is None:
            self.reserve()
            result = self.view.all_statements(budget=self.budget)
            self.adopt(result.report)
            owners: set[URIRef] = set()
            for edge in result.items:
                self.budget.consume("work")
                if edge.predicate in (
                    RDFS.subClassOf,
                    OWL.equivalentClass,
                ) and isinstance(edge.subject, URIRef):
                    bound_node(edge.subject, self.budget)
                    self.budget.consume("states")
                    self.budget.consume("bytes", 64)
                    owners.add(edge.subject)
            self.owners = tuple(owners)
        return self.owners

    def neighbours(
        self, node: URIRef, step: RelationStep, inverse: bool
    ) -> Iterator[RelationWitness]:
        """Keep raw assertions and someValuesFrom projections as different roles."""
        if step.role != "existential":
            self.reserve()
            result = self.view.relationships(
                str(node),
                predicates=(step.predicate,),
                direction="incoming" if inverse else "outgoing",
                budget=self.budget,
            )
            self.adopt(result.report)
            for edge in result.items:
                self.budget.consume("work")
                _edge(edge, self.budget)
                target = edge.subject if inverse else edge.object
                if not isinstance(target, URIRef):
                    continue
                self.budget.consume("states")
                self.budget.consume("bytes", 128)
                yield RelationWitness(node, target, step, inverse, edge)
            return
        if self.supplied is not None:
            records = iter(self.supplied)
        elif inverse:
            records = (
                record
                for owner in self.all_owners()
                for record in self.projections(owner)
            )
        else:
            records = iter(self.projections(node))
        for record in records:
            self.budget.consume("work")
            if (
                record.status != "supported"
                or record.quantifier != OWL.someValuesFrom
                or record.property != URIRef(step.predicate)
                or not isinstance(record.filler, URIRef)
            ):
                continue
            source, target = (
                (record.filler, record.owner)
                if inverse
                else (record.owner, record.filler)
            )
            if source != node:
                continue
            bound_node(target, self.budget)
            self.budget.consume("states")
            self.budget.consume("bytes", 128)
            yield RelationWitness(node, target, step, inverse, record)
