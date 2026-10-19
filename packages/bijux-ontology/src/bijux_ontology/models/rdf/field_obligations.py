"""Incremental model obligations over exact selected RDF subject and field facts."""

from itertools import chain

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF
from rdflib.term import Node

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.models.emission.counts import check_output_counts
from bijux_ontology.models.emission.coverage import predicate_issues
from bijux_ontology.models.emission.direction import project_direction
from bijux_ontology.models.emission.groups import TermKey, check_groups
from bijux_ontology.models.emission.matching import (
    ValueObservation,
    identity_matches,
    matches_value,
)
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.models.rdf.observations import observe_rdf_term
from bijux_ontology.models.resource_roles.checking import _RoleChecks
from bijux_ontology.models.value_constraints.checking import (
    application_matches,
)
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry
from bijux_ontology.rdf.literals.checking import check_literal
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.structure import read_rdf_list
from bijux_ontology.rdf.terms import term_key

from .preparation import RdfBindingDiscovery


class RdfFieldObligations:
    """Evaluate native subject/field observations while recursive bindings grow."""

    def __init__(
        self,
        source: PreparedSource,
        graph: Graph,
        registry: dict[str, ModelDeclaration],
        bindings: RdfBindingDiscovery,
        budget: Budget,
        issues: IssueCollector,
        role_checks: _RoleChecks | None,
        rdf_direction: DirectionPolicy | None,
        datatype_registry: DatatypeRegistry | None,
    ) -> None:
        self.source = source
        self.graph = graph
        self.registry = registry
        self.bindings = bindings
        self.budget = budget
        self.issues = issues
        self.role_checks = role_checks
        self.rdf_direction = rdf_direction
        self.datatype_registry = datatype_registry
        self.checked = 0

    def match_value(
        self,
        field: FieldDeclaration,
        branch: ValueDeclaration,
        traits: ValueObservation,
        node: Node,
        *,
        container: str = "unordered",
        empty: bool = False,
    ) -> bool | None:
        """Reuse selected physical terms and one operation's role contracts."""
        role_result = None
        if (
            branch.role is not None
            and self.role_checks is not None
            and isinstance(node, URIRef | BNode)
        ):
            role_result = self.role_checks.matches(
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
            self.registry,
            container=container,
            empty=empty,
            role_result=role_result,
        )

    def check_binding(self, binding: RdfModelUsage) -> None:
        """Finish known subject facts and each field before reading another binding."""
        graph = self.graph
        self.budget.consume("work")
        where = binding.location or f"entity[{binding.subject}]"
        model = self.registry.get(binding.model)
        if model is None:
            self.issues.append(
                Issue(
                    "input.model_reference_missing",
                    "RDF usage references an unknown model",
                    where,
                    kind="input_error",
                    actual=binding.model,
                )
            )
            return
        self.checked += 1
        subject = binding.subject
        if subject not in self.bindings.nodes:
            self.issues.append(
                Issue(
                    "emission.reference_unverified",
                    "No selected RDF description was supplied for this subject",
                    where,
                    kind="unverified",
                    actual=str(subject),
                )
            )
            return
        traits = observe_rdf_term(graph, subject, self.budget)
        if not identity_matches(model.subject, traits):
            self.issues.append(
                Issue(
                    "emission.subject_mismatch",
                    "Subject violates the explicit RDF identity contract",
                    where,
                    expected=model.subject.expected_iri or "an IRI subject",
                    actual=str(subject),
                )
            )
        if set(model.types) - set(traits.types):
            self.issues.append(
                Issue(
                    "emission.types_mismatch",
                    "Subject lacks required asserted types in this graph",
                    where,
                    expected=", ".join(model.types),
                    actual=", ".join(traits.types),
                )
            )
        if model.coverage is not None:
            self.checked += 1
            self.issues.append(
                Issue(
                    "emission.source_coverage_unverified",
                    "Native RDF does not establish original source-key coverage",
                    where,
                    kind="unverified",
                )
            )
            self.issues.extend(
                predicate_issues(
                    model,
                    chain(
                        (
                            (str(predicate), False)
                            for _, predicate, _ in graph.triples((subject, None, None))
                        ),
                        (
                            (str(predicate), True)
                            for _, predicate, _ in graph.triples((None, None, subject))
                        ),
                    ),
                    where=where,
                    budget=self.budget,
                )
            )
        group_states: dict[str, bool | None] = {}
        group_terms: dict[str, tuple[TermKey, ...] | None] = {}
        for field in model.fields:
            self.budget.consume("work")
            self.checked += 1
            field_where = f"{where}.{field.name}"
            endpoints: tuple[Node, ...]
            if field.predicate == "@id":
                endpoints = (subject,) if not field.reverse else ()
            elif field.predicate == "@type":
                endpoints = (
                    tuple(graph.objects(subject, RDF.type)) if not field.reverse else ()
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
                        or (
                            isinstance(value, Literal)
                            and self.source.literal_fidelity
                            not in {"source_terms", "supplied_terms"}
                        )
                        for value in endpoints
                    )
                    else tuple(dict.fromkeys(term_key(value) for value in endpoints))
                )
            self.check_field(field, endpoints, field_where)
        if model.emission_groups:
            self.checked += len(model.emission_groups)
            self.issues.extend(
                check_groups(
                    model, group_states, group_terms, where=where, budget=self.budget
                )
            )

    def check_field(
        self,
        field: FieldDeclaration,
        endpoints: tuple[Node, ...],
        field_where: str,
    ) -> None:
        """Interleave list/term observation, finite checks and child discovery."""
        graph = self.graph
        if not endpoints and field.emission == "guaranteed":
            self.issues.append(
                Issue(
                    "emission.required_entity_fact_missing",
                    "Required predicate is absent from the selected graph",
                    field_where,
                )
            )
        if field.output is not None and field.output.require_property:
            self.issues.append(
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
            self.budget.consume("work")
            values.add(term_key(endpoint))
            need_list = any(branch.container == "@list" for branch in field.values) or (
                field.output is not None
                and (
                    field.output.minimum_list_members is not None
                    or field.output.maximum_list_members is not None
                )
            )
            is_list = (
                endpoint == RDF.nil
                or next(graph.objects(endpoint, RDF.first), None) is not None
                or next(graph.objects(endpoint, RDF.rest), None) is not None
                or ((endpoint, RDF.type, RDF.List) in graph)
            )
            members: tuple[Node, ...] | None = None
            if need_list and is_list:
                try:
                    members = read_rdf_list(
                        graph,
                        endpoint,
                        max_items=self.budget.limits.max_work,
                        budget=self.budget,
                    )
                    self.budget.consume("work", len(members))
                    list_lengths.append(len(members))
                except InputError as error:
                    if isinstance(error, LimitExceeded):
                        raise
                    list_lengths.append(None)
                    self.issues.append(
                        Issue(
                            "emission.value_unverified",
                            str(error),
                            field_where,
                            kind="unverified",
                        )
                    )
            elif need_list:
                non_list = True
            endpoint_traits = observe_rdf_term(graph, endpoint, self.budget)
            unordered = any(
                self.match_value(field, branch, endpoint_traits, endpoint) is True
                for branch in field.values
            )
            if need_list and is_list and (not unordered):
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
                if not self.check_value(field, node, container, empty, field_where):
                    exact_values = False

        if field.output is not None:
            if not exact_values:
                self.issues.append(
                    Issue(
                        "emission.cardinality_unverified",
                        "Exact RDF value identities are unavailable",
                        field_where,
                        kind="unverified",
                    )
                )
            self.issues.extend(
                check_output_counts(
                    field.output,
                    distinct_values=len(values) if exact_values else None,
                    list_lengths=list_lengths,
                    non_list=non_list,
                    where=field_where,
                    budget=self.budget,
                )
            )

    def check_value(
        self,
        field: FieldDeclaration,
        node: Node,
        container: str,
        empty: bool,
        field_where: str,
    ) -> bool:
        """Finish one original term's obligations before discovering its child."""
        graph = self.graph
        exact_values = True
        self.budget.consume("work", 1 + len(field.values))
        actual = observe_rdf_term(graph, node, self.budget)
        recognized, projected = (
            project_direction(graph, node, self.rdf_direction, self.budget)
            if any(branch.kind == "literal" for branch in field.values)
            else (False, None)
        )
        literal_valid: bool | None = None
        if isinstance(node, Literal) and (not empty):
            literal = (
                Report("literal", 1)
                if projected is not None
                else check_literal(
                    node,
                    location=field_where,
                    datatype_registry=self.datatype_registry,
                    budget=self.budget,
                )
            )
            literal_valid = literal.verified
            self.checked += literal.checked
            self.issues.extend(literal.issues)
            if self.source.literal_fidelity not in {
                "source_terms",
                "supplied_terms",
            }:
                exact_values = False
                self.issues.append(
                    Issue(
                        "literal.lexical_unverified",
                        "Preparation did not retain exact source literal identity",
                        field_where,
                        kind="unverified",
                        actual=self.source.literal_fidelity,
                    )
                )
        matches = tuple(
            None
            if recognized and projected is None and (branch.kind == "literal")
            else None
            if projected is not None
            and branch.kind == "literal"
            and (not isinstance(node, Literal))
            and (branch.datatype is not None)
            else self.match_value(
                field,
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
            for branch, matched in zip(field.values, matches, strict=True):
                if matched is False or branch.constraints is None:
                    constrained.append(matched)
                    continue
                application = application_matches(
                    branch.constraints,
                    node,
                    budget=self.budget,
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
            self.issues.append(
                Issue(
                    "emission.value_unverified"
                    if None in matches
                    else "emission.value_mismatch",
                    "RDF value does not establish a declared alternative",
                    field_where,
                    kind="unverified" if None in matches else "finding",
                    actual=actual.kind,
                )
            )
        if projected is not None and (not isinstance(node, Literal)) and (not empty):
            self.checked += 1
            if self.source.literal_fidelity not in {
                "source_terms",
                "supplied_terms",
            }:
                self.issues.append(
                    Issue(
                        "literal.lexical_unverified",
                        "Preparation did not retain exact component literals",
                        field_where,
                        kind="unverified",
                        actual=self.source.literal_fidelity,
                    )
                )
        self.bindings.discover(
            field, node, actual, matches, empty=empty, field_where=field_where
        )
        return exact_values
