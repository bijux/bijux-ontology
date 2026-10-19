"""Grouped exact arithmetic over explicit associated RDF record populations."""

from collections.abc import Sequence
from fractions import Fraction
from threading import Event

from rdflib import BNode, Literal, URIRef
from rdflib.term import Node

from bijux_ontology.data.keys.types import KeyRecord
from bijux_ontology.data.records.reading import FiniteSteps, RecordReader, read_records
from bijux_ontology.declarations.aggregates import AggregateContract
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.paths.traversal import finite_path_steps
from bijux_ontology.rdf.paths.types import capture_path
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


def _entity_scope(reader: RecordReader) -> tuple[object, bool]:
    """Retain aggregate blank identity without applying key equality policy."""
    if not reader.metadata.is_dataset:
        return "@default", False
    if reader.selected.kind == "union":
        return None, True
    name = reader.selected.graph_name
    return name if name is not None else "@default", False


def _scalar(
    reader: RecordReader,
    record: KeyRecord,
    steps: FiniteSteps | None,
    selected: AggregateContract,
    operation: Operation,
    issues: IssueCollector,
    excluded: list[str],
    *,
    unit: bool = False,
) -> Node | None:
    """Apply quantity or unit coverage, absence and multiplicity independently."""
    observed = reader.values(record.subject, steps, operation)
    if not observed.complete:
        issues.append(
            Issue(
                "data.key_path_unverified"
                if observed.coverage == "unsupported_path"
                else "literal.lexical_unverified",
                "Key paths require finite sequences and inverse predicates"
                if observed.coverage == "unsupported_path"
                else "Key path requires exact intermediate literal identity",
                selected.name
                if observed.coverage == "unsupported_path"
                else record.identifier,
                kind="unverified",
            )
        )
        return None
    if not observed.values:
        if not unit and selected.missing == "exclude":
            operation.consume("states")
            excluded.append(record.identifier)
        else:
            issues.append(
                Issue(
                    "data.key_absent",
                    "A record has no value for a required key component",
                    record.identifier,
                    kind="finding"
                    if not unit and selected.missing == "reject"
                    else "unverified",
                )
            )
        return None
    if len(observed.values) != 1:
        issues.append(
            Issue(
                "data.key_multiple",
                "A record has multiple RDF values for a key component",
                record.identifier,
                kind="finding"
                if not unit and selected.multiplicity == "reject"
                else "unverified",
            )
        )
        return None
    value = observed.values[0]
    if isinstance(value, Literal) and not observed.exact_literals:
        issues.append(
            Issue(
                "literal.lexical_unverified",
                "Exact source literal identity was not retained",
                record.identifier,
                kind="unverified",
                actual=reader.metadata.literal_fidelity,
            )
        )
        return None
    bound_node(value, operation)
    operation.consume("states")
    return value


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
    member_steps = (
        None
        if selected.member_path is None
        else finite_path_steps(
            capture_path(selected.member_path, budget=operation), operation
        )
    )
    unit_steps = (
        None
        if selected.unit_path is None
        else finite_path_steps(
            capture_path(selected.unit_path, budget=operation), operation
        )
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
        if isinstance(record.graph_name, BNode) and not any(
            node is record.graph_name
            for node in record.source._graph_names(budget=operation)
        ):
            if selected.member_path is None:
                raise ResourceError(
                    "Blank graph binding lacks exact snapshot ownership"
                )
            issues.append(
                Issue(
                    "data.key_binding_unverified",
                    "Blank graph selection lacks exact snapshot ownership",
                    record.identifier,
                    kind="unverified",
                )
            )
            continue
        reader = read_records(
            record.source, operation, graph_name=record.graph_name, union=record.union
        )
        if reader.metadata.graph_fidelity != "complete":
            raise ResourceError("Aggregate record graph coverage is incomplete")
        scope, union = _entity_scope(reader)
        entity: object = (
            str(record.subject)
            if isinstance(record.subject, URIRef)
            else (id(record.source), scope, union, record.subject)
        )
        row: tuple[Node, ...]
        if selected.member_path is not None:
            if (
                isinstance(record.subject, BNode)
                and not reader.presence(record.subject, operation).owned
            ):
                issues.append(
                    Issue(
                        "data.key_binding_unverified",
                        "Blank record binding lacks exact snapshot ownership",
                        record.identifier,
                        kind="unverified",
                    )
                )
                continue
            member = _scalar(
                reader, record, member_steps, selected, operation, issues, excluded
            )
            if member is None:
                continue
            row = (member,)
        else:
            if not reader.described(record.subject, operation):
                _issue(
                    issues,
                    "A selected record subject is absent from its owned scope",
                    group.identifier,
                )
                continue
            row = ()
        retained.append(AggregateContribution(record, row))
        if selected.unit_path is not None:
            unit_value = _scalar(
                reader,
                record,
                unit_steps,
                selected,
                operation,
                issues,
                excluded,
                unit=True,
            )
            if unit_value is None:
                continue
            row = (*row, unit_value)
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
