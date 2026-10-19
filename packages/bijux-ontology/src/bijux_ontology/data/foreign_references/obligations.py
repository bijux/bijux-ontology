"""Operation-local exact target-model obligations over qualified owned records."""

from collections.abc import Sequence
from typing import cast

from rdflib import BNode, Graph

from bijux_ontology.data.keys.extraction import _Key
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.vocabulary.browsing.inspection import (
    VocabularyView,
    inspect_vocabulary,
)
from bijux_ontology.vocabulary.sources.view import Vocabulary

from .resolution import qualify_key


class TargetObligations:
    """Share one qualified report across exact source-target correlations only."""

    def __init__(
        self,
        models: Sequence[ModelDeclaration],
        vocabulary: Vocabulary | Graph | None,
        budget: Budget,
        issues: IssueCollector,
    ) -> None:
        self.budget = budget
        self.issues = issues
        self._supplied_models = models
        self._supplied_vocabulary = vocabulary
        self._models: tuple[ModelDeclaration, ...] | None = None
        self._vocabulary: VocabularyView | None = None
        self._completed: dict[tuple[object, ...], tuple[_Key, Report]] = {}

    def _prepare(self) -> None:
        """Own one finite immutable model selection and native vocabulary view."""
        if self._models is not None:
            return
        models = self._supplied_models
        if isinstance(cast(object, models), str | bytes) or not isinstance(
            models, Sequence
        ):
            raise InputError(
                "Foreign target models require a finite declaration sequence"
            )
        count = len(models)
        self.budget.consume("states", count)
        self.budget.consume("bytes", 64 + 16 * count)
        self.budget.consume("work", count)
        try:
            captured = tuple(models[index] for index in range(count))
        except IndexError as error:
            raise InputError(
                "Target model declarations changed during capture"
            ) from error
        vocabulary = self._supplied_vocabulary
        view = (
            inspect_vocabulary(vocabulary, budget=self.budget)
            if vocabulary is not None
            else None
        )
        self._models = captured
        self._vocabulary = view

    def check(self, key: _Key, model: str) -> Report:
        """Never reuse source identity as proof of current metadata or graph scope."""
        graph = qualify_key(key, self.budget)
        self._prepare()
        selected = key.reader.selected
        name = selected.graph_name
        graph_key = ("blank", id(name)) if isinstance(name, BNode) else name
        subject = key.record.subject
        subject_key = ("blank", id(subject)) if isinstance(subject, BNode) else subject
        identity = (
            model,
            id(key.record.source),
            id(graph),
            selected.kind,
            graph_key,
            subject_key,
        )
        self.budget.consume("work")
        completed = self._completed.get(identity)
        if completed is not None:
            return completed[1]
        self.budget.consume("states")
        self.budget.consume("bytes", 256)
        report = check_rdf_emission(
            key.record.source,
            self._models or (),
            (RdfModelUsage(model, subject, location=key.record.identifier),),
            vocabulary=self._vocabulary,
            graph_name=key.record.graph_name,
            union=key.record.union,
            budget=self.budget,
        )
        try:
            if qualify_key(key, self.budget) is not graph:
                raise InputError(
                    "Foreign target physical graph changed during checking"
                )
        except LimitExceeded:
            self.issues.retain_completed(report)
            raise
        # The retained key keeps the original source, reader, names and blanks alive.
        self._completed[identity] = key, report
        return report
