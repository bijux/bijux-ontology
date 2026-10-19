"""Finite vocabulary facts and ownership changes, without compatibility guesses."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal as Kind

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.vocabulary.anonymous.comparison import compare_anonymous
from bijux_ontology.vocabulary.anonymous.types import AnonymousDifferences
from bijux_ontology.vocabulary.browsing.inspection import (
    VocabularyView,
    inspect_vocabulary,
)
from bijux_ontology.vocabulary.browsing.results import TermInfo
from bijux_ontology.vocabulary.facts.comparison import compare_selected_facts
from bijux_ontology.vocabulary.facts.types import FactSelection, SelectedFactDifferences
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.types import source_role
from bijux_ontology.vocabulary.sources.view import Vocabulary

ANNOTATIONS = (
    str(RDFS.label),
    str(SKOS.prefLabel),
    str(SKOS.altLabel),
    str(SKOS.hiddenLabel),
    str(SKOS.definition),
    str(SKOS.notation),
    str(OWL.deprecated),
)
_STRUCTURE = frozenset(
    (
        RDFS.subClassOf,
        RDFS.subPropertyOf,
        RDFS.domain,
        RDFS.range,
        OWL.equivalentClass,
        OWL.equivalentProperty,
        OWL.inverseOf,
        SKOS.broader,
        SKOS.narrower,
        SKOS.broaderTransitive,
        SKOS.narrowerTransitive,
        SKOS.related,
        SKOS.broadMatch,
        SKOS.narrowMatch,
        SKOS.relatedMatch,
        SKOS.closeMatch,
        SKOS.exactMatch,
        SKOS.inScheme,
        SKOS.topConceptOf,
        SKOS.hasTopConcept,
        SKOS.member,
        SKOS.memberList,
    )
)
_DISJOINT = frozenset((OWL.disjointWith, OWL.propertyDisjointWith))
_CHARACTERISTICS = frozenset(
    (
        OWL.FunctionalProperty,
        OWL.InverseFunctionalProperty,
        OWL.TransitiveProperty,
        OWL.SymmetricProperty,
        OWL.AsymmetricProperty,
        OWL.ReflexiveProperty,
        OWL.IrreflexiveProperty,
    )
)
_LEGACY = frozenset((OWL.DeprecatedClass, OWL.DeprecatedProperty))


ChangeKind = Kind[
    "term-added",
    "term-removed",
    "kinds",
    "annotation",
    "deprecation",
    "structure",
    "disjointness",
    "characteristics",
    "imports",
    "scopes",
    "anonymous",
]


@dataclass(frozen=True)
class Change:
    """Before/after supplied facts, retaining RDF terms and source evidence.

    Anonymous changes deliberately have no fabricated named owner or literal
    values: their evidence is the maintained anonymous-structure comparison.
    """

    kind: ChangeKind
    iri: str
    predicate: str | None = None
    before: tuple[Node, ...] = ()
    after: tuple[Node, ...] = ()
    before_scopes: tuple[str, ...] = ()
    after_scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class VocabularyChanges:
    """Selected differences and their completed scope, not an upgrade verdict."""

    changes: tuple[Change, ...]
    report: Report
    selected_facts: SelectedFactDifferences | None = field(default=None, kw_only=True)
    anonymous: AnonymousDifferences | None = field(default=None, kw_only=True)


def selected_view(
    source: Vocabulary | Graph | PreparedSource | VocabularyView,
    scopes: Sequence[str],
    limits: Limits | None,
    *,
    budget: Budget | None = None,
) -> VocabularyView:
    """Use only the requested roles; a preselected view must not silently widen them."""
    if isinstance(source, VocabularyView):
        if {source_role(scope) for scope in source.summary().scopes} - set(scopes):
            raise InputError(
                "Prepared view includes roles outside the requested change scope"
            )
        return source
    if isinstance(source, PreparedSource):
        source = load_vocabulary(source, limits=limits)
    return inspect_vocabulary(source, scopes=scopes, limits=limits, budget=budget)


def _inventory(
    view: VocabularyView, budget: Budget, issues: IssueCollector
) -> dict[str, TermInfo] | None:
    selected = view.terms(budget=budget)
    list.extend(issues, selected.report.issues)
    if not selected.report.complete:
        return None
    return {term.iri: term for term in selected.items}


@dataclass
class _FactValue:
    """One exact object and every selected source asserting it."""

    value: Node
    scopes: set[str]


FactKey = tuple[ChangeKind, str, str]
Facts = dict[FactKey, dict[tuple[str | None, ...], _FactValue]]


def _facts(
    view: VocabularyView,
    annotations: Sequence[str],
    budget: Budget,
    issues: IssueCollector,
) -> Facts | None:
    selected = view.all_statements(budget=budget)
    list.extend(issues, selected.report.issues)
    if not selected.report.complete:
        return None
    facts: Facts = {}
    for edge in selected.items:
        budget.consume("work")
        subject, predicate, value = edge.subject, edge.predicate, edge.object
        if isinstance(subject, BNode) or isinstance(value, BNode):
            continue
        if not isinstance(subject, URIRef):
            continue
        kind: ChangeKind | None = (
            "deprecation"
            if predicate == OWL.deprecated
            else "annotation"
            if str(predicate) in annotations
            else "structure"
            if predicate in _STRUCTURE
            else "disjointness"
            if predicate in _DISJOINT
            else "imports"
            if predicate == OWL.imports
            else "characteristics"
            if predicate == RDF.type and value in _CHARACTERISTICS
            else "deprecation"
            if predicate == RDF.type and value in _LEGACY
            else None
        )
        if kind is None:
            continue
        owner, target = subject, value
        if kind == "disjointness" and isinstance(value, URIRef) and value < subject:
            owner, target = value, subject
        values = facts.setdefault((kind, str(owner), str(predicate)), {})
        values.setdefault(term_key(target), _FactValue(target, set())).scopes.add(
            edge.scope
        )
    return facts


def _fact_scope(values: dict[tuple[str | None, ...], _FactValue]) -> tuple[str, ...]:
    """Retain actual source evidence, independently of unordered role comparison."""
    return tuple(sorted({scope for value in values.values() for scope in value.scopes}))


def _fact_identity(
    values: dict[tuple[str | None, ...], _FactValue],
) -> dict[tuple[str | None, ...], frozenset[str]]:
    """Import positions are provenance; only actual source roles affect identity."""
    return {
        key: frozenset(source_role(scope) for scope in value.scopes)
        for key, value in values.items()
    }


def compare_vocabularies(
    before: Vocabulary | Graph | PreparedSource | VocabularyView,
    after: Vocabulary | Graph | PreparedSource | VocabularyView,
    *,
    scopes: Sequence[str] = ("local",),
    annotation_predicates: Sequence[str] = ANNOTATIONS,
    fact_selection: FactSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> VocabularyChanges:
    """Compare declared inventories, exact selected facts and anonymous structure.

    The default local scope prevents lookup copies from concealing owned losses.
    Prefix spelling and import-array order never create new RDF identifiers.
    Raw differences are informational; compatibility needs consumer contracts.
    """
    if (
        not isinstance(scopes, Sequence)
        or isinstance(scopes, (str, bytes))
        or not scopes
        or any(not isinstance(scope, str) for scope in scopes)
        or set(scopes) - {"local", "imports", "lookup"}
    ):
        raise InputError("Select supported nonempty source roles")
    if not isinstance(annotation_predicates, Sequence) or isinstance(
        annotation_predicates, (str, bytes)
    ):
        raise InputError("Annotation predicates must be an explicit sequence")
    for iri in annotation_predicates:
        absolute_iri(iri)
    if fact_selection is not None and not isinstance(fact_selection, FactSelection):
        raise InputError("Expected an explicit FactSelection")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Explicit limits conflict with the shared operation budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    changes: list[Change] = []

    def retain(change: Change) -> None:
        """Reserve result capacity before retaining each completed difference."""
        operation.consume("states")
        issues.append(
            Issue(
                "vocabulary.fact_changed",
                "Selected supplied RDF facts differ",
                change.iri,
                severity="info",
                actual=change.kind,
            )
        )
        changes.append(change)

    complete = True
    checked = 0
    selected_facts = None
    anonymous = None
    try:
        if (
            fact_selection is not None or isinstance(before, PreparedSource)
        ) and not isinstance(before, Vocabulary | VocabularyView):
            before = load_vocabulary(before, budget=operation)
        if (
            fact_selection is not None or isinstance(after, PreparedSource)
        ) and not isinstance(after, Vocabulary | VocabularyView):
            after = load_vocabulary(after, budget=operation)
        if fact_selection is not None:
            selected_facts = compare_selected_facts(
                before, after, fact_selection, budget=operation
            )
            checked += selected_facts.report.checked
            if selected_facts.report.outcome == "exhausted":
                raise LimitExceeded("Selected fact comparison exhausted its allowance")
        left = selected_view(
            before,
            scopes,
            operation.limits,
            budget=operation if fact_selection is not None else None,
        )
        right = selected_view(
            after,
            scopes,
            operation.limits,
            budget=operation if fact_selection is not None else None,
        )
        previous = _inventory(left, operation, issues)
        current = _inventory(right, operation, issues)
        if previous is None or current is None:
            return VocabularyChanges(
                (),
                Report(
                    "vocabulary.changes",
                    checked,
                    tuple(issues),
                    allow_empty=True,
                    requested=checked + 1,
                    complete=False,
                    regime="asserted-scoped-facts",
                    families=("vocabulary.changes",),
                    children=(selected_facts.report,)
                    if selected_facts is not None
                    else (),
                ),
                selected_facts=selected_facts,
            )
        for iri in sorted(previous.keys() | current.keys()):
            operation.consume("work")
            checked += 1
            old = previous.get(iri)
            new = current.get(iri)
            if old is None and new is not None:
                retain(
                    Change(
                        "term-added",
                        iri,
                        after=tuple(URIRef(kind) for kind in new.kinds),
                        after_scopes=new.scopes,
                    )
                )
            elif new is None and old is not None:
                retain(
                    Change(
                        "term-removed",
                        iri,
                        before=tuple(URIRef(kind) for kind in old.kinds),
                        before_scopes=old.scopes,
                    )
                )
            elif old is not None and new is not None:
                old_kinds = tuple(
                    URIRef(kind)
                    for kind in old.kinds
                    if URIRef(kind) not in _CHARACTERISTICS | _LEGACY
                )
                new_kinds = tuple(
                    URIRef(kind)
                    for kind in new.kinds
                    if URIRef(kind) not in _CHARACTERISTICS | _LEGACY
                )
                if old_kinds != new_kinds:
                    retain(
                        Change(
                            "kinds",
                            iri,
                            before=old_kinds,
                            after=new_kinds,
                            before_scopes=old.scopes,
                            after_scopes=new.scopes,
                        )
                    )
                if {source_role(scope) for scope in old.scopes} != {
                    source_role(scope) for scope in new.scopes
                }:
                    retain(
                        Change(
                            "scopes",
                            iri,
                            before_scopes=old.scopes,
                            after_scopes=new.scopes,
                        )
                    )
        old_selected = _facts(left, annotation_predicates, operation, issues)
        new_selected = _facts(right, annotation_predicates, operation, issues)
        if old_selected is None or new_selected is None:
            raise LimitExceeded("Selected fact inventory could not be completed")
        old_facts, new_facts = old_selected, new_selected
        for key in sorted(old_facts.keys() | new_facts.keys()):
            operation.consume("work")
            checked += 1
            old_values, new_values = old_facts.get(key, {}), new_facts.get(key, {})
            if _fact_identity(old_values) != _fact_identity(new_values):
                kind, iri, predicate = key
                retain(
                    Change(
                        kind if old_values.keys() != new_values.keys() else "scopes",
                        iri,
                        predicate,
                        tuple(
                            old_values[value].value
                            for value in sorted(old_values, key=repr)
                        ),
                        tuple(
                            new_values[value].value
                            for value in sorted(new_values, key=repr)
                        ),
                        _fact_scope(old_values),
                        _fact_scope(new_values),
                    )
                )
        anonymous = compare_anonymous(left, right, budget=operation)
        checked += anonymous.report.checked
        if anonymous.changes:
            retain(Change("anonymous", ""))
        if not anonymous.report.complete:
            issues.extend(anonymous.report.issues)
            complete = False
        # One empty-to-empty inventory/fact comparison is still actual work.
        if not checked:
            checked = 1
    except LimitExceeded as error:
        issues.exhausted(str(error), location="vocabulary.changes")
        complete = False
    return VocabularyChanges(
        tuple(changes),
        Report(
            "vocabulary.changes",
            checked,
            tuple(issues),
            allow_empty=True,
            requested=checked if complete else checked + 1,
            complete=complete,
            regime="asserted-scoped-facts",
            families=("vocabulary.changes",),
            children=(
                *((selected_facts.report,) if selected_facts is not None else ()),
                *((anonymous.report,) if anonymous is not None else ()),
            ),
        ),
        selected_facts=selected_facts,
        anonymous=anonymous,
    )
