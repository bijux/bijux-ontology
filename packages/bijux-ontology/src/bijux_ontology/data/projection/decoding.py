"""Opt-in narrow Python conversions, preserving every original RDF term."""

from decimal import Decimal

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget, IssueCollector, retain_report
from bijux_ontology.rdf.literals.checking import check_literal, datatype_relation
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, term_key

from .types import FieldState, ProjectedValue, ReadField, ReferenceHandle


def decode_term(
    term: Node,
    field: ReadField,
    *,
    source: PreparedSource,
    graph: URIRef | BNode | None,
    selected: set[tuple[str | None, ...]],
    budget: Budget,
    issues: IssueCollector,
    where: str,
) -> tuple[ProjectedValue, FieldState]:
    """Raw retention needs no datatype interpretation; conversions require proof."""
    bound_node(term, budget)
    if source.literal_fidelity not in ("source_terms", "supplied_terms") and isinstance(
        term, Literal
    ):
        issues.append(
            Issue(
                "projection.term_unverified",
                "Exact literal identity was not retained",
                where,
                kind="unverified",
            )
        )
        return term, "unverified"
    if field.decoder == "raw":
        return term, "raw"
    if field.decoder == "reference":
        if not isinstance(term, URIRef | BNode):
            issues.append(
                Issue(
                    "projection.value_invalid",
                    "A reference requires an RDF resource",
                    where,
                )
            )
            return term, "invalid"
        key = term_key(term)
        budget.consume("work", sum(len(value or "") for value in key) + 1)
        if field.references == "selected" and key not in selected:
            issues.append(
                Issue(
                    "projection.reference_unverified",
                    "Reference has no explicitly selected local record",
                    where,
                    kind="unverified",
                )
            )
            return term, "unverified"
        budget.consume("bytes", 64)
        budget.consume("states")
        return ReferenceHandle(source, graph, term), "decoded"
    if not isinstance(term, Literal):
        issues.append(
            Issue(
                "projection.value_invalid",
                "A literal decoder requires an RDF literal",
                where,
            )
        )
        return term, "invalid"
    datatype = str(term.datatype or (RDF.langString if term.language else XSD.string))
    supported = (
        field.decoder == "string"
        and datatype in (str(XSD.string), str(RDF.langString))
        or field.decoder == "boolean"
        and datatype == str(XSD.boolean)
        or field.decoder == "decimal"
        and datatype == str(XSD.decimal)
        or field.decoder == "integer"
        and datatype_relation(datatype, str(XSD.integer)) in ("equal", "subset")
    )
    if not supported:
        issues.append(
            Issue(
                "projection.conversion_unverified",
                "Datatype is outside the selected decoder profile",
                where,
                kind="unverified",
            )
        )
        return term, "unverified"
    checked = check_literal(term, location=where, budget=budget)
    retain_report(checked, budget, issues)
    if not checked.verified:
        return term, "invalid" if checked.outcome == "disagreement" else "unverified"
    budget.consume("work", len(term) + 1)
    budget.consume("bytes", 128 + 8 * len(term))
    text = str(term)
    if field.decoder == "decimal":
        return Decimal(text), "decoded"
    if field.decoder == "integer":
        return int(text), "decoded"
    if field.decoder == "boolean":
        return text in ("true", "1"), "decoded"
    return text, "decoded"
