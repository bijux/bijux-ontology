"""Finite per-entity annotation coverage over explicitly selected source scopes."""

from collections.abc import Sequence
from typing import cast

from rdflib import Literal, URIRef
from rdflib.term import Node

from bijux_ontology.absence import Absent
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Edge, Selection

from ..languages.matching import matches_language
from .types import (
    AnnotationCoverage,
    AnnotationRequirement,
    LanguageCoverage,
    LanguageRequirement,
    _capture,
    _iri,
    _language,
    _text,
)


def _matches(value: Node, language: str | Absent, mode: str, budget: Budget) -> bool:
    budget.consume("work")
    if not isinstance(value, Literal):
        return False
    if isinstance(language, Absent):
        return value.language is None
    if mode == "exact":
        return value.language is not None and value.language.lower() == language.lower()
    return matches_language(
        value.language, language, extended=mode == "extended", budget=budget
    )


class _CoverageRead:
    """Keep operation-local reports and counters without another vocabulary index."""

    def __init__(self, view: VocabularyView, budget: Budget) -> None:
        self.view = view
        self.budget = budget
        self.issues = IssueCollector(budget)
        self.complete = True
        self.exhausted = False

    def count_issue(
        self,
        entity: URIRef,
        rule: AnnotationRequirement,
        count: int,
        *,
        language: LanguageRequirement | None = None,
        minimum: bool = False,
    ) -> None:
        """Emit only a proved count violation with its actual selected entity."""
        target = language if language is not None else rule
        bound = target.minimum if minimum else target.maximum
        # Hexadecimal evidence avoids interpreter decimal-conversion limits.
        if bound is not None and bound.bit_length() > 128:
            size = 2 + (bound.bit_length() + 3) // 4
            if size > self.budget.limits.max_bytes:
                raise LimitExceeded("Annotation count evidence exceeds max_bytes")
            self.budget.consume("bytes", size)
            bound_text = hex(bound)
        else:
            bound_text = str(bound)
        code = "annotations.language_" if language is not None else "annotations."
        code += "minimum_missing" if minimum else "maximum_exceeded"
        requested = (
            f"; language={language.language!s}; mode={language.mode}"
            if language is not None
            else ""
        )
        self.issues.append(
            Issue(
                code,
                "Selected annotations violate a declared count bound",
                str(entity),
                expected=f"{rule.predicate}{requested}; bound={bound_text}",
                actual=str(count),
            )
        )

    def valid(self, entity: URIRef, edge: Edge, rule: AnnotationRequirement) -> bool:
        """Check every original annotation object, including resource values."""
        value = edge.object
        # Owned native terms retain immutable string-backed lexical storage.
        _text(cast(str, value), self.budget)
        kind = (
            "literal"
            if isinstance(value, Literal)
            else "iri"
            if isinstance(value, URIRef)
            else "blank"
        )
        if kind not in rule.value_kinds:
            self.issues.append(
                Issue(
                    "annotations.value_kind",
                    "Annotation has a forbidden value kind",
                    str(entity),
                    expected=f"{rule.predicate}: {rule.value_kinds}",
                    actual=value.n3(),
                )
            )
            return False
        if isinstance(value, Literal) and rule.datatypes:
            datatype = term_key(value)[3]
            if datatype not in rule.datatypes:
                self.issues.append(
                    Issue(
                        "annotations.datatype",
                        "Annotation has a forbidden datatype",
                        str(entity),
                        expected=f"{rule.predicate}: {rule.datatypes}",
                        actual=str(datatype),
                    )
                )
                return False
        return True

    def requirement(
        self, entity: URIRef, rule: AnnotationRequirement
    ) -> AnnotationCoverage:
        """Retain known failures and raw values even if later work is exhausted."""
        selected = self.view.statements(
            entity, predicates=(rule.predicate,), budget=self.budget
        )
        list.extend(self.issues, selected.report.issues)
        complete = selected.report.complete
        self.exhausted |= not complete
        seen: set[tuple[str | None, ...]] = set()
        valid: list[Edge] = []
        primary: list[list[Edge]] = []
        languages: list[LanguageCoverage] = []
        try:
            self.budget.consume("states", len(rule.languages))
            primary = [[] for _ in rule.languages]
            for edge in selected.items:
                self.budget.consume("work")
                key = term_key(edge.object)
                if key in seen:
                    continue
                self.budget.consume("states")
                seen.add(key)
                if not self.valid(entity, edge, rule):
                    continue
                self.budget.consume("states")
                valid.append(edge)
                if rule.maximum is not None and len(valid) == rule.maximum + 1:
                    self.count_issue(entity, rule, len(valid))
                for index, language in enumerate(rule.languages):
                    if _matches(
                        edge.object, language.language, language.mode, self.budget
                    ):
                        self.budget.consume("states")
                        primary[index].append(edge)
                        if (
                            language.maximum is not None
                            and len(primary[index]) == language.maximum + 1
                        ):
                            self.count_issue(
                                entity, rule, len(primary[index]), language=language
                            )
            if complete and len(valid) < rule.minimum:
                self.count_issue(entity, rule, len(valid), minimum=True)
            for language, values in zip(rule.languages, primary, strict=True):
                chosen: str | Absent | None = language.language if values else None
                fallback = False
                if not values and complete:
                    for alternative in language.fallback:
                        alternatives = []
                        for edge in valid:
                            if _matches(
                                edge.object, alternative, language.mode, self.budget
                            ):
                                self.budget.consume("states")
                                alternatives.append(edge)
                                if (
                                    language.maximum is not None
                                    and len(alternatives) == language.maximum + 1
                                ):
                                    self.count_issue(
                                        entity,
                                        rule,
                                        len(alternatives),
                                        language=language,
                                    )
                        if alternatives:
                            values, chosen, fallback = alternatives, alternative, True
                            break
                if complete and len(values) < language.minimum:
                    self.count_issue(
                        entity, rule, len(values), language=language, minimum=True
                    )
                self.budget.consume("states")
                languages.append(
                    LanguageCoverage(
                        language, chosen, fallback, tuple(values), complete
                    )
                )
        except LimitExceeded as error:
            complete = False
            self.exhausted = True
            self.issues.exhausted(str(error), location=str(entity))
        self.complete &= complete
        return AnnotationCoverage(
            entity, URIRef(rule.predicate), selected.items, tuple(languages), complete
        )


def check_annotation_coverage(
    view: VocabularyView,
    requirements: Sequence[AnnotationRequirement],
    *,
    entities: Sequence[str] | None = None,
    kinds: Sequence[str] = (),
    namespaces: Sequence[str] = (),
    budget: Budget | None = None,
) -> Selection[AnnotationCoverage]:
    """Check explicit requirements over named entities or asserted kind selection.

    The supplied view fixes local/import/lookup scope. Explicit entities and
    kind/namespace selectors are mutually exclusive. Values count as distinct
    exact RDF terms; repeated provenance does not inflate cardinality. Languages
    count permitted literal values only. Fallback is explicitly reported and
    never supplied by lookup. Missing minima require completed inventories;
    known shape and maximum violations survive later incompleteness. This adds
    no default ontology/SKOS annotation convention or datatype lexical regime.
    """
    if (
        not isinstance(view, VocabularyView)
        or budget is not None
        and not isinstance(budget, Budget)
    ):
        raise InputError("Expected an owned VocabularyView and optional Budget")
    operation = budget if budget is not None else Budget(view.limits)
    read = _CoverageRead(view, operation)
    results = []
    requested = 0
    try:
        rules = _capture(requirements, operation)
        if not rules or any(
            not isinstance(rule, AnnotationRequirement) for rule in rules
        ):
            raise InputError("Select a nonempty typed annotation requirement inventory")
        if len({rule.predicate for rule in rules}) != len(rules):
            raise InputError("Annotation predicate has multiple selected requirements")
        for rule in rules:
            _iri(rule.predicate, operation)
            for datatype in rule.datatypes:
                _iri(datatype, operation)
            for language in rule.languages:
                _language(language.language, language.mode, operation)
                for alternative in language.fallback:
                    _language(alternative, language.mode, operation)
        selected_kinds = _capture(kinds, operation)
        selected_namespaces = _capture(namespaces, operation)
        for value in (*selected_kinds, *selected_namespaces):
            _iri(value, operation)
        if entities is not None:
            if selected_kinds or selected_namespaces:
                raise InputError("Choose explicit entities or kind/namespace selection")
            selected_entities = _capture(entities, operation)
            for value in selected_entities:
                _iri(value, operation)
            if len(set(selected_entities)) != len(selected_entities):
                raise InputError("Annotation entity selection requires unique IRIs")
        else:
            selection = view.terms(
                kinds=selected_kinds, namespaces=selected_namespaces, budget=operation
            )
            list.extend(read.issues, selection.report.issues)
            read.complete &= selection.report.complete
            selected_entities = tuple(term.iri for term in selection.items)
        requested = len(selected_entities) * len(rules)
        for iri in selected_entities:
            operation.consume("work")
            if view.lookup(iri).status == "absent":
                read.complete = False
                read.issues.append(
                    Issue(
                        "annotations.entity_unresolved",
                        "Selected entity is absent from supplied scope",
                        iri,
                        kind="unverified",
                    )
                )
                continue
            entity = URIRef(iri)
            for rule in rules:
                operation.consume("states")
                result = read.requirement(entity, rule)
                results.append(result)
                if read.exhausted:
                    raise LimitExceeded(
                        "Annotation coverage stopped before completing selected scope"
                    )
    except LimitExceeded as error:
        read.complete = False
        read.issues.exhausted(str(error))
    return Selection(
        tuple(results),
        Report(
            "annotations.coverage",
            len(results),
            tuple(read.issues),
            allow_empty=True,
            complete=read.complete,
            requested=max(requested, len(results) + (not read.complete)),
            families=("annotation-coverage",),
            regime="asserted",
        ),
    )
