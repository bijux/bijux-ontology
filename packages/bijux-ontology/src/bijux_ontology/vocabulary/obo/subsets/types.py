"""Native OBO subset definitions and exact application enumeration evidence."""

from dataclasses import dataclass

from rdflib import BNode, Literal, URIRef

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.vocabulary.browsing.results import Edge


@dataclass(frozen=True)
class OboSubsetDefinition:
    """Original direct definition witnesses and exact outgoing metadata.

    A definition is an annotation convention; it does not declare a SKOS scheme
    or assert that the subset is a logically conservative ontology module.
    """

    iri: URIRef
    assertions: tuple[Edge, ...]
    metadata: tuple[Edge, ...]


@dataclass(frozen=True)
class OboSubsets:
    """Selected source inventory, including malformed membership assertions."""

    definitions: tuple[OboSubsetDefinition, ...]
    memberships: tuple[Edge, ...]
    assertions: tuple[Edge, ...]
    closed: bool
    report: Report
    limits: Limits

    def select(self, iri: str, *, budget: Budget | None = None) -> ValueEnumeration:
        """Use original named members through the ordinary allowed-value engine.

        Closed absence excludes only after the complete inventory is qualified.
        Anonymous members remain visible in memberships, but cannot supply a
        guessed named application value. The original inventory stays required.
        """
        if not isinstance(iri, str) or isinstance(iri, BNode | Literal):
            raise InputError("OBO subset selection requires a named IRI string")
        if budget is not None and not isinstance(budget, Budget):
            raise InputError("Expected an optional shared Budget")
        operation = budget if budget is not None else Budget(self.limits)
        issues = IssueCollector(operation)
        children: list[Report] = []
        values: list[URIRef] = []
        seen: set[URIRef] = set()
        checked = 0
        complete = False
        anchor = ""
        try:
            bound_text(iri, operation)
            absolute_iri(iri)
            anchor = iri
            retain_report(self.report, operation, issues)
            children.append(self.report)
            complete = self.report.complete
            selected = URIRef(iri)
            defined = False
            for definition in self.definitions:
                operation.consume("work")
                bound_node(definition.iri, operation)
                defined |= definition.iri == selected
            checked += 1
            if not defined:
                issues.append(
                    Issue(
                        "obo.subset_undefined",
                        "Selected OBO subset has no supplied direct definition",
                        anchor,
                        kind="finding" if self.closed and complete else "unverified",
                    )
                )
                complete = False
            for edge in self.memberships:
                operation.consume("work")
                bound_node(edge.object, operation)
                if edge.object != selected:
                    continue
                bound_node(edge.subject, operation)
                if isinstance(edge.subject, URIRef) and edge.subject not in seen:
                    operation.consume("states", 2)
                    seen.add(edge.subject)
                    values.append(edge.subject)
                elif not isinstance(edge.subject, URIRef):
                    complete = False
                    issues.append(
                        Issue(
                            "obo.subset_unverified",
                            "Anonymous membership cannot establish a named "
                            "allowed value",
                            anchor,
                            kind="unverified",
                        )
                    )
                checked += 1
        except LimitExceeded as error:
            complete = False
            issues.exhausted(str(error), location=anchor)
        return ValueEnumeration(
            tuple(values),
            closed=self.closed,
            report=Report(
                "obo.subset_selection",
                checked,
                tuple(issues),
                allow_empty=True,
                requested=checked if complete else checked + 1,
                complete=complete,
                regime="asserted-obo-subset",
                children=tuple(children),
            ),
        )
