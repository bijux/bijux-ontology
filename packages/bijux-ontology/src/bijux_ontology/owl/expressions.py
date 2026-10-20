"""Finite RDF encodings of OWL expressions, separate from logical reasoning."""

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.rdf.structure import read_rdf_list

from .constructs import unsupported_constructs

_LIST_EXPRESSIONS = frozenset(
    (
        OWL.unionOf,
        OWL.intersectionOf,
        OWL.oneOf,
        OWL.disjointUnionOf,
        OWL.members,
        OWL.distinctMembers,
        OWL.withRestrictions,
        OWL.propertyChainAxiom,
        OWL.hasKey,
    )
)
_CARDINALITIES = frozenset(
    (
        OWL.cardinality,
        OWL.minCardinality,
        OWL.maxCardinality,
        OWL.qualifiedCardinality,
        OWL.minQualifiedCardinality,
        OWL.maxQualifiedCardinality,
    )
)
_RESTRICTION_VALUES = _CARDINALITIES | {
    OWL.someValuesFrom,
    OWL.allValuesFrom,
    OWL.hasValue,
    OWL.hasSelf,
}


def _location(node: Node, source: str) -> str:
    return (
        f"{source}: {node}" if isinstance(node, URIRef) else f"{source}: anonymous node"
    )


def _nonnegative_integer(value: Node) -> bool:
    """Check XML Schema integer lexical space without unbounded integer parsing."""
    if not isinstance(value, Literal) or value.datatype != XSD.nonNegativeInteger:
        return False
    text = str(value).strip(" \t\r\n")
    digits = text[1:] if text.startswith(("+", "-")) else text
    return (
        bool(digits)
        and all("0" <= char <= "9" for char in digits)
        and (not text.startswith("-") or not digits.strip("0"))
    )


def check_expression_shapes(
    graph: Graph, source: str, budget: Budget, issues: IssueCollector
) -> int:
    """Retain malformed encoding findings and explicitly unsupported semantics."""
    count = 0
    group_types = (OWL.AllDisjointClasses, OWL.AllDisjointProperties, OWL.AllDifferent)
    for node in sorted(set(graph.subjects()), key=lambda value: value.n3()):
        budget.consume("work")
        predicates = unsupported_constructs(graph, node)
        types = set(graph.objects(node, RDF.type))
        groups = types.intersection(group_types)
        restriction = OWL.Restriction in types or bool(
            tuple(graph.objects(node, OWL.onProperty))
        )
        if not predicates and not groups and not restriction:
            continue
        count += 1
        location = _location(node, source)
        malformed = False
        for predicate in sorted(_LIST_EXPRESSIONS, key=str):
            heads = tuple(
                sorted(graph.objects(node, predicate), key=lambda value: value.n3())
            )
            if len(heads) > 1:
                issues.append(
                    Issue(
                        "ontology.expression_component",
                        "Expression requires one list for each constructor",
                        location,
                        actual=str(predicate),
                    ),
                )
                malformed = True
            for head in heads:
                try:
                    members = read_rdf_list(
                        graph, head, max_items=budget.limits.max_work, budget=budget
                    )
                    budget.consume("work", len(members))
                except LimitExceeded:
                    raise
                except InputError as error:
                    issues.append(
                        Issue(
                            "ontology.expression_list",
                            str(error),
                            location,
                            actual=str(predicate),
                        ),
                    )
                    malformed = True
                    continue
                minimum = (
                    2
                    if predicate
                    in (
                        OWL.unionOf,
                        OWL.intersectionOf,
                        OWL.disjointUnionOf,
                        OWL.propertyChainAxiom,
                        OWL.members,
                        OWL.distinctMembers,
                    )
                    else 1
                )
                if len(members) < minimum:
                    issues.append(
                        Issue(
                            "ontology.expression_arity",
                            "Expression list has too few operands",
                            location,
                            expected=str(minimum),
                            actual=str(len(members)),
                        ),
                    )
                    malformed = True
                if predicate not in (OWL.oneOf, OWL.withRestrictions) and any(
                    not isinstance(member, URIRef | BNode) for member in members
                ):
                    issues.append(
                        Issue(
                            "ontology.expression_operand",
                            "Expression list requires resource operands",
                            location,
                            actual=str(predicate),
                        ),
                    )
                    malformed = True
                if (
                    predicate == OWL.oneOf
                    and RDFS.Datatype not in types
                    and any(isinstance(member, Literal) for member in members)
                ):
                    issues.append(
                        Issue(
                            "ontology.expression_operand",
                            "Object enumeration requires resource operands",
                            location,
                            actual=str(predicate),
                        ),
                    )
                    malformed = True
                if (
                    predicate == OWL.oneOf
                    and RDFS.Datatype in types
                    and any(not isinstance(member, Literal) for member in members)
                ):
                    issues.append(
                        Issue(
                            "ontology.expression_operand",
                            "Data enumeration requires literal operands",
                            location,
                            actual=str(predicate),
                        ),
                    )
                    malformed = True
                if predicate == OWL.withRestrictions and any(
                    not isinstance(member, BNode)
                    or len(tuple(graph.predicate_objects(member))) != 1
                    or not isinstance(next(graph.objects(member), None), Literal)
                    for member in members
                ):
                    issues.append(
                        Issue(
                            "ontology.facet_shape",
                            "Each facet node requires one literal-valued restriction",
                            location,
                        ),
                    )
                    malformed = True
        for group in sorted(groups, key=str):
            allowed = (
                (OWL.members, OWL.distinctMembers)
                if group == OWL.AllDifferent
                else (OWL.members,)
            )
            if (
                sum(len(tuple(graph.objects(node, predicate))) for predicate in allowed)
                != 1
            ):
                issues.append(
                    Issue(
                        "ontology.group_shape",
                        "Disjoint/different group needs exactly one members list",
                        location,
                    ),
                )
                malformed = True
        for predicate in (
            OWL.complementOf,
            OWL.datatypeComplementOf,
            OWL.onDatatype,
            OWL.inverseOf,
        ):
            values = tuple(graph.objects(node, predicate))
            if values and (
                len(values) != 1 or not isinstance(values[0], URIRef | BNode)
            ):
                issues.append(
                    Issue(
                        "ontology.expression_operand",
                        "Unary expression requires one resource operand",
                        location,
                        actual=str(predicate),
                    ),
                )
                malformed = True
        if (
            tuple(graph.objects(node, OWL.withRestrictions))
            and len(tuple(graph.objects(node, OWL.onDatatype))) != 1
        ):
            issues.append(
                Issue(
                    "ontology.datatype_restriction_shape",
                    "Datatype facets need exactly one base datatype",
                    location,
                ),
            )
            malformed = True
        if restriction:
            properties = tuple(graph.objects(node, OWL.onProperty))
            constraints = [
                (predicate, value)
                for predicate in sorted(_RESTRICTION_VALUES, key=str)
                for value in sorted(
                    graph.objects(node, predicate), key=lambda item: item.n3()
                )
            ]
            if (
                len(properties) != 1
                or not isinstance(properties[0], URIRef | BNode)
                or len(constraints) != 1
            ):
                issues.append(
                    Issue(
                        "ontology.restriction_shape",
                        "Restriction needs one property and one constraint",
                        location,
                    ),
                )
                malformed = True
            for predicate, value in constraints:
                if predicate in _CARDINALITIES and not _nonnegative_integer(value):
                    issues.append(
                        Issue(
                            "ontology.cardinality_shape",
                            "Cardinality requires xsd:nonNegativeInteger",
                            location,
                            actual=str(value),
                        ),
                    )
                    malformed = True
                if predicate in (
                    OWL.someValuesFrom,
                    OWL.allValuesFrom,
                ) and not isinstance(value, URIRef | BNode):
                    issues.append(
                        Issue(
                            "ontology.restriction_filler",
                            "Quantified restriction requires a resource filler",
                            location,
                        ),
                    )
                    malformed = True
                if predicate == OWL.hasSelf and not (
                    isinstance(value, Literal)
                    and value.datatype == XSD.boolean
                    and str(value) in ("true", "1")
                ):
                    issues.append(
                        Issue(
                            "ontology.self_restriction_shape",
                            "Self restriction requires the boolean true value",
                            location,
                        ),
                    )
                    malformed = True
                if predicate in (
                    OWL.qualifiedCardinality,
                    OWL.minQualifiedCardinality,
                    OWL.maxQualifiedCardinality,
                ):
                    qualifiers = tuple(graph.objects(node, OWL.onClass)) + tuple(
                        graph.objects(node, OWL.onDataRange)
                    )
                    if len(qualifiers) != 1 or not isinstance(
                        qualifiers[0], URIRef | BNode
                    ):
                        issues.append(
                            Issue(
                                "ontology.qualified_cardinality_shape",
                                "Qualified cardinality needs one qualifier",
                                location,
                            ),
                        )
                        malformed = True
        if not malformed:
            issues.append(
                Issue(
                    "ontology.expression_unverified",
                    "Encoding checked; logical consequences remain unevaluated",
                    location,
                    kind="unverified",
                    actual=", ".join(predicates)
                    or ", ".join(sorted(map(str, groups)))
                    or str(OWL.Restriction),
                ),
            )
    return count
