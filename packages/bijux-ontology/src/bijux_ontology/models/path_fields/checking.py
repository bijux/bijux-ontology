"""Check declared path endpoints and intermediate ownership on actual RDF."""

from collections.abc import Sequence
from dataclasses import replace
from typing import cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.term import Node

from bijux_ontology.declarations.path_fields import IntermediateRequirement
from bijux_ontology.declarations.types import ValueDeclaration
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.jsonld.preparation import JSONLDPreparation
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.models.application_values import (
    application_matches,
    retain_enumeration_report,
)
from bijux_ontology.models.occurrences import check_output_counts
from bijux_ontology.models.rdf_emission import _traits
from bijux_ontology.models.value_matching import identity_matches, matches_value
from bijux_ontology.rdf.literals import check_literal
from bijux_ontology.rdf.path_traversal import (
    PathIdentityUnavailable,
    finite_path_steps,
    path_values,
)
from bijux_ontology.rdf.paths import capture_path
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node, bound_text
from bijux_ontology.reports import Issue, Location, Report

from .types import PathFieldUsage


def _branch(value: ValueDeclaration, budget: Budget) -> None:
    """Bound retained branch metadata before pure matching constructs its sets."""
    for text in (value.datatype, value.model, value.container):
        if text is not None:
            bound_text(text, budget)
    for metadata in (value.language, value.direction):
        if isinstance(metadata, str):
            bound_text(metadata, budget)
    budget.consume("states", len(value.types))
    for iri in value.types:
        bound_text(iri, budget)


def _count(value: int | None, budget: Budget) -> None:
    """Bound count formatting before diagnostics can retain a caller integer."""
    if value is not None:
        if value.bit_length() > min(4096, budget.limits.max_bytes):
            raise LimitExceeded("Path count representation exceeds its allowance")
        bound_text(str(value), budget)


def _location(value: Location | None, budget: Budget) -> None:
    """Qualify caller evidence before retaining any part in a diagnostic."""
    if value is None:
        return
    _count(value.branch, budget)
    for text in (value.source, value.term, value.graph, value.model):
        if text is not None:
            bound_text(text, budget)
    budget.consume("states", len(value.path))
    for part in value.path:
        budget.consume("work")
        if isinstance(part, str):
            bound_text(part, budget)
        else:
            if part.bit_length() > min(4096, budget.limits.max_bytes):
                raise LimitExceeded("Source path index exceeds max_bytes")
            bound_text(str(part), budget)
    allowance = len(value.source) + sum(
        2 * len(part) + 1 if isinstance(part, str) else len(str(part)) + 1
        for part in value.path
    )
    if allowance > budget.limits.max_bytes:
        raise LimitExceeded("Source diagnostic pointer exceeds max_bytes")
    budget.consume("bytes", 4 * allowance)
    budget.consume("work", allowance)


def _intermediate(
    graph: Graph,
    subject: Node,
    steps: tuple[tuple[URIRef, bool], ...],
    requirement: IntermediateRequirement,
    where: str,
    budget: Budget,
    issues: IssueCollector,
    literal_identity: bool,
) -> None:
    """Apply a distinct prefix count and explicit local record/ownership rules."""
    if requirement.step >= len(steps):
        raise InputError("Intermediate requirements must precede the endpoint step")
    _count(requirement.minimum, budget)
    _count(requirement.maximum, budget)
    for iri in requirement.types:
        bound_text(iri, budget)
    if requirement.identity.expected_iri is not None:
        bound_text(requirement.identity.expected_iri, budget)
    nodes = path_values(
        graph,
        subject,
        steps[: requirement.step],
        budget,
        literal_identity=literal_identity,
    )
    minimum = requirement.minimum or 0
    if (
        len(nodes) < minimum
        or requirement.maximum is not None
        and len(nodes) > requirement.maximum
    ):
        issues.append(
            Issue(
                "emission.path_intermediate_count_mismatch",
                "Intermediate node count violates the logical field contract",
                where,
                actual=str(len(nodes)),
            )
        )
    for node in nodes:
        budget.consume("work")
        if not isinstance(node, URIRef | BNode):
            issues.append(
                Issue(
                    "emission.path_intermediate_mismatch",
                    "A required intermediate record is not a resource",
                    where,
                )
            )
            continue
        traits = _traits(graph, node, budget)
        if not identity_matches(requirement.identity, traits) or set(
            requirement.types
        ) - set(traits.types):
            issues.append(
                Issue(
                    "emission.path_intermediate_mismatch",
                    "Intermediate record lacks its explicit identity or asserted type",
                    where,
                    actual=str(node),
                )
            )
        budget.consume("work")
        if (
            requirement.described
            and next(graph.triples((node, None, None)), None) is None
        ):
            issues.append(
                Issue(
                    "emission.path_intermediate_mismatch",
                    "Required intermediate record has no selected description",
                    where,
                    actual=str(node),
                )
            )
        if requirement.ownership == "exclusive":
            predicate, inverse = steps[requirement.step - 1]
            owners = path_values(
                graph,
                node,
                ((predicate, not inverse),),
                budget,
                literal_identity=literal_identity,
            )
            if len(owners) != 1:
                issues.append(
                    Issue(
                        "emission.path_ownership_mismatch",
                        "Intermediate record requires one owner on its selected edge",
                        where,
                        actual=str(len(owners)),
                    )
                )


def check_path_fields(
    source: PreparedSource | JSONLDPreparation,
    usages: Sequence[PathFieldUsage],
    *,
    graph_name: GraphSelection | None = None,
    union: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Report:
    """Check explicit logical fields without treating their paths as properties.

    Finite predicate/sequence/inverse steps use one shared bounded RDF walker.
    A JSONLDPreparation retains its conversion report; named or owned subject
    bindings are still required. No compact source field/blank correspondence,
    source null/omission, OWL inference or unsupported path operator is guessed.
    Resource roles, nested model definitions and list contracts remain unknown
    when they need evidence beyond this operation's selected endpoint subset.
    """
    if not isinstance(source, PreparedSource | JSONLDPreparation):
        raise InputError(
            "Path fields require owned RDF or an established JSON-LD preparation"
        )
    if not isinstance(usages, Sequence) or isinstance(
        cast(object, usages), str | bytes
    ):
        raise InputError("Path fields require a finite sequence of explicit bindings")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Path field limits conflict with the operation budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    children: list[Report] = []
    enumerations: dict[tuple[int, bool], Report] = {}
    checked = 0
    complete = True
    anchor = ""
    prepared: PreparedSource | None = (
        source if isinstance(source, PreparedSource) else source.source
    )
    try:
        operation.consume("resources")
        if isinstance(source, JSONLDPreparation):
            for issue in source.report.issues:
                for text in (
                    issue.code,
                    issue.message,
                    issue.location,
                    issue.expected,
                    issue.actual,
                    issue.evidence_json,
                ):
                    if text is not None:
                        bound_text(text, operation)
                _location(issue.details, operation)
                issues.append(issue)
            children.append(source.report)
        if prepared is None:
            issues.append(
                Issue(
                    "emission.path_mapping_unverified",
                    "JSON-LD preparation did not establish an owned RDF projection",
                    kind="unverified",
                )
            )
            complete = False
        else:
            for selection in (prepared.graph_name, graph_name):
                if selection is not None:
                    bound_text(selection, operation)
            if isinstance(graph_name, BNode) and not any(
                name is graph_name for name in prepared._graph_names(budget=operation)
            ):
                issues.append(
                    Issue(
                        "emission.path_mapping_unverified",
                        "Blank graph selection lacks exact snapshot ownership",
                        kind="unverified",
                    )
                )
                return Report(
                    "path-fields",
                    0,
                    tuple(issues),
                    children=tuple(children),
                    requested=1,
                    complete=False,
                    regime="actual-rdf-output",
                )
            graph = prepared._read(graph_name, union=union, budget=operation)
            operation.consume("triples", len(graph))
            blanks: set[int] = set()
            present: set[Node] = set()
            for subject, _, value in graph:
                operation.consume("work")
                operation.consume("states", 3)
                present.update((subject, value))
                blanks.update(
                    id(node) for node in (subject, value) if isinstance(node, BNode)
                )
            count = len(usages)
            operation.consume("states", count)
            if not count:
                issues.append(
                    Issue(
                        "emission.path_mapping_unverified",
                        "No logical field subject binding was established",
                        kind="unverified",
                    )
                )
                complete = False
            for index in range(count):
                anchor = ""
                operation.consume("work")
                operation.consume("resources")
                usage = usages[index]
                if not isinstance(usage, PathFieldUsage):
                    raise InputError("Expected typed PathFieldUsage bindings")
                field = usage.field
                bound_text(field.name, operation)
                bound_node(usage.subject, operation)
                _location(usage.location, operation)
                qualified_anchor = (
                    str(usage.location) if usage.location is not None else field.name
                )
                bound_text(qualified_anchor, operation)
                anchor = qualified_anchor
                checked += 1
                if isinstance(usage.subject, BNode) and id(usage.subject) not in blanks:
                    issues.append(
                        Issue(
                            "emission.path_mapping_unverified",
                            "Blank subject is foreign to the selected snapshot",
                            anchor,
                            kind="unverified",
                        )
                    )
                    continue
                if usage.subject not in present:
                    issues.append(
                        Issue(
                            "emission.path_mapping_unverified",
                            "Selected subject has no occurrence in this graph",
                            anchor,
                            kind="unverified",
                        )
                    )
                    continue
                captured = capture_path(field.path, budget=operation)
                steps = finite_path_steps(captured, operation)
                if steps is None:
                    issues.append(
                        Issue(
                            "emission.path_mapping_unverified",
                            "Path is outside the finite sequence/inverse subset",
                            anchor,
                            kind="unverified",
                        )
                    )
                    continue
                operation.consume(
                    "states", len(field.values) + len(field.intermediates)
                )
                for branch in field.values:
                    operation.consume("work")
                    _branch(branch, operation)
                    retain_enumeration_report(
                        branch.constraints,
                        enumerations,
                        budget=operation,
                        issues=issues,
                    )
                for count_bound in (
                    field.output.minimum_values,
                    field.output.maximum_values,
                    field.output.minimum_list_members,
                    field.output.maximum_list_members,
                ):
                    _count(count_bound, operation)
                literal_identity = prepared.literal_fidelity in (
                    "source_terms",
                    "supplied_terms",
                )
                try:
                    for requirement in field.intermediates:
                        _intermediate(
                            graph,
                            usage.subject,
                            steps,
                            requirement,
                            anchor,
                            operation,
                            issues,
                            literal_identity,
                        )
                    endpoints = path_values(
                        graph,
                        usage.subject,
                        steps,
                        operation,
                        literal_identity=literal_identity,
                    )
                except PathIdentityUnavailable:
                    issues.append(
                        Issue(
                            "literal.lexical_unverified",
                            "Path requires exact intermediate literal identity",
                            anchor,
                            kind="unverified",
                        )
                    )
                    continue
                faithful = prepared.literal_fidelity in (
                    "source_terms",
                    "supplied_terms",
                ) or not any(isinstance(node, Literal) for node in endpoints)
                if not faithful:
                    issues.append(
                        Issue(
                            "literal.lexical_unverified",
                            "Exact endpoint literal identity was not retained",
                            anchor,
                            kind="unverified",
                        )
                    )
                for node in endpoints:
                    operation.consume("work")
                    valid: bool | None = None
                    if isinstance(node, Literal):
                        literal = check_literal(node, location=anchor, budget=operation)
                        checked += literal.checked
                        issues.extend(literal.issues)
                        valid = literal.verified
                    actual = _traits(graph, node, operation)
                    results: list[bool | None] = []
                    for branch in field.values:
                        operation.consume("work")
                        if branch.container not in (None, "@set"):
                            results.append(None)
                            continue
                        matched = matches_value(branch, actual, {})
                        if matched is not False and branch.constraints is not None:
                            application = application_matches(
                                branch.constraints,
                                node,
                                budget=operation,
                                lexical_valid=valid,
                            )
                            matched = (
                                False
                                if application is False
                                else None
                                if matched is None or application is None
                                else True
                            )
                        results.append(matched)
                    if True not in results:
                        unknown = None in results
                        issues.append(
                            Issue(
                                "emission.value_unverified"
                                if unknown
                                else "emission.value_mismatch",
                                "Endpoint does not establish a value alternative",
                                anchor,
                                kind="unverified" if unknown else "finding",
                                details=usage.location,
                            )
                        )
                if (
                    field.output.require_property
                    or not field.output.allow_null
                    or not field.output.allow_empty
                    or field.output.minimum_list_members is not None
                    or field.output.maximum_list_members is not None
                ):
                    issues.append(
                        Issue(
                            "emission.path_mapping_unverified",
                            "Source presence or list members remain unverified",
                            anchor,
                            kind="unverified",
                        )
                    )
                if faithful:
                    counts = replace(
                        field.output,
                        minimum_list_members=None,
                        maximum_list_members=None,
                    )
                    issues.extend(
                        check_output_counts(
                            counts,
                            distinct_values=len(endpoints),
                            list_lengths=(),
                            non_list=False,
                            where=anchor,
                            budget=operation,
                        )
                    )
    except LimitExceeded as error:
        issues.exhausted(str(error), location=anchor)
        complete = False
    return Report(
        "path-fields",
        checked,
        tuple(issues),
        children=(*children, *enumerations.values()),
        requested=max(1, checked + (not complete)),
        complete=complete,
        regime="actual-rdf-output",
        families=("path-field",),
    )
