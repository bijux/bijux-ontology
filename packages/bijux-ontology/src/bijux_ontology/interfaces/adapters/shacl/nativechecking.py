"""Qualify actual native RDF representation before checking imported fields."""

from collections.abc import Sequence
from typing import cast

from rdflib import URIRef
from rdflib.namespace import RDF

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node, bound_text
from bijux_ontology.reports import Issue, Report, combine_reports

from .types import ShapeContractImport


def check_imported_rdf(
    source: PreparedSource,
    imported: ShapeContractImport,
    usages: Sequence[RdfModelUsage],
    *,
    graph_name: GraphSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Report:
    """Check selected ordinary RDF values after qualifying the list-head boundary.

    Static declarations alone cannot distinguish field list-members from SHACL
    property list-heads. Actual selected list-shaped endpoints therefore remain
    required unknown. This guard uses original selected graph terms, never a
    list interpreter or an altered generic value matcher.
    """
    if not isinstance(source, PreparedSource) or not isinstance(
        imported, ShapeContractImport
    ):
        raise InputError("Imported RDF checking requires owned source and import")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Expected Limits")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Imported RDF limits conflict with the shared Budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    checked = 0
    try:
        retain_report(imported._translation_report, operation, issues)
        if not imported.extraction.declarations:
            return imported.rdf_contract_report
        if source.union:
            issues.append(
                Issue(
                    "adapter.shape_representation_unverified",
                    "Declared unions are outside the single-graph import profile",
                    kind="unverified",
                )
            )
        graph = source._read(graph_name, budget=operation)
        if not isinstance(usages, Sequence) or isinstance(
            cast(object, usages), str | bytes
        ):
            raise InputError("Imported RDF usages require a finite sequence")
        count = len(usages)
        operation.consume("states", count)
        operation.consume("bytes", 16 * count)
        captured = []
        model = imported.extraction.declarations[0]
        for index in range(count):
            operation.consume("work")
            try:
                usage = usages[index]
            except IndexError as error:
                raise InputError(
                    "Imported RDF usages changed during capture"
                ) from error
            if not isinstance(usage, RdfModelUsage) or usage.model != model.identifier:
                raise InputError("Imported RDF usages require the imported model")
            bound_text(usage.model, operation)
            bound_text(usage.location, operation)
            bound_node(usage.subject, operation)
            captured.append(usage)
            for field in model.fields:
                operation.consume("work")
                bound_text(field.predicate, operation)
                predicate = URIRef(field.predicate)
                values = (
                    graph.subjects(predicate, usage.subject)
                    if field.reverse
                    else graph.objects(usage.subject, predicate)
                )
                for value in values:
                    operation.consume("work", 3)
                    bound_node(value, operation)
                    if (
                        value == RDF.nil
                        or next(graph.triples((value, RDF.first, None)), None)
                        is not None
                        or next(graph.triples((value, RDF.rest, None)), None)
                        is not None
                    ):
                        issues.append(
                            Issue(
                                "adapter.shape_list_unverified",
                                "Selected RDF list-shaped values require the "
                                "original SHACL obligation",
                                kind="unverified",
                            )
                        )
            checked += 1
        if not captured:
            issues.append(
                Issue(
                    "adapter.shape_binding_unverified",
                    "No actual RDF model binding was supplied",
                    kind="unverified",
                )
            )
        if any(
            issue.code
            in (
                "adapter.shape_list_unverified",
                "adapter.shape_representation_unverified",
            )
            for issue in issues
        ):
            return Report(
                "shacl-imported-rdf",
                checked,
                tuple(issues),
                requested=max(1, count),
                complete=False,
            )
        native = check_rdf_emission(
            source,
            imported.extraction.declarations,
            captured,
            graph_name=graph_name,
            budget=operation,
        )
        retain_report(native, operation, issues)
        return combine_reports(
            "shacl-imported-rdf", imported._translation_report, native
        )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    return Report(
        "shacl-imported-rdf",
        checked,
        tuple(issues),
        requested=max(1, checked),
        complete=False,
    )
