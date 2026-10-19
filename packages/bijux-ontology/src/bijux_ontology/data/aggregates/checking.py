"""Grouped exact arithmetic over explicit associated RDF record populations."""

from collections.abc import Sequence
from fractions import Fraction
from threading import Event

from rdflib import BNode, Literal, URIRef
from rdflib.paths import Path

from bijux_ontology.data.keys.extraction import _KeyExtraction, _scope
from bijux_ontology.data.keys.types import KeyRecord
from bijux_ontology.declarations.aggregates import AggregateContract
from bijux_ontology.declarations.keys import KeyComponent, KeyContract
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.paths import RdfPath, capture_path
from bijux_ontology.rdf.sources.metadata import SnapshotMetadata
from bijux_ontology.rdf.terms import bound_node, term_key

from .preparation import arithmetic, contract, number, owned_literal, reserve
from .types import (
    AggregateAnswer,
    AggregateCheck,
    AggregateContribution,
    AggregateGroup,
)


def _issue(issues: IssueCollector, message: str, group: str) -> None:
    """Unknown population/value coverage cannot become an omitted contribution."""
    issues.append(Issue("data.aggregate_unverified", message, group, kind="unverified"))


def _component(path: URIRef | Path | RdfPath, operation: Operation) -> KeyComponent:
    """Reuse the typed key extractor with a path owned by this same allowance."""
    owned = capture_path(path, budget=operation)
    reserve(operation)
    result = object.__new__(KeyComponent)
    object.__setattr__(result, "path", owned)
    object.__setattr__(result, "equality", "rdf-term")
    object.__setattr__(result, "normalization", "none")
    return result


def _key_contract(
    selected: AggregateContract,
    path: URIRef | Path | RdfPath,
    operation: Operation,
    *,
    unit: bool = False,
) -> KeyContract:
    """Reuse scoped extraction without applying quantity exclusions to units."""
    reserve(operation)
    result = object.__new__(KeyContract)
    object.__setattr__(result, "name", selected.name)
    object.__setattr__(result, "components", (_component(path, operation),))
    object.__setattr__(result, "absence", "unverified" if unit else selected.missing)
    object.__setattr__(
        result,
        "multiplicity",
        "unverified" if unit else selected.multiplicity,
    )
    return result


def _records(
    group: AggregateGroup,
    selected: AggregateContract,
    operation: Operation,
    issues: IssueCollector,
    retained: list[AggregateContribution],
    excluded: list[str],
) -> list[tuple[object, Fraction | tuple[object, ...]]]:
    """Reuse existing exact path/graph extraction and preserve every description."""
    records = capture_items(group.members, KeyRecord, operation)
    identifiers: set[str] = set()
    entities: dict[object, Fraction | tuple[object, ...]] = {}
    values: list[tuple[object, Fraction | tuple[object, ...]]] = []
    key_contract = (
        None
        if selected.member_path is None
        else _key_contract(selected, selected.member_path, operation)
    )
    unit_contract = (
        None
        if selected.unit_path is None
        else _key_contract(selected, selected.unit_path, operation, unit=True)
    )
    for raw in records:
        reserve(operation, 3)
        bound_text(raw.identifier, operation)
        bound_node(raw.subject, operation)
        if raw.identifier in identifiers:
            raise InputError("Each aggregate occurrence requires a unique identifier")
        identifiers.add(raw.identifier)
        record = KeyRecord(
            raw.identifier,
            raw.source,
            raw.subject,
            graph_name=raw.graph_name,
            union=raw.union,
        )
        metadata = SnapshotMetadata.capture(record.source, operation)
        if metadata.graph_fidelity != "complete":
            raise ResourceError("Aggregate record graph coverage is incomplete")
        scope, union = _scope(record)
        entity: object = (
            str(record.subject)
            if isinstance(record.subject, URIRef)
            else (id(record.source), scope, union, record.subject)
        )
        if key_contract is not None:
            extraction = _KeyExtraction((record,), key_contract, operation, issues, 1)
            keys = tuple(extraction)
            if extraction.exclusions:
                excluded.extend(extraction.exclusions)
            if not keys:
                continue
            row = keys[0].values
        else:
            if isinstance(record.graph_name, BNode) and not any(
                node is record.graph_name
                for node in record.source._graph_names(budget=operation)
            ):
                raise ResourceError(
                    "Blank graph binding lacks exact snapshot ownership"
                )
            graph = record.source._read(
                record.graph_name,
                union=record.union,
                budget=operation,
            )
            present = False
            for triple in graph.triples((record.subject, None, None)):
                operation.consume("work")
                operation.consume("triples")
                if not isinstance(record.subject, BNode) or triple[0] is record.subject:
                    present = True
                    break
            if not present:
                _issue(
                    issues,
                    "A selected record subject is absent from its owned scope",
                    group.identifier,
                )
                continue
            row = ()
        retained.append(AggregateContribution(record, row))
        if unit_contract is not None:
            unit_values = tuple(
                _KeyExtraction((record,), unit_contract, operation, issues, 1)
            )
            if not unit_values:
                continue
            row = (*row, *unit_values[0].values)
            retained[-1] = AggregateContribution(record, row)
        if selected.unit is not None and row[-1] != selected.unit:
            _issue(
                issues,
                "A contributing value lacks the exact declared unit",
                group.identifier,
            )
            continue
        if selected.function == "count":
            value: Fraction | tuple[object, ...] = Fraction(1)
        elif selected.function == "distinct_count":
            node = row[0]
            value = (
                ("blank", id(record.source), scope, union, node)
                if isinstance(node, BNode)
                else term_key(node)
            )
        else:
            if not isinstance(row[0], Literal):
                raise ResourceError("A numeric member path requires a literal value")
            value = number(row[0], operation)
        if selected.counting == "entities":
            if entity in entities:
                prior = entities[entity]
                if isinstance(prior, Fraction) and isinstance(value, Fraction):
                    arithmetic(prior, value, operation)
                if prior != value:
                    _issue(
                        issues,
                        "Repeated descriptions disagree on one entity contribution",
                        group.identifier,
                    )
                continue
            entities[entity] = value
        values.append((entity, value))
    return values


def _aggregate(
    selected: AggregateContract,
    values: list[tuple[object, Fraction | tuple[object, ...]]],
    operation: Operation,
) -> Fraction:
    """Compute one explicit finite function with no floating point or rounding."""
    if selected.function == "count":
        return Fraction(len(values))
    if selected.function == "distinct_count":
        seen = set()
        for _, value in values:
            reserve(operation)
            seen.add(value)
        return Fraction(len(seen))
    result = Fraction(0)
    for index, (_, value) in enumerate(values):
        if not isinstance(value, Fraction):
            raise InputError("Numeric aggregate received a nonnumeric contribution")
        arithmetic(result, value, operation)
        if selected.function == "sum":
            result += value
        elif index == 0 or (
            value < result if selected.function == "min" else value > result
        ):
            result = value
    return result


def check_aggregates(
    groups: Sequence[AggregateGroup],
    selected: AggregateContract,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> AggregateCheck:
    """Compare selected related-record populations to declared values or bounds.

    Complete entity/occurrence populations are explicit. Identical RDF triples
    remain sets; distinct descriptions count only in occurrence mode. Values
    follow finite sequence/inverse paths and existing exact source-term controls.
    Missing partitions, unsupported numbers, conflicting descriptions or ambiguous
    units never establish a complete arithmetic claim. Exclusions are explicit.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    answers: list[AggregateAnswer] = []
    owned = None
    requested = 1
    finished = False
    try:
        owned = contract(selected, operation)
        captured = capture_items(groups, AggregateGroup, operation)
        if not captured:
            raise InputError("Choose a nonempty finite aggregate group inventory")
        requested = len(captured)
        seen = set()
        for group in captured:
            reserve(operation, 8)
            if type(group) is not AggregateGroup or type(group.identifier) is not str:
                raise InputError("Expected ordinary named AggregateGroup records")
            bound_text(group.identifier, operation)
            if (
                not group.identifier
                or group.identifier in seen
                or type(group.complete) is not bool
            ):
                raise InputError("Group names must be unique and completeness explicit")
            seen.add(group.identifier)
            expected = owned_literal(group.expected, operation)
            members = []
            for record in capture_items(group.members, KeyRecord, operation):
                reserve(operation)
                bound_text(record.identifier, operation)
                bound_node(record.subject, operation)
                if record.graph_name is not None:
                    if isinstance(record.graph_name, BNode | URIRef):
                        bound_node(record.graph_name, operation)
                    else:
                        bound_text(record.graph_name, operation)
                members.append(
                    KeyRecord(
                        record.identifier,
                        record.source,
                        record.subject,
                        graph_name=record.graph_name,
                        union=record.union,
                    )
                )
            group = AggregateGroup(
                group.identifier, tuple(members), expected, group.complete
            )
            local = IssueCollector(operation)
            retained: list[AggregateContribution] = []
            excluded: list[str] = []
            actual = None
            complete = False
            try:
                required = number(
                    expected,
                    operation,
                    count=owned.function in ("count", "distinct_count"),
                )
                values = _records(group, owned, operation, local, retained, excluded)
                if not group.complete:
                    _issue(
                        local,
                        "The selected population has missing or unknown partitions",
                        group.identifier,
                    )
                if not values and (
                    owned.empty != "zero" or owned.function in ("min", "max")
                ):
                    _issue(
                        local,
                        "The declared aggregate has no supported empty-group value",
                        group.identifier,
                    )
                if not local:
                    actual = _aggregate(owned, values, operation)
                    arithmetic(actual, required, operation)
                    agrees = (
                        actual == required
                        if owned.comparison == "equal"
                        else actual <= required
                        if owned.comparison == "at_most"
                        else actual >= required
                    )
                    if not agrees:
                        local.append(
                            Issue(
                                "data.aggregate_mismatch",
                                "The aggregate differs from its declared bound",
                                group.identifier,
                                expected=str(required),
                                actual=str(actual),
                            )
                        )
                complete = not any(
                    issue.kind in ("unverified", "exhausted", "input_error")
                    for issue in local
                )
            except LimitExceeded as error:
                local.exhausted(str(error), location=group.identifier)
            except ResourceError as error:
                try:
                    _issue(local, str(error), group.identifier)
                except LimitExceeded as stopped:
                    local.exhausted(str(stopped), location=group.identifier)
            except InputError as error:
                try:
                    local.append(
                        Issue(
                            "data.aggregate_input_error",
                            str(error),
                            group.identifier,
                            kind="input_error",
                        )
                    )
                except LimitExceeded as stopped:
                    local.exhausted(str(stopped), location=group.identifier)
            report = Report(
                "data.aggregate_group",
                int(actual is not None or any(i.kind == "finding" for i in local)),
                tuple(local),
                requested=1,
                complete=complete,
                exclusions=tuple(excluded),
                regime="explicit-related-record-population-exact-arithmetic",
            )
            answers.append(
                AggregateAnswer(
                    group.identifier,
                    expected,
                    actual,
                    tuple(retained),
                    tuple(excluded),
                    report,
                )
            )
            issues.retain_completed(report)
            if any(issue.kind == "exhausted" for issue in local):
                break
        finished = len(answers) == requested
    except LimitExceeded as error:
        issues.exhausted(str(error), location="data.aggregates")
    except InputError as error:
        try:
            issues.append(
                Issue("data.aggregate_input_error", str(error), kind="input_error")
            )
        except LimitExceeded as stopped:
            issues.exhausted(str(stopped), location="data.aggregates")
    return AggregateCheck(
        owned,
        tuple(answers),
        Report(
            "data.aggregates",
            len(answers),
            tuple(issues),
            requested=requested,
            complete=finished,
            children=tuple(answer.report for answer in answers),
            regime="explicit-related-record-population-exact-arithmetic",
        ),
    )
