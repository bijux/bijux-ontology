"""Finite named taxonomy conversion through existing role and SKOS boundaries."""

import threading
import time
from typing import Literal

from rdflib import BNode, Dataset, URIRef
from rdflib import Literal as RdfLiteral
from rdflib.namespace import OWL, RDF, RDFS, SKOS
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.owl.constructs import _UNSUPPORTED
from bijux_ontology.rdf.migrations.candidates import MigrationCheck
from bijux_ontology.rdf.patches.types import PatchUnavailable
from bijux_ontology.rdf.roles.classification import classify_statement
from bijux_ontology.rdf.roles.types import TermRoleOccurrence
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, bound_text, term_key
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.browsing.inspection import (
    VocabularyView,
    inspect_vocabulary,
)
from bijux_ontology.vocabulary.browsing.results import Edge
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.workflows.ontology.publication.projection import (
    compile_skos_publication,
)
from bijux_ontology.workflows.ontology.publication.types import SkosPublication, _iris
from bijux_ontology.workflows.upgrades.plans.types import MigrationPlanResult

from .types import (
    ClassConcept,
    PreferredClassLabel,
    TaxonomyExclusion,
    TaxonomyProjection,
    TaxonomyPublication,
)

_EXPRESSIONS = _UNSUPPORTED - {
    OWL.propertyChainAxiom,
    OWL.datatypeComplementOf,
    OWL.onDatatype,
    OWL.withRestrictions,
}


class _Taxonomy:
    """One bounded conversion retaining actual source assertion identities."""

    def __init__(self, policy: TaxonomyProjection, budget: Operation) -> None:
        """Own only operation-local output and already-qualified evidence vectors."""
        self.policy, self.budget = policy, budget
        self.issues = IssueCollector(budget)
        self.children: list[Report] = []
        self.roles: list[TermRoleOccurrence] = []
        self.exclusions: list[TaxonomyExclusion] = []
        self.bindings: tuple[ClassConcept, ...] = ()
        self.selected: set[Node] = set()
        self.mapping: dict[Node, Node] = {}
        self.choices: dict[Node, dict[str, PreferredClassLabel]] = {}
        self.label_predicates: frozenset[str] = frozenset()
        self.definition_predicates: frozenset[str] = frozenset()
        self.checked = 0

    def adopt(self, report: Report) -> None:
        """Preserve completed child findings before any later output allocation."""
        retain_report(report, self.budget, self.issues)
        self.budget.consume("states")
        self.children.append(report)
        if not report.complete:
            raise PatchUnavailable("A required taxonomy observation is incomplete")

    def issue(
        self, code: str, message: str, node: Node, *, unknown: bool = False
    ) -> None:
        """Retain qualified semantic locations without synthesizing source paths."""
        bound_node(node, self.budget)
        self.issues.append(
            Issue(
                "taxonomy." + code,
                message,
                str(node),
                kind="unverified" if unknown else "finding",
            )
        )

    def exclude(
        self,
        reason: Literal["expression", "outside-parent", "label-choice"],
        edge: Edge,
    ) -> None:
        """Reserve an original assertion witness before retaining an omission."""
        self.budget.consume("states")
        self.budget.consume("bytes", 64)
        self.exclusions.append(TaxonomyExclusion(reason, edge))

    def capture(self) -> None:
        """Requalify every stored declaration under the caller's actual allowance."""
        policy, budget = self.policy, self.budget
        _iris((policy.scheme,), budget)
        classes = _iris(policy.classes, budget)
        _iris(policy.label_predicates, budget)
        _iris(policy.definition_predicates, budget)
        budget.consume(
            "states", len(policy.label_predicates) + len(policy.definition_predicates)
        )
        self.label_predicates = frozenset(policy.label_predicates)
        self.definition_predicates = frozenset(policy.definition_predicates)
        budget.consume("states", len(classes))
        self.selected = {URIRef(value) for value in classes}
        supplied = capture_items(policy.class_mappings, ClassConcept, budget)
        for item in supplied:
            _iris((item.class_iri, item.concept_iri), budget)
        for choice in capture_items(
            policy.preferred_labels, PreferredClassLabel, budget
        ):
            _iris((choice.class_iri,), budget)
            bound_node(choice.term, budget)
            budget.consume("states", 2)
            budget.consume("bytes", 96)
            self.choices.setdefault(URIRef(choice.class_iri), {})[
                (choice.term.language or "").lower()
            ] = choice
        for check in capture_items(policy.checks, MigrationCheck, budget):
            bound_text(check.name, budget)
        if policy.source_class_predicate is not None:
            _iris((policy.source_class_predicate,), budget)
        budget.consume("states", len(classes) * 2)
        budget.consume("bytes", len(classes) * 64)
        self.bindings = (
            supplied
            if policy.identity == "separate-iri"
            else tuple(ClassConcept(value, value) for value in classes)
        )
        self.mapping = {
            URIRef(item.class_iri): URIRef(item.concept_iri) for item in self.bindings
        }
        if URIRef(policy.scheme) in self.mapping.values():
            raise InputError("Scheme and selected concept identities must be distinct")

    def view(self, source: PreparedSource) -> VocabularyView:
        """Use the existing prepared reader without widening physical selection."""
        if (
            source.union
            or isinstance(source.graph_name, BNode)
            or source.is_dataset
            and source.graph_name is None
        ):
            raise PatchUnavailable("Taxonomy requires plain or pinned single graphs")
        if source.literal_fidelity not in ("source_terms", "supplied_terms"):
            raise PatchUnavailable("Taxonomy requires exact supplied literal terms")
        if source.graph_fidelity != "complete":
            raise PatchUnavailable(
                "Taxonomy requires complete selected graph inventory"
            )
        return inspect_vocabulary(
            load_vocabulary(source, budget=self.budget), budget=self.budget
        )

    def qualify(self, view: VocabularyView, source: PreparedSource) -> None:
        """Require class-position witnesses, independently of simultaneous facets."""
        selection = view.all_statements(budget=self.budget)
        self.adopt(selection.report)
        self.budget.consume("states", len(self.selected))
        found: set[Node] = set()
        graph = (
            URIRef(source.graph_name)
            if source.graph_name not in (None, "@default")
            else None
        )
        for edge in selection.items:
            self.budget.consume("work")
            if edge.subject not in self.selected and edge.object not in self.selected:
                continue
            occurrences = classify_statement(
                (edge.subject, edge.predicate, edge.object),
                source_scope=edge.scope,
                graph=graph,
                budget=self.budget,
            )
            for occurrence in occurrences:
                self.budget.consume("work")
                if occurrence.term in self.selected and occurrence.position in (
                    "subject",
                    "object",
                ):
                    self.budget.consume("states")
                    self.budget.consume("bytes", 16)
                    self.roles.append(occurrence)
                    if occurrence.role == "class":
                        found.add(occurrence.term)
        for node in self.selected:
            self.budget.consume("work")
            if node not in found:
                self.issue(
                    "class_role_unverified",
                    "Selected IRI lacks an actual source class-position witness",
                    node,
                    unknown=True,
                )

    def hierarchy(self, view: VocabularyView) -> tuple[Edge, ...]:
        """Select outgoing subclasses and recognize local unrepresented expressions.

        The finite expression anchors reuse existing OWL construct recognition,
        with datatype/property-chain facets outside this class projection. Named
        equivalent/disjoint axioms are not subclass edges. An incident anonymous
        endpoint on those symmetric anchors requires an explicit omission
        decision in either orientation. No expression semantics or closure is
        evaluated.
        """
        selected = view.all_statements(budget=self.budget)
        self.adopt(selected.report)
        edges = []
        for edge in selected.items:
            self.budget.consume("work")
            outgoing = edge.subject in self.selected
            reverse_expression = (
                edge.predicate in (OWL.equivalentClass, OWL.disjointWith)
                and isinstance(edge.subject, BNode)
                and edge.object in self.selected
            )
            if not outgoing and not reverse_expression:
                continue
            expression = (
                reverse_expression
                or edge.predicate in _EXPRESSIONS
                or (
                    edge.predicate in (OWL.equivalentClass, OWL.disjointWith)
                    and isinstance(edge.object, BNode)
                )
            )
            if not expression and edge.predicate != RDFS.subClassOf:
                continue
            if expression or not isinstance(edge.object, URIRef):
                if self.policy.expressions == "omit":
                    self.exclude("expression", edge)
                else:
                    self.issue(
                        "expression_unrepresented",
                        "Requested class expression has no SKOS representation",
                        edge.subject if outgoing else edge.object,
                    )
            elif edge.object not in self.selected:
                if self.policy.outside_parents == "omit":
                    self.exclude("outside-parent", edge)
                else:
                    self.issue(
                        "outside_parent",
                        "Subclass assertion crosses the selected class inventory",
                        edge.subject,
                    )
            else:
                self.budget.consume("states")
                self.budget.consume("bytes", 16)
                edges.append(edge)
        return tuple(edges)

    def add(
        self, graph: Dataset, subject: Node, predicate: URIRef, value: Node
    ) -> None:
        """Charge exact output terms before allocating a new generated assertion."""
        for term in (subject, predicate, value):
            bound_node(term, self.budget)
        self.budget.consume("work")
        triple = subject, predicate, value
        if triple not in graph.default_graph:
            self.budget.consume("triples")
            self.budget.consume("states")
            graph.default_graph.add(triple)

    def annotations(self, view: VocabularyView, graph: Dataset, node: Node) -> None:
        """Select exact source literal candidates, preserving each rejected option."""
        assert isinstance(node, URIRef)
        choices = self.choices.get(node, {})
        if not self.label_predicates and not self.definition_predicates:
            if choices:
                self.issue(
                    "label_choice_invalid",
                    "No label predicate was selected for this choice",
                    node,
                )
            return
        selection = view.statements(node, budget=self.budget)
        self.adopt(selection.report)
        labels = self.label_predicates
        definitions = self.definition_predicates
        groups: dict[str, dict[tuple[str | None, ...], list[Edge]]] = {}
        for edge in selection.items:
            self.budget.consume("work")
            bound_node(edge.predicate, self.budget)
            predicate = str(edge.predicate)
            if predicate not in labels and predicate not in definitions:
                continue
            if not isinstance(edge.object, RdfLiteral):
                self.issue(
                    "annotation_invalid",
                    "Selected label/definition assertion must contain an RDF literal",
                    node,
                )
                continue
            if predicate in definitions:
                self.add(graph, self.mapping[node], SKOS.definition, edge.object)
            if predicate in labels:
                bound_node(edge.object, self.budget)
                self.budget.consume("states", 3)
                self.budget.consume("bytes", 96)
                language = (edge.object.language or "").lower()
                key = term_key(edge.object)
                groups.setdefault(language, {}).setdefault(key, []).append(edge)
        for language, choice in choices.items():
            self.budget.consume("work")
            if language not in groups or term_key(choice.term) not in groups[language]:
                self.issue(
                    "label_choice_invalid",
                    "Preferred label choice is not an actual selected source candidate",
                    node,
                )
        for language, candidates in groups.items():
            self.budget.consume("work")
            selected_choice = choices.get(language)
            chosen = (
                term_key(selected_choice.term)
                if selected_choice is not None
                else next(iter(candidates))
            )
            if len(candidates) > 1 and selected_choice is None:
                self.issue(
                    "label_ambiguous",
                    "Competing labels require an exact language-specific choice",
                    node,
                )
                continue
            for key, witnesses in candidates.items():
                self.budget.consume("work")
                if key == chosen:
                    self.add(
                        graph, self.mapping[node], SKOS.prefLabel, witnesses[0].object
                    )
                else:
                    for edge in witnesses:
                        self.exclude("label-choice", edge)

    def build(
        self, view: VocabularyView, hierarchy: tuple[Edge, ...]
    ) -> PreparedSource:
        """Create a new owned SKOS graph; original OWL assertions remain untouched."""
        self.budget.consume("states", 2)
        self.budget.consume("bytes", 256)
        graph = Dataset()
        scheme = URIRef(self.policy.scheme)
        self.add(graph, scheme, RDF.type, SKOS.ConceptScheme)
        self.budget.consume("states", len(hierarchy))
        children = {edge.subject for edge in hierarchy}
        for node, concept in self.mapping.items():
            self.budget.consume("work")
            self.add(graph, concept, RDF.type, SKOS.Concept)
            self.add(graph, concept, SKOS.inScheme, scheme)
            if self.policy.source_class_predicate is not None:
                self.add(
                    graph, concept, URIRef(self.policy.source_class_predicate), node
                )
            self.annotations(view, graph, node)
            if node not in children:
                self.add(graph, scheme, SKOS.hasTopConcept, concept)
                self.add(graph, concept, SKOS.topConceptOf, scheme)
            self.checked += 1
        for edge in hierarchy:
            self.add(
                graph,
                self.mapping[edge.subject],
                SKOS.broader,
                self.mapping[edge.object],
            )
        if self.policy.identity == "same-iri-punning":
            for occurrence in self.roles:
                self.budget.consume("work")
                subject, predicate, value = occurrence.statement
                if (
                    occurrence.position == "subject"
                    and occurrence.role == "class"
                    and predicate == RDF.type
                    and value in (OWL.Class, RDFS.Class, OWL.DeprecatedClass)
                ):
                    self.add(graph, subject, RDF.type, value)
        return PreparedSource._from_owned(
            graph,
            is_dataset=False,
            document_iri=None,
            effective_url=None,
            literal_fidelity="supplied_terms",
            graph_fidelity="complete",
            max_triples=self.budget.limits.max_triples,
        )


def project_taxonomy(
    source: PreparedSource,
    hierarchy: PreparedSource,
    policy: TaxonomyProjection,
    *,
    hierarchy_basis: Literal["asserted", "supplied-inferred"],
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> TaxonomyPublication:
    """Project selected named classes using an explicit supplied subclass graph.

    The finite profile accepts plain/pinned single graphs. No reasoning,
    equivalence conversion or global taxonomy-root claim is performed. Required
    ordinary consumer checks qualify the actual generated SKOS artifact; missing
    checks cannot yield an exportable publication. Export requires the existing
    migration-plan acceptance API. The original sources remain unchanged.
    """
    if (
        not isinstance(source, PreparedSource)
        or not isinstance(hierarchy, PreparedSource)
        or not isinstance(policy, TaxonomyProjection)
    ):
        raise InputError(
            "Taxonomy publication requires owned sources and typed choices"
        )
    if hierarchy_basis not in ("asserted", "supplied-inferred"):
        raise InputError("Declare the supplied hierarchy basis explicitly")
    operation = Operation(limits, budget, timeout, cancel)
    conversion = _Taxonomy(policy, operation)
    publication: MigrationPlanResult | None = None
    try:
        conversion.capture()
        view = conversion.view(source)
        conversion.qualify(view, source)
        hierarchy_view = view if hierarchy is source else conversion.view(hierarchy)
        edges = conversion.hierarchy(hierarchy_view)
        generated = conversion.build(view, edges)
        if not conversion.issues:
            operation.consume("work", 0)
            remaining = operation.deadline - time.monotonic()
            if remaining <= 0:
                raise LimitExceeded("Taxonomy publication exceeded its deadline")
            result = compile_skos_publication(
                generated,
                SkosPublication(
                    policy.scheme,
                    "retain-reference",
                    concepts=tuple(item.concept_iri for item in conversion.bindings),
                    annotation_predicates=(policy.source_class_predicate,)
                    if policy.source_class_predicate
                    else (),
                    checks=policy.checks,
                ),
                budget=operation,
                timeout=remaining,
                cancel=cancel,
            )
            conversion.adopt(result.report)
            publication = result if result.report.verified else None
    except LimitExceeded as error:
        conversion.issues.exhausted(str(error))
    except (PatchUnavailable, ResourceError) as error:
        try:
            conversion.issues.append(
                Issue("taxonomy.scope_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as exhausted:
            conversion.issues.exhausted(str(exhausted))
    report = Report(
        "ontology.taxonomy_publication",
        conversion.checked,
        tuple(conversion.issues),
        requested=max(1, len(policy.classes)),
        children=tuple(conversion.children),
        regime="explicit-taxonomy-" + hierarchy_basis,
        exclusions=("Explicit omitted assertion witnesses are retained in the result",)
        if conversion.exclusions
        else (),
    )
    return TaxonomyPublication(
        conversion.bindings,
        publication if report.verified else None,
        report,
        tuple(conversion.roles),
        tuple(conversion.exclusions),
    )
