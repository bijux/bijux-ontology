"""Scoped application resource roles over existing owned RDF and type proofs."""

from collections.abc import Sequence
from dataclasses import replace
from typing import Literal as Choice
from typing import cast

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import OWL, RDF, SKOS

from bijux_ontology.declarations.resource_roles import ResourceRole
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.owl.obligations import OntologyObligation, _NativeProofs
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.roles.classification import (
    bound_node,
    bound_text,
    classify_statement,
    declaration_role,
)
from bijux_ontology.rdf.roles.types import TermRole
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.inspection import VocabularyView, inspect_vocabulary
from bijux_ontology.vocabulary.view import Vocabulary

from .types import RoleUse


def _contract(contract: ResourceRole, budget: Budget) -> None:
    """Charge already validated declaration metadata to the actual operation."""
    budget.consume("states", len(contract.types) + 1)
    for iri in contract.types:
        bound_text(iri, budget)
    if contract.bridge is not None:
        bound_text(contract.bridge.predicate, budget)


class _RoleChecks:
    """Reuse one owned vocabulary and selected actual graph for finite lookups."""

    def __init__(
        self,
        vocabulary: Vocabulary | Graph | VocabularyView,
        budget: Budget,
        data: Graph | None = None,
    ) -> None:
        self.budget = budget
        self.view = (
            vocabulary
            if isinstance(vocabulary, VocabularyView)
            else inspect_vocabulary(vocabulary, budget=budget)
        )
        self.schema = self.view._reader.graph
        self.actual_data = data is not None
        self.data = data if data is not None else self.schema
        self.proofs = _NativeProofs(self.schema, budget)
        self._facets: dict[URIRef | BNode, frozenset[TermRole]] = {}
        self._property_kinds: dict[str, frozenset[str]] = {}
        self.children: list[Report] = []
        self.exhaustion: Report | None = None

    def individual_endpoint(self, predicate: str, *, subject: bool = False) -> bool:
        """Use exact property declarations to qualify ordinary assertion positions."""
        bound_text(predicate, self.budget)
        if predicate.startswith("@"):
            return False
        bound_node(URIRef(predicate), self.budget)
        if predicate not in self._property_kinds:
            kinds: set[str] = set()
            supported = (
                str(OWL.ObjectProperty),
                str(OWL.DatatypeProperty),
                str(OWL.AnnotationProperty),
            )
            for kind in self.view.lookup(predicate).kinds:
                bound_text(kind, self.budget)
                if kind in supported:
                    self.budget.consume("states")
                    kinds.add(kind)
            if self.actual_data:
                for value in self.data.objects(URIRef(predicate), RDF.type):
                    self.budget.consume("work")
                    bound_node(value, self.budget)
                    if isinstance(value, URIRef) and str(value) in supported:
                        self.budget.consume("states")
                        kinds.add(str(value))
            self.budget.consume("states", len(kinds) + 1)
            self._property_kinds[predicate] = frozenset(kinds)
        captured = self._property_kinds[predicate]
        selected = captured.intersection(
            map(str, (OWL.ObjectProperty, OWL.DatatypeProperty, OWL.AnnotationProperty))
        )
        if len(selected) != 1:
            return False
        return (
            str(OWL.ObjectProperty) in selected
            or subject
            and str(OWL.DatatypeProperty) in selected
        )

    def facets(self, node: URIRef | BNode) -> frozenset[TermRole]:
        """Read indexed incident statements, preserving their per-position roles."""
        self.budget.consume("work")
        if node in self._facets:
            return self._facets[node]
        result: set[TermRole] = set()
        complete = True
        for direction in ("outgoing", "incoming"):
            selected = self.view.statements(
                node, direction=direction, budget=self.budget
            )
            if not selected.report.complete:
                self.children.append(selected.report)
                complete = False
            for edge in selected.items:
                for occurrence in classify_statement(
                    (edge.subject, edge.predicate, edge.object),
                    source_scope=edge.scope,
                    budget=self.budget,
                ):
                    if occurrence.term == node and occurrence.position in (
                        "subject",
                        "object",
                    ):
                        self.budget.consume("states")
                        result.add(occurrence.role)
                        if occurrence.role == "unknown" and self.individual_endpoint(
                            str(edge.predicate),
                            subject=occurrence.position == "subject",
                        ):
                            self.budget.consume("states")
                            result.add("individual")
        if self.data is not self.schema:
            for pattern in ((node, None, None), (None, None, node)):
                for triple in self.data.triples(pattern):
                    for occurrence in classify_statement(triple, budget=self.budget):
                        if occurrence.term == node and occurrence.position in (
                            "subject",
                            "object",
                        ):
                            self.budget.consume("states")
                            result.add(occurrence.role)
                            if (
                                occurrence.role == "unknown"
                                and self.individual_endpoint(
                                    str(triple[1]),
                                    subject=occurrence.position == "subject",
                                )
                            ):
                                self.budget.consume("states")
                                result.add("individual")
        self.budget.consume("states", len(result) + 1)
        owned = frozenset(result)
        if complete:
            self._facets[node] = owned
        return owned

    def asserted_types(self, node: URIRef | BNode) -> tuple[tuple[str, ...], bool]:
        """Select ordinary actual type witnesses; declarations never become them."""
        types: set[str] = set()
        individual = False
        for _, _, value in self.data.triples((node, RDF.type, None)):
            self.budget.consume("work")
            bound_node(value, self.budget)
            if value == OWL.NamedIndividual:
                individual = True
            elif isinstance(value, URIRef) and declaration_role(value) is None:
                self.budget.consume("states")
                types.add(str(value))
                individual = True
        return tuple(sorted(types)), individual

    def check(
        self,
        use: RoleUse,
        *,
        asserted_types: tuple[str, ...] | None = None,
        actual_individual: bool = False,
    ) -> Report:
        """Qualify one role, keeping chosen bridge and actual instance scopes apart."""
        issues = IssueCollector(self.budget)
        anchor = ""
        start = len(self.children)
        try:
            bound_text(use.location, self.budget)
            anchor = use.location
            bound_node(use.term, self.budget)
            _contract(use.contract, self.budget)
            self._check(
                use.term,
                use.contract,
                anchor,
                issues,
                asserted_types,
                actual_individual,
            )
        except LimitExceeded as error:
            issues.exhausted(str(error), location=anchor)
        return Report(
            "resource.role",
            1,
            tuple(issues),
            regime="application-resource-role",
            children=tuple(self.children[start:]),
            complete=not any(issue.kind == "exhausted" for issue in issues),
        )

    def matches(
        self,
        contract: ResourceRole,
        term: URIRef | BNode | None,
        asserted_types: tuple[str, ...],
        existing_types: tuple[str, ...] = (),
        *,
        predicate: str | None = None,
        reverse: bool = False,
    ) -> bool | None:
        """Conjoin one branch without retaining rejected alternatives globally."""
        if term is None:
            return None
        if (
            contract.kind == "instance"
            and contract.bridge is not None
            and not self.actual_data
        ):
            return None
        if contract.kind == "instance" and contract.bridge is None and existing_types:
            for iri in existing_types:
                bound_text(iri, self.budget)
            self.budget.consume("states", len(existing_types))
            contract = replace(
                contract, types=tuple(dict.fromkeys((*contract.types, *existing_types)))
            )
        individual = predicate is not None and self.individual_endpoint(
            predicate, subject=reverse
        )
        report = self.check(
            RoleUse(term, contract),
            asserted_types=asserted_types,
            actual_individual=individual,
        )
        if report.outcome == "exhausted" and self.exhaustion is None:
            self.exhaustion = report
        if any(
            issue.kind == "finding" and issue.severity == "error"
            for issue in report.issues
        ):
            return False
        return True if report.verified else None

    def _check(
        self,
        node: URIRef | BNode,
        contract: ResourceRole,
        anchor: str,
        issues: IssueCollector,
        asserted_types: tuple[str, ...] | None,
        actual_individual: bool = False,
    ) -> None:
        """Conjoin role, punning and bridge obligations without inference leakage."""
        if contract.bridge is not None:
            bridge = contract.bridge
            self._check(
                node,
                ResourceRole(bridge.source_role, punning=contract.punning),
                anchor,
                issues,
                asserted_types,
                actual_individual,
            )
            selected = self.view.statements(
                node,
                predicates=(bridge.predicate,),
                direction="incoming" if bridge.reverse else "outgoing",
                budget=self.budget,
            )
            if not selected.report.complete:
                self.children.append(selected.report)
            targets = set()
            for edge in selected.items:
                self.budget.consume("work")
                self.budget.consume("states")
                target = edge.subject if bridge.reverse else edge.object
                bound_node(target, self.budget)
                targets.add(target)
            if len(targets) != 1:
                issues.append(
                    Issue(
                        "roles.bridge_unverified",
                        "Role bridge needs exactly one established target",
                        anchor,
                        kind="unverified",
                    )
                )
                return
            target = next(iter(targets))
            if not isinstance(target, URIRef | BNode):
                issues.append(
                    Issue(
                        "roles.bridge_unverified",
                        "Role bridge target is not an RDF resource",
                        anchor,
                        kind="unverified",
                    )
                )
                return
            self._check(target, replace(contract, bridge=None), anchor, issues, None)
            return
        facets = self.facets(node)
        ordinary, individual = (
            self.asserted_types(node)
            if asserted_types is None
            else (asserted_types, bool(asserted_types))
        )
        if asserted_types is not None:
            self.budget.consume("states", len(facets))
            described = set(facets)
            for iri in asserted_types:
                bound_text(iri, self.budget)
                bound_node(URIRef(iri), self.budget)
                self.budget.consume("states", 2)
                marker = declaration_role(URIRef(iri))
                if marker is not None:
                    described.add(marker)
                elif isinstance(node, URIRef) and iri == str(node):
                    described.add("class")
                if iri == str(SKOS.Concept):
                    described.add("concept")
            facets = frozenset(described)
            ordinary = tuple(
                iri for iri in asserted_types if declaration_role(URIRef(iri)) is None
            )
            individual = bool(ordinary) or str(OWL.NamedIndividual) in asserted_types
        individual |= actual_individual or "individual" in facets
        if (
            contract.punning == "forbid"
            and "class" in facets
            and ("individual" in facets or "concept" in facets or individual)
        ):
            issues.append(
                Issue(
                    "roles.punning_forbidden",
                    "Application policy forbids established "
                    "class/individual facet reuse",
                    anchor,
                )
            )
        if contract.kind != "instance":
            if contract.kind not in facets:
                issues.append(
                    Issue(
                        "roles.role_unverified",
                        "Selected resource lacks established evidence "
                        "for the required role",
                        anchor,
                        kind="unverified",
                        expected=contract.kind,
                    )
                )
            return
        if not individual:
            issues.append(
                Issue(
                    "roles.instance_type_missing",
                    "Instance role requires an actual individual/type witness; "
                    "a declaration alone is insufficient",
                    anchor,
                )
            )
            return
        if ordinary:
            answer = self.proofs.answer(
                OntologyObligation(
                    "required_coinstance",
                    tuple(dict.fromkeys((*ordinary, *contract.types))),
                )
            )
            if answer.status == "contradicted":
                issues.append(
                    Issue(
                        "roles.type_conflict",
                        "Required instance and actual type witnesses "
                        "have an explicit supported conflict",
                        anchor,
                    )
                )
            elif self.proofs.classes.ancestors(ordinary)[1]:
                issues.append(
                    Issue(
                        "roles.role_unverified",
                        "Instance types involve unsupported expressions",
                        anchor,
                        kind="unverified",
                    )
                )
        covered = set(ordinary)
        if contract.type_mode == "named" and ordinary:
            covered = set(self.proofs.classes.ancestors(ordinary)[0])
        missing = set(contract.types) - covered
        if missing:
            issues.append(
                Issue(
                    "roles.instance_type_missing"
                    if contract.type_mode == "asserted"
                    else "roles.role_unverified",
                    "Required instance types lack the selected "
                    "asserted/named witnesses",
                    anchor,
                    kind="finding"
                    if contract.type_mode == "asserted"
                    else "unverified",
                )
            )


def check_resource_roles(
    vocabulary: Vocabulary | Graph | VocabularyView,
    uses: Sequence[RoleUse],
    *,
    data: PreparedSource | Graph | None = None,
    graph: GraphSelection | None = None,
    profile: Choice["owl2-dl"] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Report:
    """Check selected finite interpretations, never complete OWL profile membership.

    Dataset inputs require an exact selected graph; no implicit union is used.
    Instance witnesses come only from the selected actual graph (vocabulary when
    data is omitted). A requested full profile remains an unavailable required
    scope until an actual profile-validation capability is supplied.
    """
    if not isinstance(uses, Sequence) or isinstance(cast(object, uses), str | bytes):
        raise InputError("Resource roles require a finite RoleUse sequence")
    if profile is not None and profile != "owl2-dl":
        raise InputError("Select the supported explicit OWL profile request")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Expected explicit Limits")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Explicit limits conflict with the shared operation budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    children: list[Report] = []
    checked = 0
    requested = len(uses)
    complete = True
    try:
        if requested > operation.limits.max_states:
            raise LimitExceeded("Role use inventory exceeds max_states")
        selected = []
        for index in range(requested):
            operation.consume("work")
            operation.consume("states")
            try:
                use = uses[index]
            except IndexError as error:
                raise InputError("Role use inventory changed during capture") from error
            if not isinstance(use, RoleUse):
                raise InputError("Expected typed RoleUse values")
            bound_text(use.location, operation)
            bound_node(use.term, operation)
            _contract(use.contract, operation)
            selected.append(use)
        actual = None
        if data is not None:
            source = (
                data
                if isinstance(data, PreparedSource)
                else prepare_rdf(data, limits=operation.limits, budget=operation)
            )
            actual = source._read(graph, budget=operation)
        elif graph is not None:
            raise InputError("A graph selection requires supplied actual data")
        checks = _RoleChecks(vocabulary, operation, actual)
        for use in selected:
            children.append(checks.check(use))
            checked += 1
            if children[-1].outcome == "exhausted":
                complete = False
                break
        if profile is not None:
            profile_issues = IssueCollector(operation)
            try:
                profile_issues.append(
                    Issue(
                        "roles.profile_unavailable",
                        "Full OWL profile validation requires an actual "
                        "optional profile capability",
                        kind="unverified",
                    )
                )
            except LimitExceeded as error:
                profile_issues.exhausted(str(error))
            children.append(
                Report(
                    "resource.profile",
                    0,
                    tuple(profile_issues),
                    requested=1,
                    complete=False,
                )
            )
    except LimitExceeded as error:
        issues.exhausted(str(error))
        complete = False
    return Report(
        "resource.roles",
        checked,
        tuple(issues),
        requested=requested,
        complete=complete,
        children=tuple(children),
        regime="finite-resource-roles",
    )
