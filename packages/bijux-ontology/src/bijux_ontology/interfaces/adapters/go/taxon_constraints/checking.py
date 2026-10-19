"""Finite GO annotation taxonomy checks over original asserted release evidence."""

from threading import Event

from rdflib import URIRef
from rdflib.namespace import OWL, RDFS

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.interfaces.adapters.vocabulary_bindings.controlled.algebra import (
    compile_allowed,
)
from bijux_ontology.interfaces.adapters.vocabulary_bindings.controlled.types import (
    AllowedDifference,
    AllowedExpression,
    AllowedIntersection,
    AllowedUnion,
)
from bijux_ontology.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.patches.types import PatchUnavailable
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.navigation import traverse
from bijux_ontology.vocabulary.relation_paths.checking import find_relation_path
from bijux_ontology.vocabulary.relation_paths.types import (
    RelationChoice,
    RelationEndpoint,
    RelationPathResult,
    RelationRepeat,
    RelationStep,
)

from .reading import PART_OF, Reader
from .types import (
    GoTaxonAnnotation,
    GoTaxonCheck,
    GoTaxonObservation,
    GoTaxonPolicy,
    GoTaxonReceipt,
    GoTaxonRelease,
    TaxonConstraint,
    TaxonEvaluation,
    iri,
)


class _Checking:
    """Memoize only actual generic path results within one fixed resource set."""

    def __init__(self, reader: Reader, policy: GoTaxonPolicy) -> None:
        """Retain a finite selected root contract without deriving biological rules."""
        self.reader, self.policy = reader, policy
        self.paths: dict[
            tuple[str, URIRef, tuple[URIRef, ...]], RelationPathResult
        ] = {}
        self.closed: set[URIRef] = set()

    def declared(self, role: str, node: URIRef) -> None:
        """Referenced-only or absent names cannot satisfy reflexive class paths."""
        iri(node, self.reader.operation)
        kinds = self.reader.resources[role].view.lookup(str(node)).kinds
        self.reader.operation.consume("work", len(kinds) + 1)
        if not any(kind in (str(OWL.Class), str(RDFS.Class)) for kind in kinds):
            raise PatchUnavailable(
                "Selected name lacks an actual class declaration in its resource role"
            )

    def path(
        self, role: str, start: URIRef, targets: tuple[URIRef, ...]
    ) -> RelationPathResult:
        """Use the existing typed class/existential engine, preserving raw witnesses."""
        operation = self.reader.operation
        iri(start, operation)
        for target in targets:
            iri(target, operation)
        operation.consume("states", len(targets))
        key = role, start, targets
        if key not in self.paths:
            step = RelationStep(str(RDFS.subClassOf), "class")
            spec = RelationRepeat(
                RelationChoice((step, RelationStep(str(PART_OF), "existential")))
                if role == "ontology"
                else step
            )
            self.reader.reserve()
            result = find_relation_path(
                self.reader.resources[role].view,
                str(start),
                spec,
                endpoint=RelationEndpoint(tuple(map(str, targets))),
                budget=operation,
            )
            self.paths[key] = result
            self.reader.adopt(result.report)
        return self.paths[key]

    def ancestry(self, taxon: URIRef) -> None:
        """Require declared, bounded, root-connected complete selected ancestry.

        Supplied source completeness remains an explicit caller declaration; no
        algorithm infers missing real-world NCBI taxonomy facts from silence.
        """
        if taxon in self.closed:
            return
        if not self.policy.complete_ancestry:
            raise PatchUnavailable("Taxonomy ancestry was not supplied as complete")
        for root in self.policy.taxonomy_roots:
            self.declared("taxonomy", root)
        self.declared("taxonomy", taxon)
        self.reader.reserve()
        view = self.reader.resources["taxonomy"].view
        closure = traverse(
            view, str(taxon), include_self=True, budget=self.reader.operation
        )
        self.reader.adopt(closure.report)
        if not closure.report.verified:
            raise PatchUnavailable("Selected taxonomy ancestry traversal is incomplete")
        for visit in closure.items:
            self.reader.operation.consume("work")
            node = URIRef(visit.iri)
            self.declared("taxonomy", node)
            self.reader.reserve()
            parents = view.relationships(
                visit.iri,
                predicates=(str(RDFS.subClassOf),),
                budget=self.reader.operation,
            )
            self.reader.adopt(parents.report)
            if (
                not parents.report.complete
                or any(issue.kind == "finding" for issue in parents.report.issues)
                or any(not isinstance(edge.object, URIRef) for edge in parents.items)
            ):
                raise PatchUnavailable(
                    "Anonymous or incomplete taxonomy ancestry is unsupported"
                )
            linked = self.path("taxonomy", node, self.policy.taxonomy_roots)
            if linked.status != "found":
                raise PatchUnavailable(
                    "A selected taxonomy ancestor does not connect to a supplied root"
                )
        self.reader.operation.consume("states")
        self.closed.add(taxon)

    def evaluate(
        self,
        annotation: GoTaxonAnnotation,
        constraint: TaxonConstraint,
        inherited: RelationPathResult,
        support: tuple[tuple[TaxonConstraint, RelationPathResult], ...],
    ) -> TaxonEvaluation:
        """Resolve one clause's real union alternatives before conjoining clauses."""
        alternatives, structure = self.reader.grouping(constraint.target)
        self.reader.reserve(len(alternatives))
        ancestry = []
        for member in alternatives:
            self.ancestry(member)
            result = self.path("taxonomy", annotation.taxon, (member,))
            ancestry.append(result)
        known = all(item.status != "incomplete" for item in ancestry)
        membership = any(item.status == "found" for item in ancestry)
        accepted = (
            (not membership if constraint.kind == "never" else membership)
            if known
            else None
        )
        self.reader.reserve(len(ancestry) + len(support))
        return TaxonEvaluation(
            constraint,
            alternatives,
            inherited,
            tuple(ancestry),
            structure,
            accepted,
            support,
        )


def _expression(
    taxon: URIRef, evaluation: TaxonEvaluation, operation: Operation
) -> AllowedExpression:
    """Use finite set algebra only within this one selected annotation universe.

    These private singleton sets are not exported as globally closed lists of
    biologically permitted taxa. The public result exposes original evaluations.
    """
    bound_text(taxon, operation)
    count = len(evaluation.ancestry) + 1
    operation.consume("states", 4 * count)
    operation.consume("work", count * (4 * len(taxon) + 1))
    operation.consume("bytes", count * (256 + 4 * len(taxon.encode("utf-8"))))
    universe = ValueEnumeration((taxon,))
    alternatives = []
    for path in evaluation.ancestry:
        if path.status == "found":
            alternatives.append(ValueEnumeration((taxon,), report=path.report))
        elif path.status == "nonmatch":
            alternatives.append(
                ValueEnumeration((), excluded=(taxon,), report=path.report)
            )
        else:
            alternatives.append(ValueEnumeration((), closed=False, report=path.report))
    union = AllowedUnion(tuple(alternatives))
    return (
        AllowedDifference(universe, union)
        if evaluation.constraint.kind == "never"
        else union
    )


def check_go_taxa(
    release: GoTaxonRelease,
    annotations: tuple[GoTaxonAnnotation, ...],
    *,
    policy: GoTaxonPolicy,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10.0,
    cancel: Event | None = None,
) -> GoTaxonCheck:
    """Check explicit annotations under the finite modern GO release convention.

    Constraints propagate through selected named is_a and existential part_of,
    never regulates. Direct computed copies and inherited copies of one condition
    retain every witness but form one idempotent obligation. Union members are
    alternatives; distinct restrictions are conjoined. No general OWL reasoning,
    biological absence, live resource resolution or global allowed-taxon list is
    claimed. Caller-pinned release changes are prepared afresh on every call.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    reports: list[Report] = []
    receipts: list[GoTaxonReceipt] = []
    observations: list[GoTaxonObservation] = []
    reader = Reader(operation, issues, reports, receipts)
    checked = 0
    requested = 1
    try:
        if not isinstance(policy, GoTaxonPolicy):
            raise InputError("Expected GoTaxonPolicy")
        for root in capture_items(policy.taxonomy_roots, URIRef, operation):
            iri(root, operation)
        if (
            type(policy.complete_ancestry) is not bool
            or type(policy.require_constraint) is not bool
        ):
            raise InputError("Taxon policy requires explicit boolean choices")
        supplied = capture_items(annotations, GoTaxonAnnotation, operation)
        requested = max(1, len(supplied))
        if not supplied:
            raise PatchUnavailable(
                "Taxon checking requires actual selected annotations"
            )
        reader.load(release)
        constraints = reader.constraints()
        state = _Checking(reader, policy)
        for annotation in supplied:
            iri(annotation.term, operation)
            iri(annotation.taxon, operation)
            bound_text(annotation.location, operation)
            reader.reserve()
            state.declared("ontology", annotation.term)
            state.ancestry(annotation.taxon)
            applicable: dict[
                tuple[str, URIRef], list[tuple[TaxonConstraint, RelationPathResult]]
            ] = {}

            for constraint in constraints:
                inherited = state.path("ontology", annotation.term, (constraint.owner,))
                if inherited.status == "found":
                    operation.consume("states", 2)
                    applicable.setdefault(
                        (constraint.kind, constraint.target), []
                    ).append((constraint, inherited))
                elif inherited.status == "incomplete":
                    raise PatchUnavailable(
                        "Required GO inheritance scope is incomplete"
                    )
            for owner in reader.authored_owners:
                inherited = state.path("ontology", annotation.term, (owner,))
                if inherited.status == "found":
                    for constraint in reader.authored(owner):
                        operation.consume("states", 2)
                        applicable.setdefault(
                            (constraint.kind, constraint.target), []
                        ).append((constraint, inherited))
                elif inherited.status == "incomplete":
                    raise PatchUnavailable(
                        "Authored GO inheritance scope is incomplete"
                    )
            if not applicable and policy.require_constraint:
                raise PatchUnavailable("No requested taxon constraint was extracted")
            evaluations = []
            for witnesses in applicable.values():
                reader.reserve(len(witnesses))
                constraint, inherited = witnesses[0]
                evaluation = state.evaluate(
                    annotation, constraint, inherited, tuple(witnesses[1:])
                )
                evaluations.append(evaluation)
                if evaluation.accepted is False:
                    issues.append(
                        Issue(
                            "go.taxon_disallowed",
                            "Annotation taxon violates a selected taxon restriction",
                            annotation.location,
                            kind="finding",
                            expected=str(constraint.target),
                            actual=str(annotation.taxon),
                        )
                    )
            expressions = tuple(
                _expression(annotation.taxon, evaluation, operation)
                for evaluation in evaluations
            )
            if expressions:
                reader.reserve()
                allowed = compile_allowed(
                    AllowedIntersection(expressions), budget=operation
                )
                reader.adopt(allowed.qualification_report())
                accepted = (
                    annotation.taxon in allowed.values if allowed.qualified else None
                )
            else:
                accepted = True
            reader.reserve(len(evaluations))
            observations.append(
                GoTaxonObservation(annotation, tuple(evaluations), accepted)
            )
            checked += 1
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (ResourceError, PatchUnavailable, InputError) as error:
        try:
            issues.append(Issue("go.taxon_unverified", str(error), kind="unverified"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return GoTaxonCheck(
        tuple(observations),
        tuple(receipts),
        tuple(reports),
        Report(
            "go.taxon_constraints",
            checked,
            tuple(issues),
            requested=requested,
            complete=checked == requested,
            children=tuple(reports),
            regime="go-computed-2024-09-selected-annotation-taxonomy",
        ),
    )
