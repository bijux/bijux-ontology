"""Bounded codecs for exact portable RDF terms and snapshot inventories."""

from typing import cast

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits, bound_text

from .types import (
    PatchBlank,
    PatchLiteral,
    PatchQuad,
    PatchResource,
    PatchValue,
    SnapshotKind,
    SnapshotWitness,
    bound_term,
    validate_witness,
)


def _budget(limits: Limits | None, budget: Budget | None) -> Budget:
    """Keep codec work in the caller's allowance when one is supplied."""
    if (
        limits is not None
        and not isinstance(limits, Limits)
        or budget is not None
        and not isinstance(budget, Budget)
    ):
        raise InputError("Patch codecs require Limits and Budget objects")
    if budget is not None and limits is not None and limits != budget.limits:
        raise InputError("Patch codec limits conflict with the shared budget")
    return budget if budget is not None else Budget(limits)


def _list(value: object, budget: Budget) -> list[object]:
    """Qualify decoded arrays before allocating typed records."""
    if not isinstance(value, list):
        raise InputError("Patch JSON inventories require arrays")
    budget.consume("states", len(value))
    budget.consume("bytes", 64 + 16 * len(value))
    return value


def _object(value: object, keys: set[str]) -> dict[str, object]:
    """Accept only the declared finite wire schema, never ignored extra fields."""
    if not isinstance(value, dict) or len(value) != len(keys) or value.keys() != keys:
        raise InputError("Malformed RDF patch JSON object")
    return cast(dict[str, object], value)


def term_data(value: PatchValue | None, budget: Budget) -> list[object]:
    """Preserve exact fields and distinguish defaults, tokens and named terms."""
    budget.consume("states", 4)
    if value is None:
        return ["default"]
    bound_term(value, budget)
    if isinstance(value, PatchBlank):
        return ["blank", value.token]
    if isinstance(value, URIRef):
        return ["iri", str(value)]
    return ["literal", value.lexical, value.language, value.datatype]


def read_term(value: object, budget: Budget) -> PatchValue | None:
    """Decode explicit RDF roles without parsing N-Quads or guessing blank IDs."""
    parts = _list(value, budget)
    budget.consume("work")
    if parts == ["default"]:
        return None
    if len(parts) == 2 and parts[0] in ("iri", "blank") and isinstance(parts[1], str):
        bound_text(parts[1], budget)
        return URIRef(parts[1]) if parts[0] == "iri" else PatchBlank(parts[1])
    if (
        len(parts) == 4
        and parts[0] == "literal"
        and isinstance(parts[1], str)
        and (parts[2] is None or isinstance(parts[2], str))
        and isinstance(parts[3], str)
    ):
        for text in parts[1:]:
            if text is not None:
                bound_text(cast(str, text), budget)
        return PatchLiteral(parts[1], parts[3], parts[2])
    raise InputError("Malformed portable RDF patch term")


def quad_data(value: PatchQuad, budget: Budget) -> list[object]:
    """Export one bounded exact quad without RDF literal value conversion."""
    budget.consume("work")
    budget.consume("states", 4)
    return [
        term_data(term, budget)
        for term in (value.subject, value.predicate, value.value, value.graph)
    ]


def read_quad(value: object, budget: Budget) -> PatchQuad:
    """Validate the four RDF positions before constructing the owned quad."""
    values = _list(value, budget)
    if len(values) != 4:
        raise InputError("A patch quad requires four term records")
    terms = [read_term(term, budget) for term in values]
    if (
        not isinstance(terms[0], URIRef | PatchBlank)
        or not isinstance(terms[1], URIRef)
        or not isinstance(terms[2], URIRef | PatchBlank | PatchLiteral)
        or terms[3] is not None
        and not isinstance(terms[3], URIRef | PatchBlank)
    ):
        raise InputError("Invalid RDF term positions in patch quad")
    return PatchQuad(terms[0], terms[1], terms[2], terms[3])


def witness_data(value: SnapshotWitness, budget: Budget) -> dict[str, object]:
    """Retain empty-graph inventory and scope beside exact quad fields."""
    validate_witness(value, budget)
    return {
        "kind": value.kind,
        "selection": term_data(value.graph_selection, budget),
        "quads": [quad_data(q, budget) for q in value.quads],
        "graphs": [term_data(g, budget) for g in value.graphs],
    }


def read_witness(value: object, budget: Budget) -> SnapshotWitness:
    """Read one finite complete structural witness and its explicit selection."""
    data = _object(value, {"kind", "selection", "quads", "graphs"})
    kind = data["kind"]
    if not isinstance(kind, str) or kind not in (
        "graph",
        "dataset",
        "default",
        "named",
    ):
        raise InputError("Unsupported RDF patch snapshot scope")
    selection = read_term(data["selection"], budget)
    if selection is not None and not isinstance(selection, URIRef | PatchBlank):
        raise InputError("Patch graph selection requires a resource")
    quads = tuple(read_quad(q, budget) for q in _list(data["quads"], budget))
    names = tuple(read_term(g, budget) for g in _list(data["graphs"], budget))
    if any(not isinstance(g, URIRef | PatchBlank) for g in names):
        raise InputError("Patch graph inventory requires named or blank resources")
    return SnapshotWitness(
        cast(SnapshotKind, kind),
        selection,
        quads,
        cast(tuple[PatchResource, ...], names),
    )
