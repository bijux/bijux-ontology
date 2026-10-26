"""Owned actual JSON samples and their operation-local semantic composition."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from rdflib import Graph

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report, combine_reports
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.json.comparison import same_json
from bijux_ontology.json.values import capture_json, encode_json
from bijux_ontology.jsonld.contracts.expectations import ContextExpectation
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.jsonld.processing.resources import DocumentLoader
from bijux_ontology.jsonld.usages.checking import PreparedUsages, prepare_usages
from bijux_ontology.models.contracts.checking import check_models
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary

if TYPE_CHECKING:
    from bijux_ontology.models.corpus.types import _CorpusEvidence


def capture_context(context: object | Absent, budget: Budget) -> object | Absent:
    """Own an explicit context before any caller callback can change its inputs."""
    return (
        ABSENT
        if context is ABSENT
        else capture_json(
            context,
            max_bytes=budget.limits.max_bytes,
            max_depth=budget.limits.max_depth,
            budget=budget,
        ).value
    )


def compose_context(
    output: object,
    context: object | Absent,
    budget: Budget,
    *,
    conflict_message: str = "Supplied context conflicts with actual serializer output",
) -> object:
    """Compose already owned JSON without copying its nested values again."""
    if context is ABSENT:
        return output
    if isinstance(output, dict):
        if "@context" in output and not same_json(output["@context"], context, budget):
            raise InputError(conflict_message)
        budget.reserve_storage(256 + 72 * (len(output) + 1))
        return {**output, "@context": context}
    if isinstance(output, list):
        budget.reserve_storage(256 + 72 * 2)
        return {"@context": context, "@graph": output}
    raise InputError("An external context requires object or array output")


def contextualize_output(
    output: object, context: object | Absent, operation: Budget
) -> object:
    """Apply a caller context to owned output, retaining mandatory context capture."""
    return compose_context(output, capture_context(context, operation), operation)


def serialization_failure(error: Exception, operation: Budget) -> Report:
    """Keep failure reporting bounded without invoking caller formatting hooks."""
    exhausted = isinstance(error, (LimitExceeded, MemoryError))
    message = "Serialization or its contract checks failed"
    # Read the built-in exception storage, bypassing custom args/str properties.
    arguments = cast(tuple[object, ...], BaseException.__dict__["args"].__get__(error))
    if len(arguments) == 1 and type(arguments[0]) is str:
        text = arguments[0]
        try:
            bound_text(text, operation)
            if text.strip():
                message = text
        except LimitExceeded:
            exhausted = True
        except InputError:
            pass
    if exhausted and message == "Serialization or its contract checks failed":
        message = "Serialization stopped within its resource limits"
    return Report(
        "serialization.sample",
        requested=1,
        complete=False,
        issues=(
            Issue(
                "execution.limit_exceeded" if exhausted else "input.invalid",
                message,
                kind="exhausted" if exhausted else "input_error",
            ),
        ),
    )


class SampledOutput:
    """Retain one paid sample and its exact original operation preparation.

    Partial wire output survives a later context or semantic failure. This owns
    no serializer, reusable model plan or cached verdict; each caller supplies
    its actual output and checks it afresh.
    """

    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        self.output_json: str | None = None
        self.document_json: str | None = None
        self.document: object = ABSENT
        self.prepared: PreparedUsages | None = None
        self._output: object = ABSENT
        self._context: object | Absent = ABSENT

    def capture_output(self, output: object) -> object:
        """Own and encode the actual return value before publishing any wire output."""
        self._output = capture_json(
            output,
            max_bytes=self.budget.limits.max_bytes,
            max_depth=self.budget.limits.max_depth,
            budget=self.budget,
        ).value
        self.output_json = encode_json(self._output, budget=self.budget)
        return self._output

    def capture_context(self, context: object | Absent) -> None:
        """Keep context ownership at the adapter's existing callback boundary."""
        self._context = capture_context(context, self.budget)

    def apply_context(self) -> None:
        """Retain the contextual document without cloning already owned children."""
        self.document = compose_context(self._output, self._context, self.budget)
        self.document_json = (
            self.output_json
            if self.document is self._output
            else encode_json(self.document, budget=self.budget)
        )

    def prepare(
        self, *, base: str | None, document_loader: DocumentLoader | None
    ) -> PreparedUsages:
        """Prepare actual source observations once for adapter introspection."""
        prepared = prepare_usages(
            self.document,
            base=base,
            document_loader=document_loader,
            budget=self.budget,
        )
        self.prepared = prepared
        self.document = prepared._document
        return prepared

    def adopt_prepared(self, prepared: PreparedUsages) -> None:
        """Requalify an explicitly supplied receipt before its document is adopted."""
        if (
            not isinstance(prepared, PreparedUsages)
            or prepared._budget is not self.budget
        ):
            raise InputError("Prepared output must share its originating Budget")
        if encode_json(prepared._document, budget=self.budget) != self.document_json:
            raise InputError("Prepared output differs from the checked document")
        self.prepared = prepared
        self.document = prepared._document

    def check(
        self,
        models: Sequence[ModelDeclaration],
        usages: Sequence[ModelUsage],
        *,
        vocabulary: Vocabulary | Graph,
        declaration_report: Report | None = None,
        base: str | None = None,
        document_loader: DocumentLoader | None = None,
        rdf_direction: DirectionPolicy | None = None,
        _corpus: _CorpusEvidence | None = None,
        datatype_registry: DatatypeRegistry | None = None,
        context_expectations: Sequence[ContextExpectation] = (),
    ) -> Report:
        """Check declarations then actual emission under the one originating budget."""
        reports = [declaration_report] if declaration_report is not None else []
        try:
            vocabulary = (
                vocabulary
                if isinstance(vocabulary, Vocabulary)
                else load_vocabulary(vocabulary, budget=self.budget)
            )
            reports.append(
                check_models(
                    models,
                    vocabulary,
                    budget=self.budget,
                    rdf_direction=rdf_direction,
                    datatype_registry=datatype_registry,
                )
            )
            reports.append(
                check_emission(
                    self.document,
                    models,
                    usages,
                    vocabulary=vocabulary,
                    base=base,
                    document_loader=document_loader,
                    budget=self.budget,
                    rdf_direction=rdf_direction,
                    _corpus=_corpus,
                    datatype_registry=datatype_registry,
                    context_expectations=context_expectations,
                    _prepared=self.prepared,
                )
            )
        except Exception as error:
            reports.append(serialization_failure(error, self.budget))
        return combine_reports("serialization.sample", *reports)
