"""Exact membership, selected coverage and vocabulary reference diagnostics."""

from collections.abc import Sequence

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri

from ..errors import InputError, LimitExceeded
from ..reports import Issue, Report
from .builtins import (
    Regime,
    builtin_kinds,
    kind_matches,
    reserved_name_known,
    validate_regime,
)
from .loading import load_vocabulary
from .view import Vocabulary

_CLASS_KINDS = frozenset((OWL.Class, RDFS.Class))
_PROPERTY_KINDS = frozenset(
    (RDF.Property, OWL.ObjectProperty, OWL.DatatypeProperty, OWL.AnnotationProperty)
)
_DECLARATION_KINDS = _CLASS_KINDS | _PROPERTY_KINDS | {RDFS.Datatype}
_REFERENCE_RULES = (
    (RDFS.subClassOf, _CLASS_KINDS),
    (RDFS.subPropertyOf, _PROPERTY_KINDS),
    (RDFS.domain, _CLASS_KINDS),
    (RDFS.range, _CLASS_KINDS | {RDFS.Datatype}),
)


def declared_kinds(
    vocabulary: Vocabulary | Graph,
    iri: str,
    *,
    scope: str = "all",
    regime: Regime = "rdf",
) -> frozenset[str]:
    """Read exact declared RDF types and built-in roles from one selected scope.

    ``local`` excludes imports/lookup, ``closure`` adds required imports, and
    ``all`` also permits descriptive lookup declarations. Actual custom RDF types
    are retained: concept instances are not relabelled as ontology classes.
    """
    absolute_iri(iri)
    validate_regime(regime)
    if scope not in ("local", "closure", "all"):
        raise InputError("Unknown vocabulary declaration scope")
    source = (
        vocabulary
        if isinstance(vocabulary, Vocabulary)
        else load_vocabulary(vocabulary)
    )
    if scope == "all":
        kinds = source._kind_index.get(iri, frozenset())
    elif scope == "local":
        kinds = source._local_kind_index.get(iri, frozenset())
    else:
        kinds = source._closure_kind_index.get(iri, frozenset())
    return kinds if scope == "local" else kinds | builtin_kinds(iri, regime)


def _kinds(graph: Graph, iri: str, regime: Regime = "rdf") -> frozenset[str]:
    return frozenset(
        str(kind)
        for kind in graph.objects(URIRef(iri), RDF.type)
        if isinstance(kind, URIRef)
    ) | builtin_kinds(iri, regime)


def _reference_issues(
    local: Graph,
    combined: Graph,
    regime: Regime,
    *,
    budget: Budget,
    issues: IssueCollector,
) -> int:
    checked = 0
    for predicate, allowed in _REFERENCE_RULES:
        for subject, target in sorted(
            local.subject_objects(predicate), key=lambda pair: tuple(map(str, pair))
        ):
            budget.consume("work")
            if not isinstance(target, URIRef):
                continue
            checked += 1
            kinds = _kinds(combined, str(target), regime)
            location = (
                str(subject) if isinstance(subject, URIRef) else "anonymous expression"
            )
            if not kind_matches(kinds, tuple(map(str, allowed)), regime):
                issues.append(
                    Issue(
                        "vocabulary.reference_missing"
                        if not kinds
                        else "vocabulary.reference_kind_mismatch",
                        "Named reference lacks the required declaration kind",
                        location,
                        expected=", ".join(sorted(map(str, allowed))),
                        actual=str(target),
                    )
                )
    return checked


def _lint_issues(
    local: Graph, regime: Regime, *, budget: Budget, issues: IssueCollector
) -> int:
    checked = 0
    named = {node for triple in local for node in triple if isinstance(node, URIRef)}
    named.update(
        node.datatype
        for node in local.objects()
        if isinstance(node, Literal) and node.datatype is not None
    )
    for term in sorted(named, key=str):
        budget.consume("work")
        known = reserved_name_known(str(term))
        if known is not None:
            checked += 1
            if not known:
                issues.append(
                    Issue(
                        "vocabulary.unknown_reserved_term",
                        "Unrecognised term in a reserved RDF vocabulary",
                        str(term),
                        actual=str(term),
                    )
                )
    if regime != "owl-dl":
        return checked
    for subject in sorted(set(local.subjects(RDF.type, OWL.DatatypeProperty)), key=str):
        budget.consume("work")
        checked += 1
        if (subject, RDF.type, OWL.ObjectProperty) in local or (
            subject,
            RDF.type,
            OWL.InverseFunctionalProperty,
        ) in local:
            issues.append(
                Issue(
                    "vocabulary.declaration_conflict",
                    "Datatype property also has an object-property characteristic",
                    str(subject)
                    if isinstance(subject, URIRef)
                    else "anonymous property",
                )
            )
    return checked


def check_vocabulary(
    vocabulary: Vocabulary | Graph,
    terms: Sequence[TermDeclaration],
    *,
    local_namespaces: Sequence[str] = (),
    exclude: Sequence[str] = (),
    require_coverage: bool = False,
    coverage_terms: Sequence[str] = (),
    coverage_kinds: Sequence[str] = (),
    coverage_scope: str = "local",
    regime: Regime = "rdf",
    check_references: bool = False,
    lint: bool = False,
    allow_empty: bool = False,
    limits: Limits | None = None,
) -> Report:
    """Check used terms and optional complete coverage of selected local terms.

    Membership means an explicit RDF type or a supported built-in declaration. Selected
    local namespaces are checked only against the local graph: external lookup
    documents cannot fill a local declaration gap. Exact IRIs are preserved.
    Reference/lint rules are opt-in; schema.org includes metadata is descriptive.
    """
    validate_regime(regime)
    sources = (
        vocabulary
        if isinstance(vocabulary, Vocabulary)
        else load_vocabulary(vocabulary, limits=limits)
    )
    selected = limits if limits is not None else sources.limits
    budget = Budget(selected)
    if coverage_scope not in ("local", "closure"):
        raise InputError("Coverage scope must be local or closure")
    for iri in (*local_namespaces, *exclude, *coverage_terms, *coverage_kinds):
        absolute_iri(iri)
    combined = sources._combined
    excluded = frozenset(exclude)
    issues = IssueCollector(budget)
    checked = 0
    complete = True
    try:
        inventory: set[str] = set()
        for term in terms:
            budget.consume("work")
            if not isinstance(term, TermDeclaration):
                raise InputError("Vocabulary inventory requires TermDeclaration values")
            absolute_iri(term.iri, location=term.location)
            for kind in term.kinds:
                absolute_iri(kind, location=term.location)
            if term.iri in excluded:
                continue
            checked += 1
            inventory.add(term.iri)
            scope = "local" if term.iri.startswith(tuple(local_namespaces)) else "all"
            kinds = declared_kinds(sources, term.iri, scope=scope, regime=regime)
            if not kinds:
                issues.append(
                    Issue(
                        "vocabulary.term_missing",
                        "Used term has no explicit declaration",
                        term.location or term.iri,
                        actual=term.iri,
                    )
                )
            elif not kind_matches(kinds, term.kinds, regime):
                issues.append(
                    Issue(
                        "vocabulary.entity_kind_mismatch",
                        "Term declaration has a different supported kind",
                        term.location or term.iri,
                        expected=", ".join(sorted(term.kinds)),
                        actual=", ".join(sorted(kinds)),
                    )
                )
        if require_coverage:
            if not local_namespaces and not coverage_terms and not coverage_kinds:
                issues.append(
                    Issue(
                        "input.coverage_scope_missing",
                        "Coverage requires selected namespaces, terms or kinds",
                        kind="input_error",
                    )
                )
            else:
                index = (
                    sources._local_kind_index
                    if coverage_scope == "local"
                    else sources._closure_kind_index
                )
                declared: set[str] = set()
                for iri, kinds in index.items():
                    budget.consume("work")
                    if (
                        iri not in excluded
                        and (
                            not local_namespaces
                            or iri.startswith(tuple(local_namespaces))
                        )
                        and (not coverage_terms or iri in coverage_terms)
                        and (
                            not coverage_kinds
                            or kind_matches(kinds, tuple(coverage_kinds), regime)
                        )
                    ):
                        declared.add(iri)
                for iri in sorted(set(coverage_terms) - set(index) - excluded):
                    budget.consume("work")
                    checked += 1
                    issues.append(
                        Issue(
                            "vocabulary.coverage_source_missing",
                            "Term has no declaration in the selected coverage scope",
                            iri,
                            actual=iri,
                        )
                    )
                if not declared and not allow_empty:
                    issues.append(
                        Issue(
                            "input.empty_coverage",
                            "No declarations match the required coverage scope",
                            kind="input_error",
                        )
                    )
                for iri in sorted(declared):
                    budget.consume("work")
                    checked += 1
                    if iri not in inventory:
                        issues.append(
                            Issue(
                                "vocabulary.coverage_gap",
                                "Selected declaration is absent from the inventory",
                                iri,
                                actual=iri,
                            )
                        )
        if check_references:
            checked += _reference_issues(
                sources._local, combined, regime, budget=budget, issues=issues
            )
        if lint:
            checked += _lint_issues(
                sources._local, regime, budget=budget, issues=issues
            )
    except LimitExceeded as error:
        issues.exhausted(str(error), location="vocabulary")
        complete = False
    return Report(
        "vocabulary",
        checked,
        tuple(issues),
        allow_empty=allow_empty,
        regime=regime,
        families=("vocabulary",),
        exclusions=tuple(sorted(excluded)),
        complete=complete,
        requested=checked if complete else checked + 1,
    )
