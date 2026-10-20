"""Sample real caller-owned serializer output against explicit semantic contracts."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rdflib import Graph

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.declarations.bindings import ContractEvidence
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report, combine_reports
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import Budget, Limits, bound_text
from bijux_ontology.json.values import own_json
from bijux_ontology.jsonld.contracts.expectations import ContextExpectation
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.jsonld.processing.resources import DocumentLoader
from bijux_ontology.jsonld.usages.checking import PreparedUsages
from bijux_ontology.models.emission.observations import (
    SerializedFieldBinding,
    SerializedObservations,
    observe_serialized_bindings,
)
from bijux_ontology.models.emission.samples import SampledOutput, serialization_failure
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry
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
    observed: SerializedObservations | None = None

    @property
    def contract_evidence(self) -> ContractEvidence:
        """Expose actual binding receipts without replacing independent output truth."""
        return ContractEvidence(
            self.report, self.observed.bindings if self.observed is not None else ()
        )

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
                "serialization.sample",
                *reports,
                serialization_failure(error, operation),
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
    bindings: Sequence[SerializedFieldBinding] = (),
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
    sample = SampledOutput(operation)
    observed = None
    try:
        sample.capture_output(output)
        sample.capture_context(context)
        sample.apply_context()
        if _prepared is not None:
            sample.adopt_prepared(_prepared)
        selected_bindings = (
            ()
            if type(bindings) is tuple and not bindings
            else capture_items(bindings, SerializedFieldBinding, operation)
        )
        if selected_bindings:
            intended = capture_items(models, ModelDeclaration, operation)
            usages = capture_items(usages, ModelUsage, operation)
            operation.consume("states", len(usages))
            operation.reserve_storage(128 + 64 * len(usages))
            selected_usages = set(usages)
            for binding in selected_bindings:
                operation.consume("work")
                if binding.usage not in selected_usages:
                    raise InputError(
                        "An observed binding requires its independent model usage"
                    )
            prepared = (
                sample.prepared
                if sample.prepared is not None
                else sample.prepare(base=base, document_loader=document_loader)
            )
            document_loader = prepared._loader
            observed = observe_serialized_bindings(
                prepared,
                intended,
                selected_bindings,
                budget=operation,
                datatype_registry=datatype_registry,
                rdf_direction=rdf_direction,
            )
            models = intended
        report = sample.check(
            models,
            usages,
            vocabulary=vocabulary,
            declaration_report=declaration_report,
            base=base,
            document_loader=document_loader,
            rdf_direction=rdf_direction,
            _corpus=_corpus,
            datatype_registry=datatype_registry,
            context_expectations=context_expectations,
        )
    except Exception as error:
        reports = () if declaration_report is None else (declaration_report,)
        report = combine_reports(
            "serialization.sample", *reports, serialization_failure(error, operation)
        )
    if observed is not None:
        report = combine_reports("serialization.sample", report, observed.report)
    return InstanceCheck(sample.output_json, sample.document_json, report, observed)
