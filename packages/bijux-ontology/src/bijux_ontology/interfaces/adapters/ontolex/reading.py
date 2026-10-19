"""Read only selected supplied RDF; no reasoner, imports or linguistic inference."""

from collections.abc import Sequence
from threading import Event
from typing import cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.literals import check_literal
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.inspection import VocabularyView

from .types import ONTOLEX as O
from .types import (
    FormLink,
    LexicalEntry,
    LexicalForm,
    LexicalPolicy,
    LexicalReference,
    LexicalSense,
    Lexicon,
    Resource,
    Statement,
)

_CORE = frozenset(
    (
        RDF.type,
        O.canonicalForm,
        O.otherForm,
        O.lexicalForm,
        O.sense,
        O.writtenRep,
        O.reference,
    )
)
_ENTRY_TYPES = frozenset((O.LexicalEntry, O.Word, O.Affix, O.MultiwordExpression))
_FORM_LINKS = (O.canonicalForm, O.otherForm, O.lexicalForm)
_DEFAULT_POLICY = LexicalPolicy()


def reserve(budget: Budget, count: int = 1) -> None:
    """Pay immutable object/reference storage before materialization."""
    budget.consume("states", count)
    budget.consume("bytes", 128 * count)


class _Reader:
    """Cache resource identities inside exactly one owned physical graph."""

    def __init__(
        self,
        graph: Graph,
        view: VocabularyView,
        policy: LexicalPolicy,
        budget: Budget,
        issues: IssueCollector,
        children: list[Report],
        faithful: bool,
    ) -> None:
        self.graph, self.view, self.policy = graph, view, policy
        self.budget, self.issues, self.children = budget, issues, children
        self.faithful = faithful
        self.forms: dict[Resource, LexicalForm] = {}
        self.senses: dict[Resource, LexicalSense] = {}

    def values(self, subject: Resource, predicate: URIRef) -> tuple[Node, ...]:
        """Retain finite source terms without lexical normalization or coercion."""
        values = []
        for value in self.graph.objects(subject, predicate):
            self.budget.consume("work")
            bound_node(value, self.budget)
            reserve(self.budget)
            values.append(value)
        return tuple(values)

    def issue(
        self, code: str, message: str, subject: Node, *, unknown: bool = False
    ) -> None:
        """Bound the source identifier before retaining a scoped diagnostic."""
        bound_node(subject, self.budget)
        self.issues.append(
            Issue(
                code, message, str(subject), kind="unverified" if unknown else "finding"
            )
        )

    def extensions(
        self, subject: Resource, supported: frozenset[URIRef]
    ) -> tuple[Statement, ...]:
        """Retain source assertions without interpreting extension semantics."""
        result = []
        for triple in self.graph.triples((subject, None, None)):
            self.budget.consume("work")
            for node in triple:
                bound_node(node, self.budget)
            if triple[1] not in supported:
                reserve(self.budget)
                result.append(triple)
        return tuple(result)

    def form(self, resource: Resource) -> LexicalForm:
        """Read a form once, preserving same-lemma distinct resource identities."""
        if resource in self.forms:
            return self.forms[resource]
        written = []
        for value in self.values(resource, O.writtenRep):
            if not isinstance(value, Literal):
                self.issue(
                    "ontolex.written_invalid",
                    "A written representation must be a literal",
                    resource,
                )
                continue
            if not self.faithful:
                self.issue(
                    "ontolex.literal_unverified",
                    "Original written literal identity was not retained",
                    resource,
                    unknown=True,
                )
            reserve(self.budget)
            checked = check_literal(value, budget=self.budget)
            self.children.append(checked)
            retain_report(checked, self.budget, self.issues)
            reserve(self.budget)
            written.append(value)
        if self.policy.require_written and not written:
            self.issue(
                "ontolex.written_missing",
                "A selected form has no required written representation",
                resource,
            )
        types = self.values(resource, RDF.type)
        extra = self.extensions(resource, frozenset((RDF.type, O.writtenRep)))
        reserve(self.budget, 2)
        form = LexicalForm(resource, types, tuple(written), extra)
        self.forms[resource] = form
        return form

    def sense(self, resource: Resource) -> LexicalSense:
        """Resolve reference roles through the exact caller-selected vocabulary view."""
        if resource in self.senses:
            return self.senses[resource]
        references = []
        raw = self.values(resource, O.reference)
        if self.policy.require_references and not raw:
            self.issue(
                "ontolex.reference_missing",
                "A selected sense has no required ontology reference",
                resource,
            )
        if (
            self.policy.reference_maximum is not None
            and len(raw) > self.policy.reference_maximum
        ):
            self.issue(
                "ontolex.reference_count",
                "Reference assertions exceed the selected finite policy",
                resource,
            )
        for target in raw:
            if not isinstance(target, URIRef | BNode):
                self.issue(
                    "ontolex.reference_invalid",
                    "An ontology reference must be a resource",
                    resource,
                )
                continue
            declaration = None
            if isinstance(target, URIRef):
                bound_node(target, self.budget)
                declaration = self.view.lookup(str(target))
                if self.policy.require_references and declaration.status != "declared":
                    self.issue(
                        "ontolex.reference_dangling",
                        "Reference lacks a declaration in the selected vocabulary view",
                        resource,
                    )
            elif self.policy.require_references:
                self.issue(
                    "ontolex.reference_unverified",
                    "Anonymous ontology references lack cross-source identity",
                    resource,
                    unknown=True,
                )
            reserve(self.budget, 2)
            references.append(LexicalReference(target, declaration))
        maximum = self.policy.sense_owners_maximum
        if maximum is not None:
            owners: set[Node] = set()
            for owner in self.graph.subjects(O.sense, resource):
                self.budget.consume("work")
                bound_node(owner, self.budget)
                reserve(self.budget)
                owners.add(owner)
            if len(owners) > maximum:
                self.issue(
                    "ontolex.sense_owner_count",
                    "Sense owners exceed the selected physical-graph policy",
                    resource,
                )
        types = self.values(resource, RDF.type)
        extra = self.extensions(resource, frozenset((RDF.type, O.reference)))
        reserve(self.budget, 2)
        sense = LexicalSense(resource, types, tuple(references), extra)
        self.senses[resource] = sense
        return sense

    def entry(self, resource: Resource) -> LexicalEntry | None:
        """Require actual lexical typing; plain labels never manufacture an entry."""
        types = self.values(resource, RDF.type)
        if not any(value in _ENTRY_TYPES for value in types):
            self.issue(
                "ontolex.entry_unverified",
                "Selected resource has no supported explicit OntoLex entry type",
                resource,
                unknown=True,
            )
            return None
        links = []
        canonical = self.values(resource, O.canonicalForm)
        low, high = self.policy.canonical_minimum, self.policy.canonical_maximum
        if (
            low is not None
            and len(canonical) < low
            or high is not None
            and len(canonical) > high
        ):
            self.issue(
                "ontolex.canonical_count",
                "Canonical-form assertions violate the selected finite policy",
                resource,
            )
        for predicate in _FORM_LINKS:
            values = (
                canonical
                if predicate == O.canonicalForm
                else self.values(resource, predicate)
            )
            for value in values:
                if not isinstance(value, URIRef | BNode):
                    self.issue(
                        "ontolex.form_invalid",
                        "A form link must identify a resource",
                        resource,
                    )
                    continue
                form = self.form(value)
                reserve(self.budget)
                links.append(FormLink(predicate, form))
        senses = []
        for value in self.values(resource, O.sense):
            if not isinstance(value, URIRef | BNode):
                self.issue(
                    "ontolex.sense_invalid",
                    "A sense link must identify a resource",
                    resource,
                )
                continue
            sense = self.sense(value)
            reserve(self.budget)
            senses.append(sense)
        extra = self.extensions(resource, frozenset((RDF.type, *_FORM_LINKS, O.sense)))
        reserve(self.budget)
        return LexicalEntry(resource, types, tuple(links), tuple(senses), extra)


def read_lexicon(
    source: PreparedSource | Graph,
    entries: Sequence[Resource],
    *,
    vocabulary: VocabularyView,
    graph_name: GraphSelection | None = None,
    policy: LexicalPolicy = _DEFAULT_POLICY,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> Lexicon:
    """Read supplied asserted Core structure without importing or inferring axioms.

    Untyped resources and unsupported required semantics cannot verify. Optional
    extension statements and the entire original snapshot remain available.
    Defaults request existing forms' written terms and senses' named declared
    references, while arity rules require an explicit caller-selected policy.
    This is neither OntoLex OWL consistency nor complete linguistic validation.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    parsed: list[LexicalEntry] = []
    prepared = None
    selected_graph = None
    requested = 1
    complete = False
    try:
        if (
            not isinstance(vocabulary, VocabularyView)
            or type(policy) is not LexicalPolicy
        ):
            raise InputError(
                "Lexical reading requires a selected VocabularyView and typed policy"
            )
        for predicate in policy.required_extensions:
            bound_node(predicate, operation)
            if predicate not in _CORE:
                issues.append(
                    Issue(
                        "ontolex.extension_unverified",
                        "Required semantic extension is outside "
                        "the asserted Core profile",
                        str(predicate),
                        kind="unverified",
                    )
                )
        if not isinstance(entries, Sequence) or isinstance(
            cast(object, entries), str | bytes
        ):
            raise InputError("Select a finite entry resource sequence")
        reserve(operation, len(entries) + 1)
        selected: list[Resource] = []
        seen: set[Resource] = set()
        for index in range(len(entries)):
            operation.consume("work")
            value = entries[index]
            if not isinstance(value, URIRef | BNode):
                raise InputError("Selected entries must be RDF resources")
            bound_node(value, operation)
            if value in seen:
                raise InputError("Select each entry resource once")
            seen.add(value)
            selected.append(value)
        requested = max(1, len(selected))
        if not selected:
            raise InputError("Select a nonempty explicit entry population")
        if not isinstance(source, PreparedSource | Graph):
            raise InputError("Lexical reading requires supplied RDF")
        prepared = (
            source
            if isinstance(source, PreparedSource)
            else prepare_rdf(source, budget=operation)
        )
        if prepared.union or prepared.graph_fidelity != "complete":
            raise InputError("Lexical reading requires one complete physical graph")
        if isinstance(graph_name, BNode) and not any(
            name is graph_name for name in prepared._graph_names(budget=operation)
        ):
            raise InputError("Blank graph selection requires snapshot ownership")
        contexts = prepared._contexts(graph_name, budget=operation)
        _, selected_graph, graph = next(contexts)
        if next(contexts, None) is not None:
            raise InputError("Select exactly one physical lexical graph")
        blanks: set[int] = set()
        for triple in graph:
            operation.consume("work")
            operation.consume("triples")
            for node in triple:
                bound_node(node, operation)
                if isinstance(node, BNode):
                    reserve(operation)
                    blanks.add(id(node))
        reader = _Reader(
            graph,
            vocabulary,
            policy,
            operation,
            issues,
            children,
            prepared.literal_fidelity in ("source_terms", "supplied_terms"),
        )
        for resource in selected:
            if isinstance(resource, BNode) and id(resource) not in blanks:
                raise InputError("Anonymous entry requires exact source ownership")
            entry = reader.entry(resource)
            if entry is not None:
                reserve(operation)
                parsed.append(entry)
        complete = len(parsed) == len(selected)
    except LimitExceeded as error:
        for child in children:
            issues.retain_completed(child)
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(Issue("ontolex.input", str(error), kind="input_error"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    report = Report(
        "adapter.ontolex.read",
        len(parsed),
        tuple(issues),
        requested=requested,
        complete=complete,
        regime="selected-asserted-core",
        children=tuple(children),
        exclusions=(
            "owl-consistency",
            "unselected-linguistic-modules",
            "optional-extension-semantics",
        ),
    )
    return Lexicon._owned(prepared, selected_graph, tuple(parsed), policy, report)
