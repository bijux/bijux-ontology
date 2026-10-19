"""Selected ontology encoding checks; these are not an OWL consistency proof."""

from collections.abc import Callable, Sequence
from functools import partial

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.owl.checking import (
    check_axiom_annotations,
    check_declarations,
    check_headers,
)
from bijux_ontology.owl.expressions import check_expression_shapes
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.builtins import (
    Regime,
    builtin_kinds,
    kind_matches,
    validate_regime,
)
from bijux_ontology.vocabulary.checking import _lint_issues
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary
from bijux_ontology.workflows.ontology.deprecation import check_deprecated_terms

_CLASS = (str(OWL.Class), str(RDFS.Class))
_PROPERTY = (str(RDF.Property),)
_REFERENCES = {
    RDFS.subClassOf: (_CLASS, _CLASS),
    RDFS.subPropertyOf: (_PROPERTY, _PROPERTY),
    RDFS.domain: (_PROPERTY, _CLASS),
    RDFS.range: (_PROPERTY, (*_CLASS, str(RDFS.Datatype))),
    OWL.equivalentClass: (_CLASS, _CLASS),
    OWL.disjointWith: (_CLASS, _CLASS),
    OWL.equivalentProperty: (_PROPERTY, _PROPERTY),
    OWL.propertyDisjointWith: (_PROPERTY, _PROPERTY),
    OWL.inverseOf: ((str(OWL.ObjectProperty),), (str(OWL.ObjectProperty),)),
    OWL.sameAs: ((), ()),
    OWL.differentFrom: ((), ()),
}


def _location(node: Node, source: str) -> str:
    return (
        f"{source}: {node}" if isinstance(node, URIRef) else f"{source}: anonymous node"
    )


def _reference_checks(
    graph: Graph,
    source: str,
    scopes: Sequence[tuple[str, Graph]],
    regime: Regime,
    budget: Budget,
    issues: IssueCollector,
) -> int:
    def declared(node: URIRef) -> frozenset[str]:
        # Declaration roles and exemptions must use the same sources as the checks.
        return frozenset(
            str(kind)
            for _, selected_graph in scopes
            for kind in selected_graph.objects(node, RDF.type)
            if isinstance(kind, URIRef)
        ) | builtin_kinds(str(node), regime)

    count = 0
    for predicate, roles in _REFERENCES.items():
        for subject, target in sorted(
            graph.subject_objects(predicate),
            key=lambda pair: (pair[0].n3(), pair[1].n3()),
        ):
            count += 1
            budget.consume("work")
            # OWL datatype definitions share equivalentClass with class axioms.
            # In OWL DL both endpoints must retain the same declaration category.
            data_definition = (
                regime == "owl-dl"
                and predicate == OWL.equivalentClass
                and any(
                    str(RDFS.Datatype) in declared(node)
                    if isinstance(node, URIRef)
                    else (node, RDF.type, RDFS.Datatype) in graph
                    for node in (subject, target)
                )
            )
            selected_roles = (
                ((str(RDFS.Datatype),), (str(RDFS.Datatype),))
                if data_definition
                else roles
            )
            for node, expected in zip((subject, target), selected_roles, strict=True):
                location = _location(subject, source)
                if isinstance(node, Literal):
                    issues.append(
                        Issue(
                            "ontology.reference_shape",
                            "Structural reference requires a resource, not a literal",
                            location,
                            actual=str(node),
                        ),
                    )
                elif isinstance(node, BNode):
                    if predicate not in (OWL.sameAs, OWL.differentFrom):
                        issues.append(
                            Issue(
                                "ontology.expression_unverified",
                                "Anonymous expression comparison is unsupported",
                                location,
                                kind="unverified",
                                actual=str(predicate),
                            ),
                        )
                elif isinstance(node, URIRef) and expected:
                    if (
                        regime == "owl-dl"
                        and predicate in (RDFS.domain, RDFS.range)
                        and node == target
                        and isinstance(subject, URIRef)
                        and str(OWL.AnnotationProperty) in declared(subject)
                    ):
                        continue
                    kinds = declared(node)
                    if not kind_matches(kinds, expected, regime):
                        issues.append(
                            Issue(
                                "ontology.reference_missing"
                                if not kinds
                                else "ontology.reference_kind",
                                "Named reference lacks the selected declaration role",
                                location,
                                expected=", ".join(expected),
                                actual=str(node),
                            ),
                        )
    return count


def check_ontology(
    vocabulary: Vocabulary | Graph,
    *,
    regime: Regime = "rdf",
    families: Sequence[str] = (
        "references",
        "declarations",
        "headers",
        "expressions",
        "annotations",
        "reserved",
    ),
    include_imports: bool = True,
    include_lookup: bool = False,
    required_annotations: Sequence[str] = (),
    metadata_terms: Sequence[str] = (),
    languages: Sequence[str] = (),
    used_terms: Sequence[str] = (),
    replacement_predicates: Sequence[str] = (),
    limits: Limits | None = None,
) -> Report:
    """Check selected supplied ontology structures without an application inventory.

    Reference resolution and checking both use the selected sources. Lookup
    sources participate only when explicitly selected. Publication metadata
    requirements are opt-in. A structurally checked expression remains unverified
    when its logical consequences are outside the supported named fragment.
    """
    validate_regime(regime)
    if isinstance(families, (str, bytes)):
        raise InputError("Check families must be a sequence of names")
    if type(include_imports) is not bool or type(include_lookup) is not bool:
        raise InputError("Source scope switches must be booleans")
    selected = tuple(families)
    if not selected or set(selected) - {
        "references",
        "declarations",
        "headers",
        "expressions",
        "annotations",
        "metadata",
        "deprecated",
        "reserved",
    }:
        raise InputError("Select supported, nonempty ontology check families")
    sources = (
        vocabulary
        if isinstance(vocabulary, Vocabulary)
        else load_vocabulary(vocabulary, limits=limits)
    )
    budget = Budget(limits if limits is not None else sources.limits)
    count = 0
    issues = IssueCollector(budget)
    complete = True
    try:
        roles = (
            "local",
            *(("imports",) if include_imports else ()),
            *(("lookup",) if include_lookup else ()),
        )
        scopes = sources._read(roles, budget=budget).scopes
        if "declarations" in selected:
            count += check_declarations(scopes, regime, budget, issues)
        for source, graph in scopes:
            if "reserved" in selected:
                count += _lint_issues(graph, "rdf", budget=budget, issues=issues)
            operations: dict[str, Callable[[], int]] = {
                "references": partial(
                    _reference_checks, graph, source, scopes, regime, budget, issues
                ),
                "expressions": partial(
                    check_expression_shapes, graph, source, budget, issues
                ),
                "headers": partial(check_headers, graph, source, budget, issues),
                "annotations": partial(
                    check_axiom_annotations, graph, source, budget, issues
                ),
            }
            for name, operation in operations.items():
                if name in selected:
                    count += operation()
        for predicate in (
            *required_annotations,
            *replacement_predicates,
            *metadata_terms,
        ):
            absolute_iri(predicate)
        for tag in languages:
            language_tag(tag)
        local = sources._read(("local",), budget=budget).graph
        if "metadata" in selected:
            terms = (
                tuple(dict.fromkeys(URIRef(iri) for iri in metadata_terms))
                if metadata_terms
                else tuple(
                    sorted(
                        {
                            node
                            for node, kind in local.subject_objects(RDF.type)
                            if isinstance(node, URIRef)
                            and kind not in (OWL.Ontology, OWL.Axiom, OWL.Annotation)
                        },
                        key=str,
                    )
                )
            )
            for term in terms:
                budget.consume("work")
                for predicate in required_annotations:
                    count += 1
                    values = tuple(local.objects(term, URIRef(predicate)))
                    if not values or any(
                        not isinstance(value, Literal)
                        or languages
                        and (value.language or "").lower()
                        not in {tag.lower() for tag in languages}
                        for value in values
                    ):
                        issues.append(
                            Issue(
                                "ontology.metadata_required",
                                "Term lacks the requested literal annotation/language",
                                _location(term, "local"),
                                expected=predicate,
                            ),
                        )
        if "deprecated" in selected:
            view = inspect_vocabulary(
                sources,
                scopes=(
                    "local",
                    *(("imports",) if include_imports else ()),
                    *(("lookup",) if include_lookup else ()),
                ),
                limits=budget.limits,
                budget=budget,
            )
            count += check_deprecated_terms(
                view, used_terms, replacement_predicates, regime, budget, issues
            )
    except LimitExceeded as error:
        issues.exhausted(str(error), location="ontology")
        complete = False
    return Report(
        "ontology",
        count,
        tuple(issues),
        regime=regime,
        families=selected,
        complete=complete,
        requested=count if complete else count + 1,
    )
