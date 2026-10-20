"""Actual accession contracts and strictly local resolution of curated xrefs."""

from collections.abc import Sequence
from typing import Literal

from rdflib import Literal as RdfLiteral
from rdflib import URIRef
from rdflib.namespace import XSD

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
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.vocabulary.accessions.checking import check_accessions
from bijux_ontology.vocabulary.accessions.types import AccessionRule, AccessionValue
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Edge
from bijux_ontology.vocabulary.obo.types import OboRecords
from bijux_ontology.vocabulary.registry.types import IdentifierRegistry

from .reading import UnsupportedScope, qualify_edge, qualify_view, read_references
from .types import (
    CrossReferenceAssertion,
    CrossReferenceCheck,
    CrossReferenceObservation,
    LocalCatalogue,
)


def check_cross_references(
    view: VocabularyView,
    terms: Sequence[URIRef],
    registry: IdentifierRegistry,
    *,
    rules: Sequence[AccessionRule],
    catalogues: Sequence[LocalCatalogue] = (),
    literal_mode: Literal["curie", "iri"] = "curie",
    unknown_namespaces: Literal["unverified", "exclude"] = "unverified",
    require_resolution: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> CrossReferenceCheck:
    """Check native dbxref assertions without fetching or inventing equivalence.

    Literals use the caller's explicit CURIE/IRI interpretation. URIRef values
    remain exact IRIs. Only untagged string literals are supported identifier text.
    Definition and synonym references require their original native OBO attachment;
    ambiguous/malformed qualifiers never acquire an arbitrary record association.

    Local closed absence is relative to a supplied catalogue's namespace and
    chosen membership contract. Open absence is unknown. Valid references without
    a supplied catalogue remain external, never nonexistent; existence is required
    only when require_resolution=True. No accession normalization suggestion alters
    the original reference or makes different IRI representations equivalent.
    """
    if not isinstance(view, VocabularyView) or not isinstance(
        registry, IdentifierRegistry
    ):
        raise InputError(
            "Cross references require a vocabulary view and IdentifierRegistry"
        )
    if literal_mode not in ("curie", "iri") or type(require_resolution) is not bool:
        raise InputError("Select literal interpretation and explicit resolution policy")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Expected explicit Limits")
    if unknown_namespaces not in ("unverified", "exclude"):
        raise InputError("Select an explicit unknown namespace policy")
    if budget is not None and limits is not None and limits != budget.limits:
        raise InputError("Cross-reference limits conflict with the shared Budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    children: list[Report] = []
    references: list[CrossReferenceAssertion] = []
    observations: list[CrossReferenceObservation] = []
    accession_check = None
    obo_records: list[OboRecords] = []
    checked = 0
    requested = 1
    finished = False

    def diagnostic(
        reference: CrossReferenceAssertion, message: str, *, unknown: bool
    ) -> None:
        """Locate the required xref finding at its original owner and source."""
        issues.append(
            Issue(
                "obo.xref_unverified" if unknown else "obo.xref_invalid",
                message,
                str(reference.owner),
                kind="unverified" if unknown else "finding",
                details=Location(
                    source=reference.assertion.scope, term=str(reference.owner)
                ),
            )
        )

    try:
        owners = capture_items(terms, URIRef, operation)
        seen: set[URIRef] = set()
        for owner in owners:
            bound_node(owner, operation)
            if owner in seen:
                raise InputError("Cross-reference owners must be unique")
            operation.consume("states")
            seen.add(owner)
        supplied = capture_items(catalogues, LocalCatalogue, operation)
        local: dict[str, LocalCatalogue] = {}
        for supplied_catalogue in supplied:
            bound_text(supplied_catalogue.namespace, operation)
            absolute_iri(supplied_catalogue.namespace)
            bound_text(supplied_catalogue.membership, operation)
            if supplied_catalogue.namespace in local:
                raise InputError(
                    "Local supplied_catalogues require unique exact namespaces"
                )
            qualify_view(supplied_catalogue.view, operation)
            operation.consume("states")
            local[supplied_catalogue.namespace] = supplied_catalogue
        qualify_view(view, operation)
        read_references(
            view, owners, operation, issues, children, references, obo_records
        )
        requested = max(1, len(references))
        values: list[AccessionValue] = []
        offsets: dict[int, int] = {}
        for index, reference in enumerate(references):
            edge = reference.assertion
            value = edge.object
            if (
                reference.attachment is not None
                and reference.attachment.status != "attached"
            ):
                diagnostic(
                    reference,
                    "Xref has no established definition or synonym attachment",
                    unknown=True,
                )
                continue
            if isinstance(value, URIRef):
                mode: Literal["curie", "iri"] = "iri"
            elif (
                isinstance(value, RdfLiteral)
                and value.language is None
                and value.datatype in (None, XSD.string)
            ):
                mode = literal_mode
            else:
                diagnostic(
                    reference,
                    "dbxref requires a named IRI or untagged identifier string",
                    unknown=False,
                )
                continue
            operation.consume("states", 2)
            operation.consume("bytes", 96)
            offsets[index] = len(values)
            values.append(
                AccessionValue(
                    str(value),
                    mode,
                    Location(source=edge.scope, term=str(reference.owner)),
                )
            )
        if values:
            accession_check = check_accessions(
                values,
                registry,
                rules=rules,
                unknown_namespaces=unknown_namespaces,
                budget=operation,
            )
            retain_report(accession_check.report, operation, issues)
            children.append(accession_check.report)
        for index, reference in enumerate(references):
            operation.consume("work")
            operation.consume("states")
            operation.consume("bytes", 128)
            offset = offsets.get(index)
            accession = (
                None
                if offset is None
                or accession_check is None
                or offset >= len(accession_check.observations)
                else accession_check.observations[offset]
            )
            status: Literal[
                "resolved", "dangling", "external", "unknown", "invalid", "excluded"
            ] = "unknown"
            iri = None
            catalogue = None
            term = None
            assertions: tuple[Edge, ...] = ()
            if offset is None:
                status = (
                    "unknown"
                    if reference.attachment is not None
                    and reference.attachment.status != "attached"
                    else "invalid"
                )
            elif accession is not None:
                if accession.status == "invalid":
                    status = "invalid"
                elif accession.status == "excluded":
                    status = "excluded"
                elif accession.status == "valid":
                    original = values[offset]
                    bound_text(original.value, operation)
                    if original.mode == "curie":
                        # Native recognition established one namespace/local record.
                        assert (
                            accession.namespace is not None
                            and accession.local is not None
                        )
                        length = len(accession.namespace) + len(accession.local)
                        if length > operation.limits.max_bytes:
                            raise LimitExceeded("Expanded xref exceeds max_bytes")
                        operation.consume("work", length + 1)
                        operation.consume("bytes", 4 * length)
                        iri = URIRef(accession.namespace + accession.local)
                    else:
                        iri = URIRef(original.value)
                    bound_node(iri, operation)
                    # Catalogue scope covers exact IRIs, independently of which
                    # registry namespace recognized an accession representation.
                    for namespace, candidate_catalogue in local.items():
                        operation.consume("work", len(namespace) + 1)
                        if iri.startswith(namespace) and (
                            catalogue is None
                            or len(namespace) > len(catalogue.namespace)
                        ):
                            catalogue = candidate_catalogue
                    if catalogue is None:
                        status = "external"
                        if require_resolution:
                            diagnostic(
                                reference,
                                "No local catalogue was supplied "
                                "for required resolution",
                                unknown=True,
                            )
                    else:
                        term = catalogue.view.lookup(str(iri))
                        for text in (term.iri, *term.kinds, *term.scopes):
                            bound_text(text, operation)
                        found = term.status == "declared"
                        complete_lookup = True
                        if catalogue.membership == "subject":
                            selected = catalogue.view.relationships(
                                str(iri), budget=operation
                            )
                            retain_report(selected.report, operation, issues)
                            children.append(selected.report)
                            for edge in selected.items:
                                qualify_edge(edge, operation)
                            assertions = selected.items
                            found = bool(assertions)
                            complete_lookup = selected.report.complete
                        if found:
                            status = "resolved"
                        else:
                            # Prefix selection already established that this exact
                            # IRI belongs to the chosen catalogue's namespace.
                            closed = catalogue.closed and complete_lookup
                            status = "dangling" if closed else "unknown"
                            diagnostic(
                                reference,
                                (
                                    "Referenced identifier is absent from the "
                                    "closed selected local catalogue"
                                )
                                if closed
                                else (
                                    "Selected local catalogue does not establish "
                                    "reference existence or absence"
                                ),
                                unknown=not closed,
                            )
            observations.append(
                CrossReferenceObservation(
                    reference, accession, iri, status, catalogue, term, assertions
                )
            )
            checked += 1
        if not references:
            issues.append(
                Issue(
                    "obo.xref_unverified",
                    "No required xref occurrence was captured",
                    kind="unverified",
                )
            )
        finished = bool(owners) and checked == requested
    except LimitExceeded as error:
        if accession_check is not None:
            issues.retain_completed(accession_check.report)
        issues.exhausted(str(error), location="obo.cross_references")
    except UnsupportedScope as error:
        try:
            issues.append(Issue("obo.xref_unverified", str(error), kind="unverified"))
        except LimitExceeded:
            issues.exhausted("Stopped retaining unsupported cross-reference scope")
    except InputError:
        try:
            issues.append(
                Issue(
                    "obo.xref_input_error",
                    "Cross-reference policy or selected source scope is invalid",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining cross-reference input failure")
    return CrossReferenceCheck(
        tuple(references),
        tuple(observations),
        accession_check,
        Report(
            "obo.cross_references",
            checked,
            tuple(issues),
            requested=requested,
            complete=finished,
            children=tuple(children),
            regime="explicit-dbref-accession-and-local-catalogue",
        ),
        tuple(obo_records),
    )
