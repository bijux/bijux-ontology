"""Finite Schema.org guidance and separately selected application expectations."""

import threading
from collections.abc import Sequence
from typing import Literal as Choice

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.owl.classes import ClassContracts
from bijux_ontology.rdf.literals import check_literal, datatype_relation
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node, literal_datatype, term_key
from bijux_ontology.reports import Issue, Location, Report

from .expectations import _edges, schemaorg_property
from .preparation import _included, _mapped, _view
from .types import Mode, SchemaOrgCheck, SchemaOrgUse, SchemaOrgView, _iri


class _MissingRelease(Exception):
    """The required release is unavailable rather than a proved data mismatch."""


class _Matching:
    """One operation's bounded existing named ancestry and exact sample checks."""

    def __init__(
        self, view: SchemaOrgView, release: Graph, data: Graph, budget: Budget
    ) -> None:
        self.view, self.release, self.data, self.budget = view, release, data, budget
        self.classes = ClassContracts(release, budget)
        self.samples: dict[tuple[str | None, ...], Report] = {}

    def sample(self, value: Literal) -> Report:
        """Validate original literal spelling once, without changing the term."""
        bound_node(value, self.budget)
        key = term_key(value)
        if key not in self.samples:
            self.budget.consume("states")
            self.samples[key] = check_literal(value, budget=self.budget)
        return self.samples[key]

    def ancestors(self, names: tuple[str, ...]) -> tuple[set[str], bool]:
        """Follow explicit alias hops using the same bounded named-class helper."""
        self.budget.consume("states", len(names))
        pending = list(names)
        seen: set[str] = set()
        retained: set[str] = set()
        unsupported = False
        while pending:
            name = pending.pop()
            bound_text(name, self.budget)
            if name in seen:
                continue
            self.budget.consume("states")
            seen.add(name)
            ancestors, unknown = self.classes.ancestors((name,))
            unsupported |= unknown
            for ancestor in ancestors:
                self.budget.consume("work")
                bound_text(ancestor, self.budget)
                mapped = _mapped(self.view, URIRef(ancestor), self.budget)
                bound_node(mapped, self.budget)
                self.budget.consume("states")
                retained.add(str(mapped))
                if str(mapped) != ancestor:
                    self.budget.consume("states")
                    pending.append(str(mapped))
        return retained, unsupported

    def datatype(self, expected: URIRef) -> bool:
        """Recognize published datatype subclasses through existing named ancestry."""
        ancestors, _ = self.ancestors((str(expected),))
        for ancestor in ancestors:
            self.budget.consume("work")
            bound_text(ancestor, self.budget)
            node = URIRef(ancestor)
            if not _included(self.view, self.release, node, self.budget):
                continue
            if any(
                edge[2] == URIRef(str(self.view.namespace) + "DataType")
                for edge in _edges(self.release, node, RDF.type, self.budget)
            ):
                return True
        return False

    def scalar(self, value: Node, expected: URIRef) -> bool | None:
        """Use a documented finite RDF encoding bridge, never lexical retyping."""
        ns = str(self.view.namespace)
        if expected == URIRef(ns + "URL") and isinstance(value, URIRef):
            return True
        if not isinstance(value, Literal):
            return False
        if not self.sample(value).verified:
            return None
        actual = literal_datatype(value)
        if expected == URIRef(ns + "Text"):
            return actual == RDF.langString or datatype_relation(
                str(actual), str(XSD.string)
            ) in ("equal", "subset")
        if expected == URIRef(ns + "Boolean"):
            return actual == XSD.boolean
        if expected == URIRef(ns + "URL"):
            return actual == XSD.anyURI
        if expected == URIRef(ns + "Integer"):
            relation = datatype_relation(str(actual), str(XSD.integer))
            if relation in ("equal", "subset"):
                return True
            # Decimal value membership requires a stronger value-space bridge.
            return (
                None
                if datatype_relation(str(actual), str(XSD.decimal))
                in ("equal", "subset")
                else False
            )
        if expected == URIRef(ns + "Number"):
            return datatype_relation(str(actual), str(XSD.decimal)) in (
                "equal",
                "subset",
            ) or actual in (XSD.float, XSD.double)
        if expected == URIRef(ns + "Float"):
            return actual in (XSD.float, XSD.double)
        return None

    def member(self, value: Node, expected: Node) -> bool | None:
        """Match selected named classes against actual types and named ancestors."""
        if not isinstance(expected, URIRef):
            return None
        expected = _mapped(self.view, expected, self.budget)
        if not _included(self.view, self.release, expected, self.budget):
            return None
        kinds = _edges(self.release, expected, RDF.type, self.budget)
        datatype = self.datatype(expected)
        if datatype:
            return self.scalar(value, expected)
        if not any(
            kind[2]
            in (RDFS.Class, OWL.Class, URIRef(str(self.view.namespace) + "Class"))
            for kind in kinds
        ):
            return None
        if isinstance(value, Literal):
            return False
        actual: list[str] = []
        unknown = False
        for kind in self.data.objects(value, RDF.type):
            self.budget.consume("work")
            bound_node(kind, self.budget)
            if not isinstance(kind, URIRef):
                unknown = True
                continue
            mapped = _mapped(self.view, kind, self.budget)
            if not _included(self.view, self.release, mapped, self.budget):
                unknown = True
                continue
            kinds = _edges(self.release, mapped, RDF.type, self.budget)
            if not any(
                edge[2]
                in (RDFS.Class, OWL.Class, URIRef(str(self.view.namespace) + "Class"))
                for edge in kinds
            ):
                unknown = True
                continue
            if mapped == expected:
                return True
            self.budget.consume("states")
            actual.append(str(mapped))
        if not actual:
            return None
        ancestors, unsupported = self.ancestors(tuple(actual))
        outside = False
        for ancestor in ancestors:
            self.budget.consume("work")
            bound_text(ancestor, self.budget)
            if not _included(self.view, self.release, URIRef(ancestor), self.budget):
                outside = True
        if str(expected) in ancestors and not unsupported and not outside:
            return True
        if unknown or unsupported or outside:
            return None
        return False

    def alternatives(self, value: Node, expected: tuple[Node, ...]) -> bool | None:
        """One established alternative suffices; unknown never proves exclusion."""
        if not expected:
            return True
        if value == RDF.nil:
            return None
        if isinstance(value, URIRef | BNode):
            for predicate in (RDF.first, RDF.rest):
                for _ in self.data.triples((value, predicate, None)):
                    self.budget.consume("work")
                    return None
            for kind in self.data.objects(value, RDF.type):
                self.budget.consume("work")
                bound_node(kind, self.budget)
                if kind in (RDF.List, RDF.Seq, RDF.Bag, RDF.Alt):
                    return None
        unknown = False
        for kind in expected:
            self.budget.consume("work")
            result = self.member(value, kind)
            if result is True:
                return True
            unknown |= result is None
        return None if unknown else False

    def text_fallback(self, value: Node, expected: tuple[Node, ...]) -> bool:
        """Allow ordinary text only through an explicit application choice."""
        if not isinstance(value, Literal) or literal_datatype(value) not in (
            XSD.string,
            RDF.langString,
        ):
            return False
        if not self.sample(value).verified:
            return False
        for kind in expected:
            self.budget.consume("work")
            if not isinstance(kind, URIRef):
                continue
            kind = _mapped(self.view, kind, self.budget)
            if not _included(self.view, self.release, kind, self.budget):
                continue
            if not self.datatype(kind) and any(
                edge[2]
                in (RDFS.Class, OWL.Class, URIRef(str(self.view.namespace) + "Class"))
                for edge in _edges(self.release, kind, RDF.type, self.budget)
            ):
                return True
        return False


def check_schemaorg(
    data: Graph | PreparedSource,
    view: SchemaOrgView,
    *,
    subjects: Sequence[URIRef | BNode],
    predicates: Sequence[URIRef],
    mode: Mode = "published",
    text_fallback: Choice["allow", "reject"] = "reject",
    lifecycle: Choice["ignore", "reject"] = "ignore",
    graph_name: GraphSelection | None = None,
    union: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> SchemaOrgCheck:
    """Check actual selected RDF against a pinned, explicit Schema.org dialect.

    Published mode reports advisory mismatch, never OWL inconsistency or strict
    application conformance. Application mode explicitly selects finite endpoint
    expectations, actual asserted/named-subclass types and optional text fallback.
    Unknown or missing type proof cannot establish a negative expectation. HTTP
    and HTTPS names remain distinct unless the caller supplied a finite IRI map.
    RDF list/container expectations are unverified in this initial finite profile.
    """
    if (
        mode not in ("published", "application")
        or text_fallback not in ("allow", "reject")
        or lifecycle not in ("ignore", "reject")
    ):
        raise InputError(
            "Select explicit Schema.org guidance/application policy options"
        )
    if type(union) is not bool:
        raise InputError("Schema.org data union selection requires a boolean")
    operation = Operation(limits, budget, timeout, cancel)
    published, application = IssueCollector(operation), IssueCollector(operation)
    uses: list[SchemaOrgUse] = []
    qualified_view = None
    source = None
    effective_graph = None
    effective_union = False
    checked = 0
    requested = 1
    children: list[Report] = []

    def issue(code: str, message: str, subject: Node, *, unknown: bool = False) -> None:
        """Keep raw guidance distinct from selected application-policy findings."""
        bound_node(subject, operation)
        if mode == "application":
            application.append(
                Issue(
                    code,
                    message,
                    str(subject),
                    kind="unverified" if unknown else "finding",
                    details=Location(term=str(subject)),
                )
            )
        published.append(
            Issue(
                code,
                message,
                str(subject),
                kind="unverified" if unknown else "finding",
                severity="warning",
                details=Location(term=str(subject)),
            )
        )

    try:
        release = _view(view, operation)
        retain_report(view.report, operation, published)
        if mode == "application":
            retain_report(view.report, operation, application)
        children.append(view.report)
        qualified_view = view
        selected_subjects = capture_items(subjects, object, operation)
        for subject in selected_subjects:
            if not isinstance(subject, URIRef | BNode):
                raise InputError("Schema.org subjects require exact RDF resources")
            bound_node(subject, operation)
        selected_predicates = capture_items(predicates, URIRef, operation)
        for predicate in selected_predicates:
            _iri(predicate, operation)
        requested = max(1, len(selected_subjects) * len(selected_predicates))
        if not selected_subjects or not selected_predicates:
            raise InputError("Schema.org checks require explicit nonempty selections")
        if release is None:
            raise _MissingRelease("Required release preparation did not complete")
        source = (
            data
            if isinstance(data, PreparedSource)
            else prepare_rdf(data, budget=operation)
        )
        if graph_name is not None:
            bound_text(graph_name, operation)
            if isinstance(graph_name, BNode) and not any(
                graph_name is name for name in source._graph_names(budget=operation)
            ):
                raise InputError("Blank graph selection requires its actual owned term")
        actual = source._read(graph_name, union=union, budget=operation)
        if source.document_iri is not None:
            bound_text(source.document_iri, operation)
        effective_graph = (
            source.graph_name if source.graph_name is not None else graph_name
        )
        effective_union = source.union or union
        matching = _Matching(view, release, actual, operation)
        properties = {}
        seen: set[tuple[tuple[str | None, ...], ...]] = set()
        for subject in selected_subjects:
            assert isinstance(subject, URIRef | BNode)
            for predicate in selected_predicates:
                operation.consume("work")
                if predicate not in properties:
                    expectation = schemaorg_property(view, predicate, budget=operation)
                    operation.consume("states")
                    properties[predicate] = expectation
                    retain_report(expectation.report, operation, published)
                    if mode == "application":
                        retain_report(expectation.report, operation, application)
                    operation.consume("states")
                    children.append(expectation.report)
                expectation = properties[predicate]
                found = False
                for owner, relation, value in actual.triples(
                    (None if isinstance(subject, BNode) else subject, predicate, None)
                ):
                    operation.consume("work")
                    for term in (owner, relation, value):
                        bound_node(term, operation)
                    if isinstance(subject, BNode) and subject is not owner:
                        continue
                    found = True
                    key = tuple(term_key(term) for term in (owner, relation, value))
                    if key in seen:
                        continue
                    operation.consume("states")
                    seen.add(key)
                    domain = matching.alternatives(owner, expectation.domains)
                    ranged = matching.alternatives(value, expectation.ranges)
                    faithful_literal = not isinstance(
                        value, Literal
                    ) or source.literal_fidelity in ("source_terms", "supplied_terms")
                    if not faithful_literal:
                        ranged = None
                    fallback = (
                        mode == "application"
                        and text_fallback == "allow"
                        and ranged is False
                        and faithful_literal
                        and matching.text_fallback(value, expectation.ranges)
                    )
                    operation.consume("states")
                    operation.consume("bytes", 128)
                    uses.append(
                        SchemaOrgUse(
                            (owner, relation, value),
                            expectation,
                            domain,
                            ranged,
                            fallback,
                        )
                    )
                    for endpoint, result in (("domain", domain), ("range", ranged)):
                        if result is True:
                            continue
                        bound_node(owner, operation)
                        diagnostic = Issue(
                            "schemaorg.expectation_unverified"
                            if result is None
                            else "schemaorg.expectation_mismatch",
                            "Actual endpoint lacks established "
                            + endpoint
                            + " membership in the supplied alternatives",
                            str(owner),
                            kind="unverified" if result is None else "finding",
                            severity="warning",
                        )
                        if mode == "application" and not (
                            endpoint == "range" and fallback
                        ):
                            application.append(
                                Issue(
                                    diagnostic.code,
                                    diagnostic.message,
                                    diagnostic.location,
                                    kind=diagnostic.kind,
                                )
                            )
                        published.append(diagnostic)
                    if isinstance(value, Literal):
                        lexical = matching.sample(value)
                        retain_report(lexical, operation, published)
                        if mode == "application":
                            retain_report(lexical, operation, application)
                    if lifecycle == "reject":
                        for replacement in expectation.superseded_by:
                            bound_node(replacement, operation)
                            item = Issue(
                                "schemaorg.superseded",
                                "Selected term is superseded in the supplied release",
                                str(predicate),
                                severity="warning",
                                expected=str(replacement),
                            )
                            if mode == "application":
                                application.append(
                                    Issue(
                                        item.code,
                                        item.message,
                                        item.location,
                                        expected=item.expected,
                                    )
                                )
                            published.append(item)
                if not found:
                    issue(
                        "schemaorg.selection_unverified",
                        "Selected subject/property pair has no owned assertion",
                        subject,
                        unknown=True,
                    )
                checked += 1
    except _MissingRelease as error:
        try:
            item = Issue(
                "schemaorg.expectation_unverified", str(error), kind="unverified"
            )
            if mode == "application":
                application.append(item)
            published.append(item)
        except LimitExceeded as exhausted:
            published.exhausted(str(exhausted))
            application.exhausted(str(exhausted))
    except LimitExceeded as error:
        published.exhausted(str(error))
        application.exhausted(str(error))
    guidance = Report(
        "schemaorg.guidance",
        checked,
        tuple(published),
        requested=requested,
        children=tuple(children),
        regime="schemaorg-published-guidance",
    )
    strict = (
        Report(
            "schemaorg.application",
            checked,
            tuple(application),
            requested=requested,
            children=tuple(children),
            regime="schemaorg-application-expectations",
        )
        if mode == "application"
        else None
    )
    return SchemaOrgCheck(
        qualified_view,
        source,
        effective_graph,
        effective_union,
        tuple(uses),
        guidance,
        strict,
        strict if strict is not None else guidance,
    )
