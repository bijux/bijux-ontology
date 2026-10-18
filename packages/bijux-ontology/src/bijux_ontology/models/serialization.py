"""Sample real caller-owned serializer output against explicit semantic contracts."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rdflib import Graph

from bijux_ontology._json import own_json
from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.jsonld.resources import DocumentLoader
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.reports import Issue, Report, combine_reports
from bijux_ontology.vocabulary.view import Vocabulary

from .checking import check_models
from .emission import ModelUsage, check_emission

if TYPE_CHECKING:
    from .corpus.types import _CorpusEvidence


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
    _corpus: _CorpusEvidence | None = None,
) -> InstanceCheck:
    """Call the actual serializer once with exact options and check its output.

    Semantic preparation and checking share one operation budget. The caller's
    ordinary Python callback executes in process and keeps its own lifetime.
    """
    if declaration_report is not None and not isinstance(declaration_report, Report):
        raise InputError("declaration_report requires a Report")
    operation = _operation(limits, budget)
    try:
        supplied = own_json(
            dict(options),
            max_bytes=operation.limits.max_bytes,
            max_depth=operation.limits.max_depth,
        )
        if not isinstance(supplied, dict):
            raise InputError("Serializer options require a named mapping")
        operation.consume("bytes", len(json.dumps(supplied).encode("utf-8")))
        output = serializer(instance, **supplied)
    except Exception as error:
        reports = () if declaration_report is None else (declaration_report,)
        return InstanceCheck(
            None,
            None,
            combine_reports("serialization.sample", *reports, _failure(error)),
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
        _corpus=_corpus,
    )


def _operation(limits: Limits | None, budget: Budget | None) -> Budget:
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Explicit limits conflict with the shared operation budget")
    return budget if budget is not None else Budget(limits)


def _failure(error: Exception) -> Report:
    exhausted = isinstance(error, (LimitExceeded, MemoryError))
    return Report(
        "serialization.sample",
        requested=1,
        complete=False,
        issues=(
            Issue(
                "execution.limit_exceeded" if exhausted else "input.invalid",
                f"{type(error).__name__}: {error}",
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
    _corpus: _CorpusEvidence | None = None,
) -> InstanceCheck:
    """Check one already serialized output using the shared semantic budget.

    The original output and contextualized document are owned separately. This
    qualifies this supplied sample only; no serializer call or universal output
    guarantee is inferred. Source and declaration diagnostics remain raw truth.
    """
    if declaration_report is not None and not isinstance(declaration_report, Report):
        raise InputError("declaration_report requires a Report")
    operation = _operation(limits, budget)
    selected = operation.limits
    reports = [declaration_report] if declaration_report is not None else []
    output_json = document_json = None
    try:
        output = own_json(
            output, max_bytes=selected.max_bytes, max_depth=selected.max_depth
        )
        output_json = json.dumps(output, ensure_ascii=False, allow_nan=False)
        operation.consume("bytes", len(output_json.encode("utf-8")))
        document = output
        if context is not ABSENT:
            owned_context = own_json(
                context, max_bytes=selected.max_bytes, max_depth=selected.max_depth
            )
            if isinstance(output, dict):
                if "@context" in output and output["@context"] != owned_context:
                    raise InputError(
                        "Supplied context conflicts with actual serializer output"
                    )
                document = {**output, "@context": owned_context}
            elif isinstance(output, list):
                document = {"@context": owned_context, "@graph": output}
            else:
                raise InputError("An external context requires object or array output")
        document = own_json(
            document, max_bytes=selected.max_bytes, max_depth=selected.max_depth
        )
        document_json = json.dumps(document, ensure_ascii=False, allow_nan=False)
        reports.append(check_models(models, vocabulary, budget=operation))
        reports.append(
            check_emission(
                document,
                models,
                usages,
                base=base,
                document_loader=document_loader,
                budget=operation,
                _corpus=_corpus,
            )
        )
    except Exception as error:
        reports.append(_failure(error))
    return InstanceCheck(
        output_json, document_json, combine_reports("serialization.sample", *reports)
    )
