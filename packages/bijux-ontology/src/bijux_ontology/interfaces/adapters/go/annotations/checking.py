"""Explicit GO vocabulary roles and lifecycle choices over selected native views."""

from collections.abc import Mapping
from threading import Event

from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.obo.identifiers import resolve_alternative_ids
from bijux_ontology.vocabulary.registry.types import IdentifierRegistry

from .types import (
    GoAnnotation,
    GoAnnotationInventory,
    GoAnnotationPolicy,
    GoAnnotations,
    GoReference,
    GoRole,
)

_ROLES: tuple[GoRole, ...] = (
    "term",
    "relation",
    "evidence",
    "taxon",
    "extension_term",
    "extension_relation",
)


def _registry(
    registry: IdentifierRegistry,
    relations: Mapping[str, str] | None,
    operation: Operation,
) -> tuple[dict[str, str], dict[str, str]]:
    """Capture bounded plain mappings before any identifier interpretation."""
    if type(registry) is not IdentifierRegistry:
        raise InputError("Supply an explicit IdentifierRegistry")
    prefixes: dict[str, str] = {}
    labels: dict[str, str] = {}
    for key, value in registry.bindings:
        operation.consume("states", 2)
        if type(key) is not str or type(value) is not str:
            raise InputError("Identifier bindings require ordinary strings")
        bound_text(key, operation)
        bound_text(value, operation)
        prefixes[key] = value
    if relations is not None:
        if type(relations) is not dict:
            raise InputError("Relation labels require a finite ordinary dict")
        for key, value in relations.items():
            operation.consume("states", 2)
            if type(key) is not str or type(value) is not str:
                raise InputError("Relation bindings require ordinary strings")
            bound_text(key, operation)
            bound_text(value, operation)
            labels[key] = value
    return prefixes, labels


def _expand(value: str, prefixes: dict[str, str], operation: Operation) -> str:
    """Expand an explicitly identified CURIE after prepaying combined text storage."""
    bound_text(value, operation)
    prefix, separator, local = value.partition(":")
    if not separator or not local or prefix not in prefixes:
        raise ResourceError("Identifier has no supplied explicit CURIE prefix")
    namespace = prefixes[prefix]
    length = len(namespace) + len(local)
    if length > operation.limits.max_bytes:
        raise LimitExceeded("Expanded GO identifier exceeds max_bytes")
    operation.consume("work", length + 1)
    operation.consume("bytes", 12 * length + 64)
    if (
        len(namespace.encode("utf-8")) + len(local.encode("utf-8"))
        > operation.limits.max_bytes
    ):
        raise LimitExceeded("Expanded GO identifier exceeds UTF-8 max_bytes")
    return str(absolute_iri(namespace + local))


def _uses(
    annotation: GoAnnotation, operation: Operation
) -> tuple[tuple[GoRole, str], ...]:
    """Keep vocabulary roles distinct from publication and entity identifiers."""
    values: list[tuple[GoRole, str]] = [
        ("term", annotation.term),
        ("relation", annotation.relation),
    ]
    values.append(("evidence", annotation.evidence.code))
    if annotation.entity_taxon:
        values.append(("taxon", annotation.entity_taxon))
    values.extend(("taxon", taxon) for taxon in annotation.interacting_taxa)
    for conjunction in annotation.extensions:
        for extension in conjunction:
            operation.consume("states", 2)
            values.extend(
                (
                    ("extension_relation", extension.relation),
                    ("extension_term", extension.target),
                )
            )
    operation.consume("states", len(values))
    return tuple(values)


def check_go_annotations(
    annotations: GoAnnotations,
    view: VocabularyView,
    registry: IdentifierRegistry,
    *,
    relations: Mapping[str, str] | None = None,
    policy: GoAnnotationPolicy | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> GoAnnotationInventory:
    """Check only caller-selected roles, retaining exact aliases and lifecycle evidence.

    GAF legacy evidence codes remain codes; no ambient ECO mapping is applied.
    GAF relation labels need an explicit caller mapping whenever that role is
    selected. Absence of a deprecation statement is retained as absence, rather
    than an asserted active state. Missing/retired terms never disappear merely
    because the same subject also has an accepted assertion.
    """
    operation = Operation(limits, budget, timeout, cancel)
    policy = GoAnnotationPolicy() if policy is None else policy
    issues = IssueCollector(operation)
    references: list[GoReference] = []
    declarations: list[TermDeclaration] = []
    children: list[Report] = []
    checked = 0
    requested = 1
    complete = True
    try:
        if type(annotations) is not GoAnnotations or not isinstance(
            view, VocabularyView
        ):
            raise InputError(
                "GO checking requires parsed assertions and a selected VocabularyView"
            )
        if (
            type(policy) is not GoAnnotationPolicy
            or type(policy.roles) is not tuple
            or any(role not in _ROLES for role in policy.roles)
            or not policy.roles
            or len(set(policy.roles)) != len(policy.roles)
            or type(policy.allow_deprecated) is not bool
            or type(policy.alternative_ids) is not bool
        ):
            raise InputError("Supply explicit finite typed GO role/lifecycle choices")
        prefixes, labels = _registry(registry, relations, operation)
        retain_report(annotations.report, operation, issues)
        children.append(annotations.report)
        complete = annotations.report.complete
        if not annotations.report.verified:
            raise ResourceError(
                "Invalid annotation ingestion cannot qualify vocabulary uses"
            )
        requested = 0
        for annotation in annotations.annotations:
            operation.consume("states", 4)
            bound_text(annotation.source, operation)
            suffix = f":line={annotation.line}"
            length = len(annotation.source) + len(suffix)
            if length > operation.limits.max_bytes:
                raise LimitExceeded("GO record location exceeds max_bytes")
            operation.consume("bytes", 12 * length + 64)
            if (
                len(annotation.source.encode("utf-8")) + len(suffix)
                > operation.limits.max_bytes
            ):
                raise LimitExceeded("GO record location exceeds UTF-8 max_bytes")
            location = annotation.source + suffix
            for role, original in _uses(annotation, operation):
                if role not in policy.roles:
                    continue
                requested += 1
                operation.consume("states", 8)
                operation.consume("bytes", 512)
                bound_text(original, operation)
                iri = None
                alternative = None
                state = None
                try:
                    if role == "evidence" and annotation.evidence.kind == "gaf_code":
                        raise ResourceError(
                            "A GAF evidence code has no supplied ECO mapping"
                        )
                    token = (
                        labels.get(original)
                        if role in ("relation", "extension_relation")
                        and annotation.format == "gaf"
                        else original
                    )
                    if token is None:
                        raise ResourceError(
                            "No caller-supplied mapping for this GAF relation label"
                        )
                    iri = _expand(token, prefixes, operation)
                    found = view.lookup(iri)
                    if (
                        found.status != "declared"
                        and role in ("term", "extension_term")
                        and policy.alternative_ids
                    ):
                        resolved = resolve_alternative_ids(
                            view,
                            (original,),
                            mode="curie",
                            registry=registry,
                            budget=operation,
                        )
                        children.append(resolved.report)
                        retain_report(resolved.report, operation, issues)
                        if len(resolved.items) == 1:
                            alternative = resolved.items[0]
                            if alternative.status == "unique":
                                iri = alternative.candidates[0].term.iri
                                found = view.lookup(iri)
                            elif alternative.status in ("ambiguous", "incomplete"):
                                raise ResourceError(
                                    "Alternative identifier is not uniquely qualified"
                                )
                    expected = (
                        (str(OWL.Class), str(RDFS.Class))
                        if role in ("term", "evidence", "taxon")
                        else (
                            str(RDF.Property),
                            str(OWL.ObjectProperty),
                            str(OWL.AnnotationProperty),
                            str(OWL.DatatypeProperty),
                        )
                        if role in ("relation", "extension_relation")
                        else ()
                    )
                    if (
                        found.status != "declared"
                        or expected
                        and not set(expected).intersection(found.kinds)
                    ):
                        issues.append(
                            Issue(
                                "go.reference_missing"
                                if found.status != "declared"
                                else "go.reference_kind_mismatch",
                                "Selected GO reference lacks its required declaration",
                                location,
                                actual=original,
                                expected=", ".join(expected),
                            )
                        )
                    state = view.deprecation(iri, budget=operation)
                    children.append(state.report)
                    retain_report(state.report, operation, issues)
                    if state.state == "deprecated" and not policy.allow_deprecated:
                        issues.append(
                            Issue(
                                "go.term_retired",
                                "Selected GO reference is explicitly deprecated",
                                location,
                                actual=original,
                            )
                        )
                    elif state.state in ("conflicting", "unverified"):
                        raise ResourceError(
                            "Selected reference lifecycle could not be qualified"
                        )
                    declarations.append(TermDeclaration(iri, expected, location))
                    checked += 1
                except LimitExceeded:
                    raise
                except InputError as error:
                    complete = False
                    issues.append(
                        Issue(
                            "go.reference_unverified"
                            if isinstance(error, ResourceError)
                            else "go.input_invalid",
                            str(error),
                            location,
                            kind="unverified"
                            if isinstance(error, ResourceError)
                            else "input_error",
                            actual=original,
                        )
                    )
                references.append(
                    GoReference(annotation, role, original, iri, alternative, state)
                )
        if not requested:
            raise ResourceError("No required annotation references were selected")
    except LimitExceeded as error:
        complete = False
        for child in children:
            issues.retain_completed(child)
        issues.exhausted(str(error))
    except InputError as error:
        complete = False
        try:
            issues.append(
                Issue(
                    "go.reference_unverified"
                    if isinstance(error, ResourceError)
                    else "go.input_invalid",
                    str(error),
                    error.location,
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return GoAnnotationInventory(
        tuple(references),
        tuple(declarations),
        Report(
            "go.annotations.check",
            checked,
            tuple(issues),
            requested=max(1, requested),
            complete=complete,
            children=tuple(children),
            regime="consumer-selected-GO-vocabulary-contract",
        ),
        policy,
    )
