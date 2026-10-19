"""Sample real caller-owned serializer output against explicit semantic contracts."""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from rdflib import Graph

from bijux_ontology._json import own_json
from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.jsonld.resources import DocumentLoader
from bijux_ontology.limits import Limits
from bijux_ontology.reports import Issue, Report, combine_reports
from bijux_ontology.vocabulary.view import Vocabulary

from .checking import check_models
from .emission import ModelUsage, check_emission


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
) -> InstanceCheck:
    """Call serializer(instance, **options) once and check its owned sample.

    Options such as mode, by_alias and exclude_none are explicit serializer inputs;
    this operation neither guesses framework defaults nor inspects serializer code.
    Supply extraction diagnostics as declaration_report to retain unknown mappings.
    A supplied context may contextualize an object or array but cannot override an
    existing conflicting context. Bindings address the returned checked document.
    Callbacks execute in-process and retain their caller-owned execution behavior.
    """
    if declaration_report is not None and not isinstance(declaration_report, Report):
        raise InputError("declaration_report requires a Report")
    selected = limits or Limits()
    reports = [declaration_report] if declaration_report is not None else []
    output_json = document_json = None
    try:
        supplied = own_json(
            dict(options), max_bytes=selected.max_bytes, max_depth=selected.max_depth
        )
        if not isinstance(supplied, dict):
            raise InputError("Serializer options require a named mapping")
        raw = serializer(instance, **supplied)
        output = own_json(
            raw, max_bytes=selected.max_bytes, max_depth=selected.max_depth
        )
        output_json = json.dumps(output, ensure_ascii=False, allow_nan=False)
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
        reports.append(check_models(models, vocabulary, limits=selected))
        reports.append(
            check_emission(
                document,
                models,
                usages,
                base=base,
                document_loader=document_loader,
                limits=selected,
            )
        )
    except Exception as error:
        # Caller serializers may raise framework-specific exceptions. Retain an
        # explicit failed sample without hiding earlier extraction findings.
        exhausted = isinstance(error, (LimitExceeded, MemoryError))
        reports.append(
            Report(
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
        )
    return InstanceCheck(
        output_json, document_json, combine_reports("serialization.sample", *reports)
    )
