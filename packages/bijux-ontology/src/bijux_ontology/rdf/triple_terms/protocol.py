"""Bounded wire inventories of maintained terms, without a second RDF model."""

from typing import TypeAlias

import pyoxigraph as ox

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.terms import bound_text

Term: TypeAlias = ox.NamedNode | ox.BlankNode | ox.Literal | ox.Triple


def encode_term(term: Term, budget: Budget, depth: int = 0) -> list[object]:
    """Qualify each actual term before retaining its finite transport record."""
    budget.consume("work")
    if depth > budget.limits.max_depth:
        raise LimitExceeded("Triple-term nesting exceeds max_depth")
    budget.consume("states")
    budget.consume("bytes", 64)
    if isinstance(term, ox.NamedNode | ox.BlankNode):
        bound_text(term.value, budget)
        return ["iri" if isinstance(term, ox.NamedNode) else "blank", term.value]
    if isinstance(term, ox.Literal):
        if term.direction is not None:
            raise InputError(
                "Directional literals are outside this triple-term profile"
            )
        for value in (term.value, term.datatype.value, term.language or ""):
            bound_text(value, budget)
        return ["literal", term.value, term.datatype.value, term.language]
    if isinstance(term, ox.Triple):
        return [
            "triple",
            encode_term(term.subject, budget, depth + 1),
            encode_term(term.predicate, budget, depth + 1),
            encode_term(term.object, budget, depth + 1),
        ]
    raise InputError("Unsupported maintained RDF term")


def decode_term(
    value: object, blanks: dict[str, ox.BlankNode], budget: Budget, depth: int = 0
) -> Term:
    """Reconstruct actual immutable terms with one shared peer blank inventory."""
    budget.consume("work")
    if depth > budget.limits.max_depth:
        raise LimitExceeded("Triple-term nesting exceeds max_depth")
    if not isinstance(value, list) or not value or not isinstance(value[0], str):
        raise InputError("Invalid triple-term transport record")
    budget.consume("states")
    budget.consume("bytes", 64)
    kind = value[0]
    if kind in ("iri", "blank") and len(value) == 2 and isinstance(value[1], str):
        text = value[1]
        bound_text(text, budget)
        if kind == "iri":
            return ox.NamedNode(text)
        if text not in blanks:
            budget.consume("states")
            blanks[text] = ox.BlankNode(text)
        return blanks[text]
    if kind == "literal" and len(value) == 4:
        lexical, datatype, language = value[1:]
        if (
            not isinstance(lexical, str)
            or not isinstance(datatype, str)
            or language is not None
            and not isinstance(language, str)
        ):
            raise InputError("Invalid literal transport record")
        for text in (lexical, datatype, language or ""):
            bound_text(text, budget)
        literal = (
            ox.Literal(lexical, language=language)
            if language is not None
            else ox.Literal(lexical, datatype=ox.NamedNode(datatype))
        )
        if literal.datatype.value != datatype:
            raise InputError("Literal transport changed datatype identity")
        return literal
    if kind == "triple" and len(value) == 4:
        subject, predicate, obj = (
            decode_term(item, blanks, budget, depth + 1) for item in value[1:]
        )
        if not isinstance(subject, ox.NamedNode | ox.BlankNode) or not isinstance(
            predicate, ox.NamedNode
        ):
            raise InputError("Invalid RDF 1.2 triple-term positions")
        return ox.Triple(subject, predicate, obj)
    raise InputError("Unsupported triple-term transport record")


def encode_quads(quads: tuple[ox.Quad, ...], budget: Budget) -> list[object]:
    """Retain only ordinary default-graph assertions containing triple terms."""
    result: list[object] = []
    for quad in quads:
        budget.consume("triples")
        budget.consume("states")
        budget.consume("bytes", 32)
        if not isinstance(quad.graph_name, ox.DefaultGraph):
            raise InputError("Triple-term profile requires one default graph")
        result.append(
            [
                encode_term(quad.subject, budget),
                encode_term(quad.predicate, budget),
                encode_term(quad.object, budget),
            ]
        )
    return result


def decode_quads(value: object, budget: Budget) -> tuple[ox.Quad, ...]:
    """Own one exact graph capture; no independently parsed roles are joined."""
    if not isinstance(value, list):
        raise InputError("Triple-term graph must be a finite record inventory")
    blanks: dict[str, ox.BlankNode] = {}
    rows = []
    for row in value:
        budget.consume("triples")
        budget.consume("states")
        budget.consume("bytes", 32)
        if not isinstance(row, list) or len(row) != 3:
            raise InputError("Invalid triple-term statement record")
        s, p, o = (decode_term(item, blanks, budget) for item in row)
        if not isinstance(s, ox.NamedNode | ox.BlankNode) or not isinstance(
            p, ox.NamedNode
        ):
            raise InputError("Invalid asserted RDF positions")
        rows.append(ox.Quad(s, p, o))
    budget.consume("states", len(rows))
    budget.consume("bytes", 32 + 8 * len(rows))
    return tuple(rows)


def occurrences(quads: tuple[ox.Quad, ...], budget: Budget) -> tuple[ox.Triple, ...]:
    """Read bounded nested occurrences without asserting their descriptions."""
    result = []
    for quad in quads:
        budget.consume("work")
        pending: list[tuple[Term, int]] = [(quad.object, 0)]
        while pending:
            term, depth = pending.pop()
            budget.consume("work")
            if depth > budget.limits.max_depth:
                raise LimitExceeded("Triple-term nesting exceeds max_depth")
            if isinstance(term, ox.Triple):
                budget.consume("states", 2)
                budget.consume("bytes", 64)
                result.append(term)
                pending.append((term.object, depth + 1))
    return tuple(result)
