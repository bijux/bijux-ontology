"""Check already serialized output against imported entity and source contracts."""

import json
from collections.abc import Sequence
from dataclasses import replace
from typing import cast

from rdflib import BNode, Graph, Literal
from rdflib.namespace import RDF

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.json.paths import at_path, bound_path
from bijux_ontology.json.values import own_json
from bijux_ontology.jsonld.contracts.expectations import ContextExpectation
from bijux_ontology.jsonld.evidence.entities import object_key
from bijux_ontology.jsonld.processing.resources import DocumentLoader
from bijux_ontology.jsonld.processing.runtime import processor
from bijux_ontology.jsonld.usages.checking import prepare_usages
from bijux_ontology.models.emission.bindings import source_entity
from bijux_ontology.models.emission.samples import (
    contextualize_output,
    serialization_failure,
)
from bijux_ontology.models.emission.serialization import (
    InstanceCheck,
    check_serialized,
)
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.vocabulary.sources.view import Vocabulary

from .types import ShapeContractImport


def check_imported_output(
    output: object,
    imported: ShapeContractImport,
    usages: Sequence[ModelUsage],
    *,
    vocabulary: Vocabulary | Graph,
    context: object | Absent = ABSENT,
    types: tuple[str, ...] = (),
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> InstanceCheck:
    """Check one actual output with joined RDF counts and explicit key meanings.

    The existing checker prepares the contextualized document once. Entity
    usages cover every same-subject, same-graph contribution, including aliases
    and split descriptions. Present ordinary keys get explicit mapping checks;
    omitted/null/empty keys do not acquire inferred source-presence obligations.
    The finite import profile does not represent JSON-LD list containers.
    ``types`` supplies independent exact application types, never shape targets.
    The caller invokes its real serializer separately with its actual options.
    """
    if not isinstance(imported, ShapeContractImport):
        raise InputError("Expected an actual ShapeContractImport")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Expected Limits")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Imported output limits conflict with the shared Budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    try:
        retain_report(imported._translation_report, operation, issues)
        if type(types) is not tuple:
            raise InputError("Exact application types require a finite tuple")
        operation.consume("states", len(types))
        for iri in types:
            if not isinstance(iri, str) or isinstance(iri, BNode | Literal):
                raise InputError("Exact application types require IRI strings")
            bound_text(iri, operation)
            absolute_iri(iri)
        declarations = imported.extraction.declarations
        if not declarations:
            return InstanceCheck(None, None, imported.rdf_contract_report)
        if len(declarations) != 1:
            raise InputError("A shape import requires one exact model identity")
        original = declarations[0]
        operation.consume("states", 1 + len(original.fields))
        for field in original.fields:
            operation.consume("work")
            for text in (field.name, field.key, field.predicate):
                if text is not None:
                    bound_text(text, operation)
        model = replace(original, types=tuple(str(iri) for iri in types))
        owned = own_json(
            output,
            max_bytes=operation.limits.max_bytes,
            max_depth=operation.limits.max_depth,
        )
        encoded = json.dumps(owned, ensure_ascii=False, allow_nan=False)
        bound_text(encoded, operation)
        selected = contextualize_output(owned, context, operation)
        processor()
        prepared = prepare_usages(
            selected, base=base, document_loader=document_loader, budget=operation
        )
        if not isinstance(usages, Sequence) or isinstance(
            cast(object, usages), str | bytes
        ):
            raise InputError("Imported output usages require a finite sequence")
        count = len(usages)
        operation.consume("states", 2 * count)
        operation.consume("bytes", 32 * count)
        entities: list[ModelUsage] = []
        expectations: list[ContextExpectation] = []
        for index in range(count):
            operation.consume("work")
            try:
                usage = usages[index]
            except IndexError as error:
                raise InputError(
                    "Output usage inventory changed during capture"
                ) from error
            if (
                not isinstance(usage, ModelUsage)
                or usage.model != original.identifier
                or usage.reference is not None
            ):
                raise InputError(
                    "Imported output usages require an actual source-object "
                    "model binding"
                )
            bound_path(usage.path, operation)
            bound_text(usage.model, operation)
            entities.append(replace(usage, entity=True))
            try:
                node = at_path(selected, usage.path)
            except (KeyError, IndexError) as error:
                raise InputError(
                    "No actual output source object exists at the binding"
                ) from error
            if not isinstance(node, dict):
                raise InputError("Imported output bindings select source objects")
            entity = source_entity(node, usage.path, prepared, operation)
            description = prepared.entities.get(entity) if entity is not None else None
            if description is None:
                issues.append(
                    Issue(
                        "adapter.shape_binding_unverified",
                        "The selected output entity was not established",
                        kind="unverified",
                    )
                )
                return InstanceCheck(
                    encoded,
                    None,
                    Report(
                        "shacl-imported-output",
                        0,
                        tuple(issues),
                        requested=1,
                        complete=False,
                    ),
                )
            for fact in description.facts:
                operation.consume("work")
                for field in model.fields:
                    operation.consume("work")
                    if (
                        fact.predicate != field.predicate
                        or fact.reverse != field.reverse
                    ):
                        continue
                    target = object_key(fact)
                    nested = (
                        prepared.entities.get(target) if target is not None else None
                    )
                    operation.consume(
                        "work", 0 if nested is None else len(nested.facts)
                    )
                    if (
                        fact.container == "list"
                        or fact.traits.identifier == str(RDF.nil)
                        or nested is not None
                        and any(
                            item.predicate in (str(RDF.first), str(RDF.rest))
                            for item in nested.facts
                        )
                    ):
                        issues.append(
                            Issue(
                                "adapter.shape_list_unverified",
                                "Selected RDF list-shaped values require the "
                                "original SHACL obligation",
                                kind="unverified",
                            )
                        )
                        return InstanceCheck(
                            encoded,
                            None,
                            Report(
                                "shacl-imported-output",
                                0,
                                tuple(issues),
                                requested=1,
                                complete=False,
                            ),
                        )
            operation.consume("states", len(model.fields))
            for field in model.fields:
                operation.consume("work")
                key = field.key or field.name
                bound_text(key, operation)
                if key not in node or node[key] is None or node[key] == []:
                    continue
                bound_path((*usage.path, key), operation)
                expectations.append(
                    ContextExpectation(
                        (*usage.path, key), field.predicate, reverse=field.reverse
                    )
                )
        if not entities:
            issues.append(
                Issue(
                    "adapter.shape_binding_unverified",
                    "No actual output entity binding was supplied",
                    kind="unverified",
                )
            )
            return InstanceCheck(
                encoded,
                None,
                Report(
                    "shacl-imported-output",
                    0,
                    tuple(issues),
                    requested=1,
                    complete=False,
                ),
            )
        return check_serialized(
            owned,
            (model,),
            entities,
            vocabulary=vocabulary,
            context=context,
            declaration_report=imported._translation_report,
            base=base,
            document_loader=prepared._loader,
            budget=operation,
            context_expectations=expectations,
            _prepared=prepared,
        )
    except ResourceError:
        try:
            issues.append(
                Issue(
                    "adapter.shape_output_backend_unavailable",
                    "Actual output checking requires the jsonld optional dependency",
                    kind="unverified",
                )
            )
        except LimitExceeded as error:
            issues.exhausted(str(error))
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            retain_report(serialization_failure(error, operation), operation, issues)
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return InstanceCheck(
        None,
        None,
        Report("shacl-imported-output", 0, tuple(issues), requested=1, complete=False),
    )
