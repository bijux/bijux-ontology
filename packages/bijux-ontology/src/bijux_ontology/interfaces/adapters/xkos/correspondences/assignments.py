"""Assess actual selected assignments using separately supplied exact value maps."""

from typing import Literal

from rdflib import BNode, URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import operation
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.migrations.values.types import ValueMap
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node

from .reading import endpoint, issue, owns, physical, reserve, values
from .types import XKOS as X
from .types import (
    AssignmentAssessment,
    AssignmentResolution,
    ConceptAssociation,
    Correspondence,
    RecordConversion,
    Resource,
)
from .writing import qualify


def _same(left: Resource, right: Resource) -> bool:
    """Anonymous record/group authority requires the actual owned resource object."""
    return (
        left is right
        if isinstance(left, BNode) or isinstance(right, BNode)
        else left == right
    )


def _choices(
    conversions: tuple[RecordConversion, ...],
    records: tuple[Resource, ...],
    table: Correspondence,
    budget: Budget,
) -> tuple[tuple[RecordConversion, tuple[tuple[URIRef, URIRef], ...]], ...]:
    """Capture existing ValueMap entries without implementing another mapping engine."""
    if type(conversions) is not tuple:
        raise InputError("Record conversions require an immutable tuple")
    reserve(budget, len(conversions))
    result: list[tuple[RecordConversion, tuple[tuple[URIRef, URIRef], ...]]] = []
    for conversion in conversions:
        if (
            type(conversion) is not RecordConversion
            or not isinstance(conversion.record, URIRef | BNode)
            or not isinstance(conversion.association, URIRef | BNode)
            or type(conversion.values) is not ValueMap
        ):
            raise InputError("Expected a record/group and an existing exact ValueMap")
        bound_node(conversion.record, budget)
        bound_node(conversion.association, budget)
        budget.consume("work", len(records) + len(table.associations) + len(result))
        if not any(_same(conversion.record, record) for record in records):
            raise InputError(
                "A conversion record is outside the exact selected record scope"
            )
        if not any(
            _same(conversion.association, group.identifier)
            for group in table.associations
        ):
            raise InputError(
                "A conversion association lacks exact owned table identity"
            )
        if any(
            _same(conversion.record, item[0].record)
            and _same(conversion.association, item[0].association)
            for item in result
        ):
            raise InputError("Supply each record/group conversion only once")
        if conversion.values.allow_language_change:
            raise InputError("Category conversions accept exact IRI pairs only")
        reserve(budget, len(conversion.values.entries))
        pairs = []
        for source, target in conversion.values.entries:
            if type(source) is not URIRef or type(target) is not URIRef:
                raise InputError(
                    "Classification conversion tables require named IRI pairs"
                )
            bound_node(source, budget)
            bound_node(target, budget)
            pairs.append((source, target))
        result.append((conversion, tuple(pairs)))
    return tuple(result)


def assess_assignments(
    table: Correspondence,
    source: PreparedSource,
    records: tuple[Resource, ...],
    *,
    predicate: URIRef = X.classifiedUnder,
    graph_name: GraphSelection | None = None,
    conversions: tuple[RecordConversion, ...] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> AssignmentAssessment:
    """Inspect supplied record assignments; an explicit per-record policy is required.

    Even a one-to-one group does not authorize a data rewrite automatically.
    Splits, merges and many-to-many groups supply candidates, never allocation
    weights or distributed counts. Each caller choice is checked against its
    original group and the actual destination release. No RDF is mutated and no
    compatibility claim extends beyond these exact selected record assertions.
    """
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Assignment assessment requires Limits")
    current = operation(limits, budget)
    issues = IssueCollector(current)
    children: tuple[Report, ...] = ()
    resolutions: list[AssignmentResolution] = []
    selected = None
    complete = False
    try:
        if (
            type(table) is not Correspondence
            or not isinstance(source, PreparedSource)
            or type(predicate) is not URIRef
        ):
            raise InputError(
                "Select an owned table/source and explicit classification predicate"
            )
        bound_node(predicate, current)
        retain_report(table.report, current, issues)
        children = (table.report,)
        qualify(table, current)
        selected, graph, metadata = physical(source, graph_name, current)
        if type(records) is not tuple or not records:
            raise InputError("Select an explicit nonempty immutable record inventory")
        reserve(current, len(records))
        for position, record in enumerate(records):
            current.consume("work", position + 1)
            if not isinstance(record, URIRef | BNode):
                raise InputError("Selected records require RDF resources")
            bound_node(record, current)
            if isinstance(record, BNode) and not owns(graph, record, current):
                raise InputError("Anonymous record requires exact source ownership")
            if any(_same(record, earlier) for earlier in records[:position]):
                raise InputError("Select each exact record once")
        choices = _choices(conversions, records, table, current)
        used: set[tuple[int, int]] = set()
        reserve(current, sum(len(pairs) for _, pairs in choices))
        for record in records:
            observed = values(graph, record, predicate, current)
            if not observed:
                issue(
                    issues,
                    "xkos.assignment_missing",
                    "Selected record has no supplied assignment in this graph",
                    record,
                )
            for category in observed:
                current.consume("work", len(table.associations) + len(choices))
                reserve(current, len(table.associations) + 1)
                current.consume(
                    "work", sum(len(group.sources) for group in table.associations)
                )
                groups = tuple(
                    group for group in table.associations if category in group.sources
                )
                target = None
                status: Literal["resolved", "requires-policy", "rejected"] = (
                    "requires-policy"
                )
                if not endpoint(category, table._releases[0], current, issues):
                    status = "rejected"
                elif not groups:
                    issue(
                        issues,
                        "xkos.assignment_unmapped",
                        "Actual source assignment has no supplied correspondence group",
                        category,
                    )
                    status = "rejected"
                matches: list[tuple[ConceptAssociation, URIRef]] = []
                for index, (conversion, pairs) in enumerate(choices):
                    if not _same(conversion.record, record):
                        continue
                    current.consume("work", len(pairs))
                    for pair_index, (before, after) in enumerate(pairs):
                        if category != before:
                            continue
                        used.add((index, pair_index))
                        current.consume("work", len(table.associations))
                        group = next(
                            group
                            for group in table.associations
                            if _same(group.identifier, conversion.association)
                        )
                        if category not in group.sources or after not in group.targets:
                            issue(
                                issues,
                                "xkos.conversion_outside_group",
                                "Explicit conversion is outside its intact group",
                                category,
                            )
                            status = "rejected"
                        elif endpoint(after, table._releases[1], current, issues):
                            reserve(current)
                            matches.append((group, after))
                        else:
                            status = "rejected"
                if status != "rejected":
                    if len(matches) == 1:
                        target = matches[0][1]
                        status = "resolved"
                    else:
                        issue(
                            issues,
                            "xkos.conversion_required",
                            "Assignment needs one explicit record/group target choice",
                            category,
                            unknown=True,
                        )
                resolutions.append(
                    AssignmentResolution(record, category, groups, target, status)
                )
        for index, (conversion, pairs) in enumerate(choices):
            for pair_index in range(len(pairs)):
                current.consume("work")
                if (index, pair_index) not in used:
                    issue(
                        issues,
                        "xkos.conversion_unexercised",
                        "Conversion has no actual selected source assignment",
                        conversion.record,
                        unknown=True,
                    )
        if not metadata.same_scope(physical(source, graph_name, current)[2]):
            raise ResourceError("Assignment source scope changed during assessment")
        qualify(table, current)
        complete = True
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "xkos.assignment_unverified"
                    if isinstance(error, ResourceError)
                    else "xkos.input_invalid",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return AssignmentAssessment(
        source,
        selected,
        predicate,
        tuple(resolutions),
        Report(
            "xkos.assignment-assessment",
            len(resolutions),
            tuple(issues),
            requested=max(1, len(resolutions)),
            complete=complete,
            children=children,
            regime="explicit-finite-record-conversion",
        ),
    )
