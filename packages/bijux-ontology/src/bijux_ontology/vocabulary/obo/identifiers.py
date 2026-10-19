"""Explicit OBO alternate identifiers retain all asserted canonical candidates."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal as Choice

from rdflib import Literal, URIRef
from rdflib.namespace import XSD

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, retain_report
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.patches.planning import capture_items
from bijux_ontology.rdf.terms import bound_node, bound_text
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.inspection import VocabularyView
from bijux_ontology.vocabulary.registry.types import IdentifierRegistry
from bijux_ontology.vocabulary.results import Deprecation, Edge, Selection, TermInfo

_ALTERNATIVE = URIRef("http://www.geneontology.org/formats/oboInOwl#hasAlternativeId")
IdentifierMode = Choice["literal", "curie", "iri"]


@dataclass(frozen=True)
class AlternativeIdCandidate:
    """One canonical subject, all original alias assertions and its declared state."""

    term: TermInfo
    assertions: tuple[Edge, ...]
    deprecation: Deprecation


@dataclass(frozen=True)
class AlternativeIdResolution:
    """One original request; incomplete observations never establish uniqueness."""

    identifier: str
    candidates: tuple[AlternativeIdCandidate, ...]
    status: Choice["unique", "ambiguous", "missing", "incomplete"]


def resolve_alternative_ids(
    view: VocabularyView,
    identifiers: Sequence[str],
    *,
    mode: IdentifierMode = "literal",
    registry: IdentifierRegistry | None = None,
    budget: Budget | None = None,
) -> Selection[AlternativeIdResolution]:
    """Resolve explicit hasAlternativeId assertions without rewriting consumer data.

    Literal mode compares exact case-sensitive strings. CURIE mode expands both
    requests and asserted aliases with the supplied registry; IRI mode interprets
    both as exact absolute IRIs. No label, replacement hint or namespace guessing
    supplies an alias. Returned candidates are observations; only complete selected
    inventory and declaration evidence can qualify a unique resolution. Absence of
    a deprecation assertion remains absent, rather than an asserted active state.
    """
    if not isinstance(view, VocabularyView) or mode not in ("literal", "curie", "iri"):
        raise InputError("Alternate identifiers require a vocabulary and explicit mode")
    if (mode == "curie" and not isinstance(registry, IdentifierRegistry)) or (
        mode != "curie" and registry is not None
    ):
        raise InputError("Supply an identifier registry exactly for CURIE mode")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Alternate identifier budget requires a Budget")
    operation = budget if budget is not None else Budget(view.limits)
    issues = IssueCollector(operation)
    children: list[Report] = []
    requests: tuple[str, ...] = ()
    observed: dict[str, dict[URIRef, list[Edge]]] = {}
    candidates: dict[str, list[AlternativeIdCandidate]] = {}
    checked = 0
    complete = True
    requested = 1
    bindings: dict[str, str] = {}
    keys: list[str] = []

    def key(value: str) -> str:
        """Apply caller-selected interpretation after bounding the expanded text."""
        bound_text(value, operation)
        text = str(value)
        if mode == "literal":
            return text
        if mode == "iri":
            return str(absolute_iri(text))
        prefix, separator, suffix = text.partition(":")
        if not separator or prefix not in bindings:
            raise InputError("Alternate identifier has no supplied CURIE prefix")
        namespace = bindings[prefix]
        if len(namespace) + len(suffix) > operation.limits.max_bytes:
            raise LimitExceeded("Expanded alternate identifier exceeds max_bytes")
        operation.consume("work", len(namespace) + len(suffix))
        operation.consume("bytes", 64 + 12 * (len(namespace) + len(suffix)))
        if (len(namespace.encode("utf-8")) + len(suffix.encode("utf-8"))) > (
            operation.limits.max_bytes
        ):
            raise LimitExceeded("Expanded alternate identifier exceeds max_bytes")
        return str(absolute_iri(namespace + suffix))

    try:
        supplied = capture_items(identifiers, str, operation)
        requested = max(1, len(supplied))
        operation.consume("states", 4 * len(supplied))
        operation.consume("bytes", 128 * len(supplied))
        for value in supplied:
            bound_text(value, operation)
        requests = tuple(str(value) for value in supplied)
        if registry is not None:
            for prefix, namespace in capture_items(registry.bindings, tuple, operation):
                bound_text(prefix, operation)
                bound_text(namespace, operation)
                operation.consume("states")
                bindings[prefix] = namespace
        for request in requests:
            value = key(request)
            operation.consume("states")
            keys.append(value)
            observed.setdefault(value, {})
            candidates.setdefault(value, [])
        selected = view.all_statements(budget=operation)
        retain_report(selected.report, operation, issues)
        children.append(selected.report)
        complete = selected.report.complete
        for edge in selected.items:
            operation.consume("work")
            if edge.predicate != _ALTERNATIVE:
                continue
            for node in (edge.subject, edge.predicate, edge.object):
                bound_node(node, operation)
            bound_text(edge.scope, operation)
            try:
                alias = key(str(edge.object))
            except LimitExceeded:
                raise
            except InputError:
                complete = False
                issues.append(
                    Issue(
                        "obo.alternative_id_unverified",
                        "An asserted alias lacks the selected interpretation",
                        kind="unverified",
                    )
                )
                continue
            if alias not in observed:
                continue
            if (
                not isinstance(edge.subject, URIRef)
                or not isinstance(edge.object, Literal)
                or edge.object.language is not None
                or edge.object.datatype not in (None, XSD.string)
            ):
                complete = False
                issues.append(
                    Issue(
                        "obo.alternative_id_unverified",
                        "An alias requires a named subject and an untagged string",
                        kind="unverified",
                    )
                )
                continue
            operation.consume("states", 2)
            observed[alias].setdefault(edge.subject, []).append(edge)
        for alias, terms in observed.items():
            for iri, assertions in terms.items():
                operation.consume("work")
                term = view.lookup(str(iri))
                state = view.deprecation(str(iri), budget=operation)
                retain_report(state.report, operation, issues)
                children.append(state.report)
                complete &= state.report.complete
                if term.status != "declared":
                    complete = False
                    issues.append(
                        Issue(
                            "obo.alternative_id_unverified",
                            "The canonical subject has no selected declaration",
                            kind="unverified",
                            actual=str(iri),
                        )
                    )
                operation.consume("states", len(assertions) + 2)
                operation.consume("work")
                operation.consume("bytes", 128 + 16 * len(assertions))
                candidates[alias].append(
                    AlternativeIdCandidate(term, tuple(assertions), state)
                )
            if len(terms) > 1:
                issues.append(
                    Issue(
                        "obo.alternative_id_ambiguous",
                        "An alternate identifier names distinct canonical terms",
                        alias,
                    )
                )
        for alias in keys:
            operation.consume("work")
            if not candidates[alias]:
                issues.append(
                    Issue(
                        "obo.alternative_id_unverified",
                        "No canonical alias assertion was established",
                        kind="unverified",
                        actual=alias,
                    )
                )
            checked += 1
        if not requests:
            issues.append(
                Issue(
                    "obo.alternative_id_unverified",
                    "No required alternate identifiers were selected",
                    kind="unverified",
                )
            )
    except LimitExceeded as error:
        complete = False
        issues.exhausted(str(error))
    # Each output slot and tuple pointer was reserved before retaining its
    # candidate. Freeze completed prefixes even when a later allowance expired.
    frozen = {alias: tuple(values) for alias, values in candidates.items()}
    report = Report(
        "obo.alternative_ids",
        checked,
        tuple(issues),
        requested=requested,
        complete=complete,
        children=tuple(children),
        regime="asserted-obo-alternative-identifiers",
    )
    # Output slots were reserved during request capture. Terminal status depends
    # on the entire scan, never on the first observed candidate in a partial map.
    items = []
    for index, request in enumerate(requests):
        matches = frozen.get(keys[index], ()) if index < len(keys) else ()
        status: Choice["unique", "ambiguous", "missing", "incomplete"] = (
            "incomplete"
            if not complete
            else "ambiguous"
            if len(matches) > 1
            else "unique"
            if matches
            else "missing"
        )
        items.append(AlternativeIdResolution(request, matches, status))
    return Selection(tuple(items), report)
