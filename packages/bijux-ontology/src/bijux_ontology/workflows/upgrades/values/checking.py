"""Before/after membership over the same owned, explicitly selected RDF edges."""

from collections.abc import Sequence
from typing import Literal

from rdflib import BNode, URIRef
from rdflib.term import Node

from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.models.emission.application import application_matches
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node, term_key

from .types import (
    ControlledField,
    ControlledValueUpgrade,
    ReplacementEvaluation,
    StoredValueImpact,
    ValueReplacement,
    ValueTransition,
)


def _anchor(record: URIRef, field: str, budget: Budget) -> str:
    """Bound the complete readable anchor before joining its owned components."""
    bound_node(record, budget)
    bound_text(field, budget)
    size = len(record.encode("utf-8")) + len(field.encode("utf-8")) + 3
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Controlled value location exceeds max_bytes")
    budget.consume("bytes", size)
    budget.consume("work", len(record) + len(field) + 3)
    return str(record) + " [" + field + "]"


def _status(before: bool | None, after: bool | None) -> ValueTransition:
    """Classify only paired actual membership, never unused declaration changes."""
    if before is None or after is None:
        return "uncertain"
    if before and not after:
        return "introduced"
    if not before and after:
        return "resolved"
    return "unchanged" if before else "persistent"


def _membership_issue(
    allowed: bool | None,
    where: str,
    details: Location,
    value: Node,
    issues: IssueCollector,
) -> None:
    """Keep exact use evidence separate from a readable policy failure diagnostic."""
    if allowed is not True:
        issues.append(
            Issue(
                "upgrade.value_inadmissible"
                if allowed is False
                else "upgrade.value_unverified",
                "Stored value is excluded by the selected consumer policy"
                if allowed is False
                else "Selected membership evidence does not decide this stored value",
                where,
                kind="finding" if allowed is False else "unverified",
                actual=str(value),
                details=details,
            )
        )


def compare_controlled_values(
    source: PreparedSource,
    fields: Sequence[ControlledField],
    records: Sequence[URIRef | BNode],
    *,
    replacements: Sequence[ValueReplacement] = (),
    graph_name: GraphSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ControlledValueUpgrade:
    """Check old/new enumeration policies against unchanged actual stored edges.

    Each named record and field is required coverage. Missing occurrences leave
    coverage unknown; this does not invent an RDF minimum-count requirement.
    Physical default/named or pinned single-graph selection is retained. Union
    projections and blank record identities are outside this finite profile.
    Literal equality is exact RDF term identity, not numeric value equality.

    Raw baseline failures and incomplete selection reports remain visible.
    Replacement evaluations are advisory; no source, policy or value is rewritten.
    """
    if not isinstance(source, PreparedSource):
        raise InputError("Controlled value comparison requires a PreparedSource")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Expected explicit Limits")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Explicit limits conflict with the shared Budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    side_issues = (IssueCollector(operation), IssueCollector(operation))
    children: tuple[list[Report], list[Report]] = ([], [])
    uses: list[StoredValueImpact] = []
    evaluated: list[ReplacementEvaluation] = []
    checked = [0, 0]
    requested = 1
    completed = 0
    finished = False

    def unknown(message: str, where: str = "") -> None:
        """Retain a fixed required-scope limitation with qualified metadata only."""
        issue = Issue(
            "upgrade.value_scope_unverified", message, where, kind="unverified"
        )
        issues.append(issue)
        for retained in side_issues:
            retained.append(issue)

    try:
        selected = capture_items(fields, ControlledField, operation)
        subjects = capture_items(records, str, operation)
        proposals = capture_items(replacements, ValueReplacement, operation)
        requested = max(1, len(selected) * len(subjects))
        names: set[str] = set()
        reports: set[tuple[int, int]] = set()
        constraints: list[tuple[URIRef, ValueConstraints, ValueConstraints]] = []
        for field in selected:
            bound_text(field.identifier, operation)
            bound_text(field.predicate, operation)
            if field.identifier in names:
                raise InputError("Controlled field identifiers must be unique")
            operation.consume("states")
            names.add(field.identifier)
            pair = []
            for side, enumeration in enumerate((field.before, field.after)):
                for terms in (enumeration.values, enumeration.excluded):
                    for term in terms:
                        operation.consume("work")
                        bound_node(term, operation)
                key = (side, id(enumeration))
                if key not in reports:
                    operation.consume("states")
                    qualification = enumeration.qualification_report()
                    retain_report(qualification, operation, side_issues[side])
                    children[side].append(qualification)
                    reports.add(key)
                pair.append(ValueConstraints(enumeration=enumeration))
            operation.consume("states")
            constraints.append((URIRef(field.predicate), pair[0], pair[1]))
        seen: set[Node] = set()
        for record in subjects:
            if not isinstance(record, URIRef | BNode):
                raise InputError("Stored records require named or blank RDF resources")
            bound_node(record, operation)
            if record in seen:
                raise InputError("Stored record bindings must not repeat")
            operation.consume("states")
            seen.add(record)
        for proposal in proposals:
            bound_node(proposal.before, operation)
            bound_node(proposal.after, operation)
        for name in (graph_name, source.graph_name):
            if isinstance(name, URIRef | BNode):
                bound_node(name, operation)
            elif name is not None:
                bound_text(name, operation)
        for text in (source.document_iri, source.effective_url):
            if text is not None:
                bound_text(text, operation)
        selected_graph_owned = True
        if isinstance(graph_name, BNode) and not any(
            node is graph_name for node in source._graph_names(budget=operation)
        ):
            selected_graph_owned = False
            unknown("Blank graph selection lacks exact owned source identity")
        if not selected or not subjects:
            unknown("Explicit nonempty fields and stored record coverage are required")
        elif (
            not selected_graph_owned
            or source.union
            or source.literal_fidelity
            not in (
                "source_terms",
                "supplied_terms",
            )
            or source.graph_fidelity != "complete"
        ):
            unknown("Actual exact terms and physical graph ownership are required")
        else:
            operation.consume("resources")
            graph = source._read(graph_name, budget=operation)
            name = source.graph_name if source.graph_name is not None else graph_name
            scope = (
                None
                if name is None or name == "@default"
                else name
                if isinstance(name, BNode)
                else URIRef(name)
            )
            graph_kind: Literal["graph", "default", "named"] = (
                "graph"
                if not source.is_dataset
                else "default"
                if scope is None
                else "named"
            )
            for record in subjects:
                if not isinstance(record, URIRef):
                    unknown("Blank record correspondence is outside this profile")
                    continue
                for field, (predicate, previous, current) in zip(
                    selected, constraints, strict=True
                ):
                    where = _anchor(record, field.identifier, operation)
                    details = Location(
                        source=source.document_iri or "",
                        term=str(record),
                        model=field.identifier,
                        graph=str(scope) if scope is not None else None,
                    )
                    pattern = (
                        (None, predicate, record)
                        if field.reverse
                        else (record, predicate, None)
                    )
                    found = False
                    for triple in graph.triples(pattern):
                        operation.consume("work")
                        operation.consume("triples")
                        for node in triple:
                            bound_node(node, operation)
                        # Reserve the terminal record before membership or optional
                        # advisory evaluation can exhaust a later allowance.
                        operation.consume("states")
                        operation.consume("bytes", 128)
                        value = triple[0] if field.reverse else triple[2]
                        before = application_matches(previous, value, budget=operation)
                        checked[0] += 1
                        _membership_issue(before, where, details, value, side_issues[0])
                        after = application_matches(current, value, budget=operation)
                        checked[1] += 1
                        use = StoredValueImpact(
                            record,
                            field.identifier,
                            triple,
                            value,
                            scope,
                            graph_kind,
                            field.reverse,
                            before,
                            after,
                            _status(before, after),
                        )
                        uses.append(use)
                        found = True
                        _membership_issue(after, where, details, value, side_issues[1])
                        for proposal in proposals:
                            operation.consume("work")
                            bound_node(value, operation)
                            bound_node(proposal.before, operation)
                            if term_key(value) != term_key(proposal.before):
                                continue
                            operation.consume("states")
                            operation.consume("bytes", 64)
                            allowed = application_matches(
                                current, proposal.after, budget=operation
                            )
                            evaluated.append(
                                ReplacementEvaluation(use, proposal, allowed)
                            )
                    if not found:
                        unknown(
                            "Required stored field has no actual selected value", where
                        )
                    completed += 1
            finished = completed == requested
    except LimitExceeded as error:
        issues.exhausted(str(error), location="upgrade.values")
    before_report, after_report = (
        Report(
            "controlled-values." + side,
            checked[index],
            tuple(side_issues[index]),
            requested=checked[index] if finished else checked[index] + 1,
            complete=finished,
            allow_empty=True,
            children=tuple(children[index]),
            regime="explicit-enumeration-membership",
        )
        for index, side in enumerate(("before", "after"))
    )
    report = Report(
        "controlled-values",
        completed,
        tuple(issues),
        requested=requested,
        complete=finished,
        children=(before_report, after_report),
        regime="stored-values-under-selected-consumer-policies",
    )
    return ControlledValueUpgrade(
        before_report, after_report, tuple(uses), tuple(evaluated), report
    )
