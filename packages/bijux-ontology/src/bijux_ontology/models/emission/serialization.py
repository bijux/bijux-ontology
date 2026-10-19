"""Sample real caller-owned serializer output against explicit semantic contracts."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from rdflib import Graph

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report, combine_reports
from bijux_ontology.execution.limits import Budget, Limits, bound_text
from bijux_ontology.json.values import encode_json, own_json
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.jsonld.processing.resources import DocumentLoader
from bijux_ontology.jsonld.usages.checking import PreparedUsages
from bijux_ontology.models.contracts.checking import check_models
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.context import same_context
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary

if TYPE_CHECKING:
    from bijux_ontology.models.corpus.types import _CorpusEvidence


@dataclass(frozen=True)
class InstanceCheck:
    """Owned sampled output and the exact contextual document that was checked.

    This result describes one actual serialization call, never all possible
    instances or future calls. Explicit options and contexts remain caller policy.
    """

    output_json: str | None
    document_json: str | None
    report: Report

    @property
    def output(self) -> object:
        """Export an independent copy of the actual serializer output."""
        return json.loads(self.output_json) if self.output_json is not None else ABSENT

    @property
    def document(self) -> object:
        """Export the checked document, including any explicitly supplied context."""
        return (
            json.loads(self.document_json) if self.document_json is not None else ABSENT
        )


def check_instance(
    instance: object,
    serializer: Callable[..., object],
    models: Sequence[ModelDeclaration],
    usages: Sequence[ModelUsage],
    *,
    vocabulary: Vocabulary | Graph,
    options: Mapping[str, object],
    context: object | Absent = ABSENT,
    declaration_report: Report | None = None,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    rdf_direction: DirectionPolicy | None = None,
    _corpus: _CorpusEvidence | None = None,
    datatype_registry: DatatypeRegistry | None = None,
) -> InstanceCheck:
    """Call the actual serializer once with exact options and check its output.

    Semantic preparation and checking share one operation budget. The caller's
    ordinary Python callback executes in process and keeps its own lifetime.
    """
    if declaration_report is not None and not isinstance(declaration_report, Report):
        raise InputError("declaration_report requires a Report")
    operation = _operation(limits, budget)
    try:
        if not isinstance(options, Mapping):
            raise InputError("Serializer options require a named mapping")
        count = len(options)
        operation.consume("states", count)
        operation.reserve_storage(256 + 72 * count)
        captured: dict[str, object] = {}
        for key in options:
            operation.consume("work")
            if len(captured) >= count:
                raise InputError("Serializer option inventory changed during capture")
            bound_text(key, operation)
            captured[key] = options[key]
        if len(captured) != count:
            raise InputError("Serializer option inventory changed during capture")
        supplied = own_json(
            captured,
            max_bytes=operation.limits.max_bytes,
            max_depth=operation.limits.max_depth,
            budget=operation,
        )
        if not isinstance(supplied, dict):
            raise InputError("Serializer options require a named mapping")
        output = serializer(instance, **supplied)
    except Exception as error:
        reports = () if declaration_report is None else (declaration_report,)
        return InstanceCheck(
            None,
            None,
            combine_reports(
                "serialization.sample", *reports, _failure(error, operation)
            ),
        )
    return check_serialized(
        output,
        models,
        usages,
        vocabulary=vocabulary,
        context=context,
        declaration_report=declaration_report,
        base=base,
        document_loader=document_loader,
        budget=operation,
        rdf_direction=rdf_direction,
        _corpus=_corpus,
        datatype_registry=datatype_registry,
    )


def _operation(limits: Limits | None, budget: Budget | None) -> Budget:
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Explicit limits conflict with the shared operation budget")
    return budget if budget is not None else Budget(limits)


def _failure(error: Exception, operation: Budget) -> Report:
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


def check_serialized(
    output: object,
    models: Sequence[ModelDeclaration],
    usages: Sequence[ModelUsage],
    *,
    vocabulary: Vocabulary | Graph,
    context: object | Absent = ABSENT,
    declaration_report: Report | None = None,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    rdf_direction: DirectionPolicy | None = None,
    _corpus: _CorpusEvidence | None = None,
    datatype_registry: DatatypeRegistry | None = None,
    context_expectations: Sequence[ContextExpectation] = (),
    _prepared: PreparedUsages | None = None,
) -> InstanceCheck:
    """Check one already serialized output using the shared semantic budget.

    The original output and contextualized document are owned separately. This
    qualifies this supplied sample only; no serializer call or universal output
    guarantee is inferred. Source and declaration diagnostics remain raw truth.
    Explicit context expectations reuse the emission operation's preparation.
    """
    if declaration_report is not None and not isinstance(declaration_report, Report):
        raise InputError("declaration_report requires a Report")
    operation = _operation(limits, budget)
    selected = operation.limits
    reports = [declaration_report] if declaration_report is not None else []
    output_json = document_json = None
    try:
        output = own_json(
            output,
            max_bytes=selected.max_bytes,
            max_depth=selected.max_depth,
            budget=operation,
        )
        output_json = encode_json(output, budget=operation)
        document = _contextualized_output(output, context, operation)
        if document is output:
            document_json = output_json
        else:
            document = own_json(
                document,
                max_bytes=selected.max_bytes,
                max_depth=selected.max_depth,
                budget=operation,
            )
            document_json = encode_json(document, budget=operation)
        if _prepared is not None:
            if (
                not isinstance(_prepared, PreparedUsages)
                or _prepared._budget is not operation
            ):
                raise InputError("Prepared output must share its originating Budget")
            exact = encode_json(_prepared._document, budget=operation)
            if exact != document_json:
                raise InputError("Prepared output differs from the checked document")
            document = _prepared._document
        vocabulary = (
            vocabulary
            if isinstance(vocabulary, Vocabulary)
            else load_vocabulary(vocabulary, budget=operation)
        )
        reports.append(
            check_models(
                models,
                vocabulary,
                budget=operation,
                rdf_direction=rdf_direction,
                datatype_registry=datatype_registry,
            )
        )
        reports.append(
            check_emission(
                document,
                models,
                usages,
                vocabulary=vocabulary,
                base=base,
                document_loader=document_loader,
                budget=operation,
                rdf_direction=rdf_direction,
                _corpus=_corpus,
                datatype_registry=datatype_registry,
                context_expectations=context_expectations,
                _prepared=_prepared,
            )
        )
    except Exception as error:
        reports.append(_failure(error, operation))
    return InstanceCheck(
        output_json, document_json, combine_reports("serialization.sample", *reports)
    )


def _contextualized_output(
    output: object, context: object | Absent, operation: Budget
) -> object:
    """Apply the existing explicit context wrapper without processing a context."""
    if context is ABSENT:
        return output
    owned_context = own_json(
        context,
        max_bytes=operation.limits.max_bytes,
        max_depth=operation.limits.max_depth,
        budget=operation,
    )
    if isinstance(output, dict):
        if "@context" in output and not same_context(
            output["@context"], owned_context, operation
        ):
            raise InputError("Supplied context conflicts with actual serializer output")
        operation.reserve_storage(256 + 72 * (len(output) + 1))
        return {**output, "@context": owned_context}
    if isinstance(output, list):
        operation.reserve_storage(256 + 72 * 2)
        return {"@context": owned_context, "@graph": output}
    raise InputError("An external context requires object or array output")
