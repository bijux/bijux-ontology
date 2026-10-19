"""Model output contracts over explicitly selected owned RDF subjects."""

from collections.abc import Sequence
from itertools import chain
from typing import cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.term import Node

from bijux_ontology.declarations.types import ModelDeclaration, ValueDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.jsonld.conversion import DirectionPolicy
from bijux_ontology.jsonld.values import ValueTraits
from bijux_ontology.models.contracts.registry import index_models
from bijux_ontology.models.emission.application import (
    application_matches,
    retain_enumeration_report,
)
from bijux_ontology.models.emission.counts import check_output_counts
from bijux_ontology.models.emission.coverage import predicate_issues
from bijux_ontology.models.emission.direction import project_direction
from bijux_ontology.models.emission.groups import TermKey, check_groups
from bijux_ontology.models.emission.matching import identity_matches, matches_value
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.models.resource_roles.checking import _RoleChecks
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry
from bijux_ontology.rdf.literals import check_literal
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.structure import read_rdf_list
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.sources.view import Vocabulary


def _traits(graph: Graph, node: Node, budget: Budget) -> ValueTraits:
    """Read exact native term traits without constructing JSON-LD or expansion."""
    bound_node(node, budget)
    if isinstance(node, Literal):
        return ValueTraits(
            "literal",
            datatype=str(
                node.datatype or (RDF.langString if node.language else XSD.string)
            ),
            language=node.language,
        )
    if isinstance(node, URIRef | BNode):
        kinds = []
        for kind in graph.objects(node, RDF.type):
            budget.consume("work")
            if isinstance(kind, URIRef):
                bound_node(kind, budget)
                kinds.append(str(kind))
        return ValueTraits(
            "iri" if isinstance(node, URIRef) else "blank",
            identifier=str(node) if isinstance(node, URIRef) else None,
            identifier_kind="iri" if isinstance(node, URIRef) else "blank",
            types=tuple(sorted(kinds)),
        )
    return ValueTraits("unknown")


def check_rdf_emission(
    source: PreparedSource,
    models: Sequence[ModelDeclaration],
    usages: Sequence[RdfModelUsage],
    *,
    vocabulary: Vocabulary | Graph | VocabularyView | None = None,
    graph_name: GraphSelection | None = None,
    union: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
    rdf_direction: DirectionPolicy | None = None,
    datatype_registry: DatatypeRegistry | None = None,
) -> Report:
    """Check actual selected RDF using the existing value and occurrence contracts.

    Graph selection applies to every subject, endpoint, type and RDF-list link.
    Unrelated subjects are checked only through unambiguous declared child links.
    No OWL entailment or source JSON null/omission is inferred. require_property
    remains source-presence unknown; guaranteed emission and minimum_values
    express required RDF predicate occurrences. Exact native literal validation
    and counts use the prepared source's available literal fidelity.
    """
    if rdf_direction not in (None, "i18n-datatype", "compound-literal"):
        raise InputError("Unknown RDF direction policy")
    if datatype_registry is not None and not isinstance(
        datatype_registry, DatatypeRegistry
    ):
        raise InputError("Expected an explicit DatatypeRegistry")
    if not isinstance(source, PreparedSource):
        raise InputError("RDF emission checking requires an owned PreparedSource")
    if isinstance(cast(object, usages), (str, bytes)) or not isinstance(
        usages, Sequence
    ):
        raise InputError("Expected RDF subject bindings")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Explicit limits conflict with the shared operation budget")
    budget = budget if budget is not None else Budget(limits)
    issues = IssueCollector(budget)
    enumeration_reports: dict[tuple[int, bool], Report] = {}
    checked = 0
    complete = True
    role_checks: _RoleChecks | None = None
    try:
        registry = index_models(models, budget, issues)
        needs_roles = False
        for declaration in registry.values():
            for field in declaration.fields:
                for branch in field.values:
                    budget.consume("work")
                    needs_roles |= branch.role is not None
                    retain_enumeration_report(
                        branch.constraints,
                        enumeration_reports,
                        budget=budget,
                        issues=issues,
                    )
                    if branch.model is not None and branch.model not in registry:
                        issues.append(
                            Issue(
                                "input.model_reference_missing",
                                "Value branch references an unknown model",
                                f"{declaration.identifier}.{field.name}",
                                kind="input_error",
                                actual=branch.model,
                            )
                        )
        graph = source._read(graph_name, union=union, budget=budget)
        role_checks = (
            _RoleChecks(vocabulary, budget, graph)
            if vocabulary is not None and needs_roles
            else None
        )

        def match_value(
            branch: ValueDeclaration,
            traits: ValueTraits,
            node: Node,
            *,
            container: str = "unordered",
            empty: bool = False,
        ) -> bool | None:
            """Reuse selected physical terms and one operation's role contracts."""
            role_result = None
            if (
                branch.role is not None
                and role_checks is not None
                and isinstance(node, URIRef | BNode)
            ):
                role_result = role_checks.matches(
                    branch.role,
                    node,
                    traits.types,
                    branch.types,
                    predicate=field.predicate,
                    reverse=field.reverse,
                )
            return matches_value(
                branch,
                traits,
                registry,
                container=container,
                empty=empty,
                role_result=role_result,
            )

        budget.consume("resources")
        budget.consume("triples", len(graph))
        nodes: set[Node] = set()
        blank_objects: set[int] = set()
        for subject, _, value in graph:
            budget.consume("work")
            nodes.update((subject, value))
            blank_objects.update(
                id(node) for node in (subject, value) if isinstance(node, BNode)
            )
        pending: list[RdfModelUsage] = []
        selected: set[tuple[str, Node]] = set()

        def bind(binding: RdfModelUsage) -> None:
            """Retain each exact model/subject obligation once, including recursion."""
            identity = binding.model, binding.subject
            if identity not in selected:
                budget.consume("states")
                selected.add(identity)
                pending.append(binding)

        for binding in usages:
            budget.consume("work")
            if not isinstance(binding, RdfModelUsage):
                raise InputError("Expected typed RDF model usage bindings")
            if binding.model not in registry:
                issues.append(
                    Issue(
                        "input.model_reference_missing",
                        "RDF usage references an unknown model",
                        binding.location or f"entity[{binding.subject}]",
                        kind="input_error",
                        actual=binding.model,
                    )
                )
                continue
            if (
                isinstance(binding.subject, BNode)
                and id(binding.subject) not in blank_objects
            ):
                issues.append(
                    Issue(
                        "emission.node_unverified",
                        "Blank subject did not come from this owned snapshot",
                        binding.location or f"entity[{binding.subject}]",
                        kind="unverified",
                    )
                )
                continue
            bind(binding)
        if not usages:
            issues.append(
                Issue(
                    "emission.node_unverified",
                    "No RDF subject binding was supplied",
                    kind="unverified",
                )
            )
        for binding in pending:
            budget.consume("work")
            where = binding.location or f"entity[{binding.subject}]"
            model = registry.get(binding.model)
            if model is None:
                issues.append(
                    Issue(
                        "input.model_reference_missing",
                        "RDF usage references an unknown model",
                        where,
                        kind="input_error",
                        actual=binding.model,
                    )
                )
                continue
            checked += 1
            subject = binding.subject
            if subject not in nodes:
                issues.append(
                    Issue(
                        "emission.reference_unverified",
                        "No selected RDF description was supplied for this subject",
                        where,
                        kind="unverified",
                        actual=str(subject),
                    )
                )
                continue
            traits = _traits(graph, subject, budget)
            if not identity_matches(model.subject, traits):
                issues.append(
                    Issue(
                        "emission.subject_mismatch",
                        "Subject violates the explicit RDF identity contract",
                        where,
                        expected=model.subject.expected_iri or "an IRI subject",
                        actual=str(subject),
                    )
                )
            if set(model.types) - set(traits.types):
                issues.append(
                    Issue(
                        "emission.types_mismatch",
                        "Subject lacks required asserted types in this graph",
                        where,
                        expected=", ".join(model.types),
                        actual=", ".join(traits.types),
                    )
                )
            if model.coverage is not None:
                checked += 1
                issues.append(
                    Issue(
                        "emission.source_coverage_unverified",
                        "Native RDF does not establish original source-key coverage",
                        where,
                        kind="unverified",
                    )
                )
                issues.extend(
                    predicate_issues(
                        model,
                        chain(
                            (
                                (str(predicate), False)
                                for _, predicate, _ in graph.triples(
                                    (subject, None, None)
                                )
                            ),
                            (
                                (str(predicate), True)
                                for _, predicate, _ in graph.triples(
                                    (None, None, subject)
                                )
                            ),
                        ),
                        where=where,
                        budget=budget,
                    )
                )
            group_states: dict[str, bool | None] = {}
            group_terms: dict[str, tuple[TermKey, ...] | None] = {}
            for field in model.fields:
                budget.consume("work")
                checked += 1
                field_where = f"{where}.{field.name}"
                endpoints: tuple[Node, ...]
                if field.predicate == "@id":
                    endpoints = (subject,) if not field.reverse else ()
                elif field.predicate == "@type":
                    endpoints = (
                        tuple(graph.objects(subject, RDF.type))
                        if not field.reverse
                        else ()
                    )
                else:
                    predicate = URIRef(field.predicate)
                    endpoints = tuple(
                        graph.subjects(predicate, subject)
                        if field.reverse
                        else graph.objects(subject, predicate)
                    )
                if model.emission_groups:
                    group_states[field.name] = bool(endpoints)
                    group_terms[field.name] = (
                        None
                        if any(
                            isinstance(value, BNode)
                            or isinstance(value, Literal)
                            and source.literal_fidelity
                            not in {"source_terms", "supplied_terms"}
                            for value in endpoints
                        )
                        else tuple(
                            dict.fromkeys(term_key(value) for value in endpoints)
                        )
                    )
                if not endpoints and field.emission == "guaranteed":
                    issues.append(
                        Issue(
                            "emission.required_entity_fact_missing",
                            "Required predicate is absent from the selected graph",
                            field_where,
                        )
                    )
                if field.output is not None and field.output.require_property:
                    issues.append(
                        Issue(
                            "emission.source_presence_unverified",
                            "RDF cannot establish source omission or null expansion",
                            field_where,
                            kind="unverified",
                        )
                    )
                list_lengths: list[int | None] = []
                non_list = False
                exact_values = True
                values: set[tuple[str | None, ...]] = set()
                for endpoint in endpoints:
                    budget.consume("work")
                    values.add(term_key(endpoint))
                    need_list = (
                        any(branch.container == "@list" for branch in field.values)
                        or field.output is not None
                        and (
                            field.output.minimum_list_members is not None
                            or field.output.maximum_list_members is not None
                        )
                    )
                    is_list = (
                        endpoint == RDF.nil
                        or next(graph.objects(endpoint, RDF.first), None) is not None
                        or next(graph.objects(endpoint, RDF.rest), None) is not None
                        or (endpoint, RDF.type, RDF.List) in graph
                    )
                    members: tuple[Node, ...] | None = None
                    if need_list and is_list:
                        try:
                            members = read_rdf_list(
                                graph,
                                endpoint,
                                max_items=budget.limits.max_work,
                                budget=budget,
                            )
                            budget.consume("work", len(members))
                            list_lengths.append(len(members))
                        except InputError as error:
                            if isinstance(error, LimitExceeded):
                                raise
                            list_lengths.append(None)
                            issues.append(
                                Issue(
                                    "emission.value_unverified",
                                    str(error),
                                    field_where,
                                    kind="unverified",
                                )
                            )
                    elif need_list:
                        non_list = True
                    endpoint_traits = _traits(graph, endpoint, budget)
                    unordered = any(
                        match_value(branch, endpoint_traits, endpoint) is True
                        for branch in field.values
                    )
                    if need_list and is_list and not unordered:
                        if members is None:
                            continue
                        observed = (
                            tuple((member, "list", False) for member in members)
                            if members
                            else ((endpoint, "list", True),)
                        )
                    else:
                        observed = ((endpoint, "unordered", False),)
                    for node, container, empty in observed:
                        budget.consume("work", 1 + len(field.values))
                        actual = _traits(graph, node, budget)
                        recognized, projected = (
                            project_direction(graph, node, rdf_direction, budget)
                            if any(branch.kind == "literal" for branch in field.values)
                            else (False, None)
                        )
                        literal_valid: bool | None = None
                        if isinstance(node, Literal) and not empty:
                            literal = (
                                Report("literal", 1)
                                if projected is not None
                                else check_literal(
                                    node,
                                    location=field_where,
                                    datatype_registry=datatype_registry,
                                    budget=budget,
                                )
                            )
                            literal_valid = literal.verified
                            checked += literal.checked
                            issues.extend(literal.issues)
                            if source.literal_fidelity not in {
                                "source_terms",
                                "supplied_terms",
                            }:
                                exact_values = False
                                issues.append(
                                    Issue(
                                        "literal.lexical_unverified",
                                        "Preparation did not retain "
                                        "exact source literal identity",
                                        field_where,
                                        kind="unverified",
                                        actual=source.literal_fidelity,
                                    )
                                )
                        matches = tuple(
                            None
                            if recognized
                            and projected is None
                            and branch.kind == "literal"
                            else None
                            if projected is not None
                            and branch.kind == "literal"
                            and not isinstance(node, Literal)
                            and branch.datatype is not None
                            else match_value(
                                branch,
                                projected
                                if branch.kind == "literal" and projected is not None
                                else actual,
                                node,
                                container=container,
                                empty=empty,
                            )
                            for branch in field.values
                        )
                        if not empty:
                            constrained: list[bool | None] = []
                            for branch, matched in zip(
                                field.values, matches, strict=True
                            ):
                                if matched is False or branch.constraints is None:
                                    constrained.append(matched)
                                    continue
                                application = application_matches(
                                    branch.constraints,
                                    node,
                                    budget=budget,
                                    lexical_valid=literal_valid,
                                )
                                constrained.append(
                                    False
                                    if application is False
                                    else None
                                    if matched is None or application is None
                                    else True
                                )
                            matches = tuple(constrained)
                        if True not in matches:
                            issues.append(
                                Issue(
                                    "emission.value_unverified"
                                    if None in matches
                                    else "emission.value_mismatch",
                                    "RDF value does not establish "
                                    "a declared alternative",
                                    field_where,
                                    kind="unverified" if None in matches else "finding",
                                    actual=actual.kind,
                                )
                            )
                        if (
                            projected is not None
                            and not isinstance(node, Literal)
                            and not empty
                        ):
                            checked += 1
                            if source.literal_fidelity not in {
                                "source_terms",
                                "supplied_terms",
                            }:
                                issues.append(
                                    Issue(
                                        "literal.lexical_unverified",
                                        "Preparation did not retain "
                                        "exact component literals",
                                        field_where,
                                        kind="unverified",
                                        actual=source.literal_fidelity,
                                    )
                                )
                        targets = {
                            branch.model
                            for branch, match in zip(field.values, matches, strict=True)
                            if branch.model is not None and match is not False
                        }
                        if targets and not empty and isinstance(node, URIRef | BNode):
                            established = {
                                target
                                for target in targets
                                if target in registry
                                and set(registry[target].types) <= set(actual.types)
                                and identity_matches(registry[target].subject, actual)
                            }
                            if len(established) == 1:
                                bind(RdfModelUsage(next(iter(established)), node))
                            elif not any(
                                (target, node) in selected for target in targets
                            ):
                                issues.append(
                                    Issue(
                                        "emission.nested_scope_unverified",
                                        "Linked model requires an "
                                        "unambiguous subject binding",
                                        field_where,
                                        kind="unverified",
                                    )
                                )
                if field.output is not None:
                    if not exact_values:
                        issues.append(
                            Issue(
                                "emission.cardinality_unverified",
                                "Exact RDF value identities are unavailable",
                                field_where,
                                kind="unverified",
                            )
                        )
                    issues.extend(
                        check_output_counts(
                            field.output,
                            distinct_values=len(values) if exact_values else None,
                            list_lengths=list_lengths,
                            non_list=non_list,
                            where=field_where,
                            budget=budget,
                        )
                    )
            if model.emission_groups:
                checked += len(model.emission_groups)
                issues.extend(
                    check_groups(
                        model, group_states, group_terms, where=where, budget=budget
                    )
                )
    except LimitExceeded as error:
        issues.exhausted(str(error), location="rdf-emission")
        complete = False
    return Report(
        "rdf-emission",
        checked,
        tuple(issues),
        children=(
            *enumeration_reports.values(),
            *(
                (role_checks.exhaustion,)
                if role_checks is not None and role_checks.exhaustion is not None
                else ()
            ),
        ),
        requested=checked if complete else checked + 1,
        complete=complete,
        regime="actual-rdf-output",
        families=("model-output",),
    )
