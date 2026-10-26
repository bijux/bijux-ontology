"""Opt-in logical literal traits over exact, graph-local RDF direction encodings."""

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget
from bijux_ontology.jsonld.evidence.values import ValueTraits
from bijux_ontology.jsonld.processing.conversion import (
    DirectionPolicy,
    _convert_original_expansion,
)
from bijux_ontology.jsonld.processing.expansion import OriginalExpansion
from bijux_ontology.rdf.identifiers import language_tag

_I18N = "https://www.w3.org/ns/i18n#"
_LANGUAGE = URIRef(str(RDF) + "language")
_DIRECTION = URIRef(str(RDF) + "direction")


def project_direction(
    graph: Graph, node: Node, policy: DirectionPolicy | None, budget: Budget
) -> tuple[bool, ValueTraits | None]:
    """Return recognized encoding and qualified traits without replacing its term.

    i18n literals retain their physical datatype. Compound literals must be blank,
    referenced once in this graph, and contain only unique plain-string value,
    direction and optional language components. Other RDF contracts and identities
    still concern the original term, not this logical projection.
    """
    budget.consume("work")
    if isinstance(node, Literal) and node.datatype is not None:
        datatype = str(node.datatype)
        if datatype.startswith(_I18N):
            if policy != "i18n-datatype" or node.language is not None:
                return True, None
            suffix = datatype.removeprefix(_I18N)
            language, separator, direction = suffix.partition("_")
            if not separator or direction not in {"ltr", "rtl"}:
                return True, None
            try:
                if language:
                    language_tag(language)
            except InputError:
                return True, None
            return True, ValueTraits(
                "literal",
                datatype=datatype,
                language=language or None,
                direction=direction,
            )
    if not isinstance(node, BNode | URIRef):
        return False, None
    candidate = policy == "compound-literal" and isinstance(node, BNode)
    parts: dict[Node, Literal] = {}
    allowed = {RDF.value, _LANGUAGE, _DIRECTION}
    for _, predicate, value in graph.triples((node, None, None)):
        budget.consume("work")
        if predicate in allowed:
            candidate = True
        elif not candidate:
            continue
        if not candidate:
            continue
        if (
            predicate not in allowed
            or predicate in parts
            or not isinstance(value, Literal)
            or value.language is not None
            or value.datatype not in (None, XSD.string)
        ):
            return True, None
        budget.consume("states")
        parts[predicate] = value
    if not candidate:
        return False, None
    if (
        policy != "compound-literal"
        or not isinstance(node, BNode)
        or RDF.value not in parts
        or _DIRECTION not in parts
    ):
        return True, None
    direction = str(parts[_DIRECTION])
    if direction not in {"ltr", "rtl"}:
        return True, None
    compound_language = str(parts[_LANGUAGE]) if _LANGUAGE in parts else None
    try:
        if compound_language is not None:
            language_tag(compound_language)
    except InputError:
        return True, None
    incoming = 0
    for _ in graph.triples((None, None, node)):
        budget.consume("work")
        incoming += 1
        if incoming > 1:
            return True, None
    if incoming != 1:
        return True, None
    return True, ValueTraits("literal", language=compound_language, direction=direction)


class DirectionCheck:
    """Qualify one output conversion only when a selected directional value exists."""

    def __init__(self, policy: DirectionPolicy | None) -> None:
        self.policy = policy
        self.seen = False

    def observe(self, traits: ValueTraits) -> None:
        """Record required representation work without retaining value payloads."""
        self.seen |= traits.direction is not None

    def check(
        self,
        original: OriginalExpansion,
        *,
        budget: Budget,
    ) -> Report | None:
        """Reuse maintained conversion and the operation's owned resource cache."""
        if not self.seen:
            return None
        if self.policy is None:
            return Report(
                "emission.rdf_direction",
                1,
                (
                    Issue(
                        "context.rdf_direction_loss",
                        "Directional output requires an explicit "
                        "RDF representation policy",
                        kind="unverified",
                    ),
                ),
            )
        return _convert_original_expansion(
            original,
            budget=budget,
            direction=self.policy,
        ).report
