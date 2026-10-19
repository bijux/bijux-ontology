"""Owned retained roots, explicit omissions and witnessed framing preservation."""

import json

from rdflib import BNode, Literal, URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    bound_text,
    retain_report,
)
from bijux_ontology.jsonld.evidence.entities import EntityKey
from bijux_ontology.jsonld.selectors.types import EntitySelector
from bijux_ontology.jsonld.usages.checking import PreparedUsages
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node

from .types import FrameOmission


def capture_predicates(values: tuple[str, ...], budget: Budget) -> frozenset[str]:
    """Own finite named RDF predicates before hashing or policy comparison."""
    if type(values) is not tuple:
        raise InputError("Framing predicate scope requires immutable IRI tuples")
    budget.consume("states", len(values))
    budget.consume("bytes", 64 + 16 * len(values))
    result = set()
    for value in values:
        if not isinstance(value, str) or isinstance(value, BNode | Literal):
            raise InputError("Framing predicates require named IRI strings")
        bound_text(value, budget)
        absolute_iri(value)
        result.add(str(value))
    return frozenset(result)


def select_records(
    view: PreparedUsages,
    selector: EntitySelector,
    budget: Budget,
    issues: IssueCollector,
    children: list[Report],
) -> tuple[EntityKey, ...]:
    """Retain actual quantified selection truth before adopting matched identities."""
    if (
        selector.coverage_unit != "rdf_entity"
        or selector.steps
        or selector.include_references
    ):
        raise InputError("Framing roots require describing RDF entity selection")
    selection = view.select(selector)
    retain_report(selection.report, budget, issues)
    budget.consume("states")
    children.append(selection.report)
    if selection.report.outcome == "exhausted":
        raise LimitExceeded("Framing selection exceeded the operation limits")
    return selection.entities


def adopt_report(
    report: Report, budget: Budget, issues: IssueCollector, children: list[Report]
) -> None:
    """Apply this budget's limits before required child adoption."""
    retain_report(report, budget, issues)
    budget.consume("states")
    children.append(report)


class FramingPreservation:
    """One operation's finite preservation scope and observed permission evidence.

    Native framing, RDF comparison and candidate publication stay with the public
    coordinator. These exact roots and policies apply jointly to both projections.
    Progress references are shared so later exhaustion retains paid exclusions.
    """

    __slots__ = (
        "retained",
        "selected_predicates",
        "permitted_predicates",
        "omitted_predicates",
        "budget",
        "issues",
        "children",
        "exclusions",
        "excluded_scopes",
        "names",
        "selected_keys",
        "permitted_keys",
        "exercised_permissions",
        "requested",
        "graph_name",
    )

    def __init__(
        self,
        retained: EntitySelector,
        selected_predicates: frozenset[str],
        permitted_predicates: frozenset[str],
        omitted_predicates: tuple[str, ...],
        budget: Budget,
        issues: IssueCollector,
        children: list[Report],
        exclusions: list[FrameOmission],
        excluded_scopes: list[str],
    ) -> None:
        budget.consume("states")
        budget.reserve_storage(384)
        self.retained = retained
        self.selected_predicates, self.permitted_predicates = (
            selected_predicates,
            permitted_predicates,
        )
        self.omitted_predicates = omitted_predicates
        self.budget, self.issues, self.children = budget, issues, children
        self.exclusions, self.excluded_scopes = exclusions, excluded_scopes
        self.names: list[str] = []
        self.selected_keys: set[EntityKey] = set()
        self.permitted_keys: set[EntityKey] = set()
        self.exercised_permissions: set[str] = set()
        self.requested = 1
        self.graph_name = retained.graph if retained.graph is not None else "@default"

    def unknown(self, message: str) -> None:
        """Retain required unknown scope without inventing source metadata."""
        self.issues.append(
            Issue("context.frame_unverified", message, kind="unverified")
        )

    def omission(self, value: FrameOmission) -> None:
        """Expose broad RDF exclusions without manufacturing source pointers."""
        texts = (value.identifier, value.graph or "", *(value.predicates or ()))
        estimated = 256 + 6 * sum(len(text) for text in texts)
        if estimated > self.budget.limits.max_bytes:
            raise LimitExceeded("Framing exclusion metadata exceeds max_bytes")
        self.budget.consume("work", estimated)
        self.budget.consume("bytes", estimated)
        self.budget.consume("states")
        encoded = json.dumps(
            {
                "record": value.identifier,
                "graph": value.graph,
                "predicates": value.predicates,
            },
            separators=(",", ":"),
        )
        self.exclusions.append(value)
        self.excluded_scopes.append(encoded)

    def capture(
        self,
        before_view: PreparedUsages,
        omission_selectors: tuple[EntitySelector, ...],
    ) -> None:
        """Own exact named roots and permitted record omissions."""
        selected = select_records(
            before_view, self.retained, self.budget, self.issues, self.children
        )
        self.requested = max(1, len(selected))
        self.names = []
        self.selected_keys = set()
        for key in selected:
            self.budget.consume("work")
            self.budget.consume("states")
            if (
                key.identifier is None
                or key.identifier.startswith("_:")
                or key.anonymous_graph
            ):
                self.unknown("Retained roots need named subject and graph identity")
                continue
            bound_text(key.identifier, self.budget)
            self.names.append(key.identifier)
            self.selected_keys.add(key)
        if not self.names:
            self.unknown(
                "No required named record was established for retained framing"
            )
        self.permitted_keys = set()
        for selector in omission_selectors:
            matches = select_records(
                before_view, selector, self.budget, self.issues, self.children
            )
            if selector.graph != self.retained.graph:
                raise InputError(
                    "Framing record allowances must use the selected physical graph"
                )
            for key in matches:
                self.budget.consume("work")
                self.budget.consume("states")
                if (
                    key.identifier is None
                    or key.identifier.startswith("_:")
                    or key.anonymous_graph
                ):
                    self.unknown("Record omission needs established named identity")
                    continue
                bound_text(key.identifier, self.budget)
                if key in self.selected_keys:
                    raise InputError("A required framing record cannot also be omitted")
                self.permitted_keys.add(key)
                self.omission(FrameOmission(key.identifier, key.graph))
        for entity in before_view.entities.entities:
            self.budget.consume("work")
            key = entity.key
            if (
                entity.described
                and not key.anonymous_graph
                and key.graph == self.retained.graph
                and key.identifier is not None
                and not key.identifier.startswith("_:")
            ):
                bound_text(key.identifier, self.budget)
                if key not in self.selected_keys and key not in self.permitted_keys:
                    self.unknown(
                        "A source record is outside the retained/omitted inventory"
                    )

    def qualify_source(self, source: PreparedSource) -> None:
        """Require every source predicate and omission permission to be exercised."""
        before_graph = source._read(self.graph_name, budget=self.budget)
        self.exercised_permissions = set()
        for name in self.names:
            retained_count = 0
            excluded_predicates = set()
            for _, predicate, value in before_graph.triples((URIRef(name), None, None)):
                self.budget.consume("work")
                bound_node(predicate, self.budget)
                bound_node(value, self.budget)
                if str(predicate) in self.selected_predicates:
                    retained_count += 1
                elif str(predicate) in self.permitted_predicates:
                    self.budget.consume("states")
                    excluded_predicates.add(str(predicate))
                    self.exercised_permissions.add(str(predicate))
                else:
                    self.unknown(
                        "A source seed predicate has no retention or omission policy"
                    )
            if not retained_count:
                self.unknown(
                    "A required named record supplies no selected retained assertion"
                )
            if excluded_predicates:
                self.budget.consume("work", len(self.omitted_predicates))
                self.budget.consume("bytes", 16 * len(excluded_predicates))
                self.omission(
                    FrameOmission(
                        name,
                        self.retained.graph,
                        tuple(
                            value
                            for value in self.omitted_predicates
                            if value in excluded_predicates
                        ),
                    )
                )
        if self.exercised_permissions != self.permitted_predicates:
            self.unknown("A predicate omission has no observed retained-seed scope")

    def qualify_output(
        self, after_view: PreparedUsages, source: PreparedSource
    ) -> None:
        """Reject descriptions and predicates outside the captured scope."""
        for entity in after_view.entities.entities:
            self.budget.consume("work")
            key = entity.key
            if (
                entity.described
                and not key.anonymous_graph
                and key.graph == self.retained.graph
                and key.identifier is not None
                and not key.identifier.startswith("_:")
            ):
                bound_text(key.identifier, self.budget)
                if key not in self.selected_keys and key not in self.permitted_keys:
                    self.unknown(
                        "An output description is outside the projection inventory"
                    )
        after_graph = source._read(self.graph_name, budget=self.budget)
        for name in self.names:
            for _, predicate, value in after_graph.triples((URIRef(name), None, None)):
                self.budget.consume("work")
                bound_node(predicate, self.budget)
                bound_node(value, self.budget)
                if (
                    str(predicate) not in self.selected_predicates
                    and str(predicate) not in self.permitted_predicates
                ):
                    self.unknown(
                        "An output seed predicate is outside the projection scope"
                    )
