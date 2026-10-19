"""Funded native role serialization for independent SHACL provider invocations."""

import json

from rdflib import Dataset, Literal

from bijux_ontology.execution.limits import Budget
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.rdf.terms import bound_node


def encode_roles(graphs: Dataset, options: dict[str, object], budget: Budget) -> bytes:
    """Prepay native N-Quads and JSON workspace without replacing serializers."""
    bound = 64
    for quad in graphs.quads():
        budget.consume("work")
        budget.consume("states", 4)
        bound += 256
        for term in quad:
            if term is None:
                continue
            bound_node(term, budget)
            size = len(str(term))
            if isinstance(term, Literal):
                size += len(term.datatype or "") + len(term.language or "")
            bound += 12 * size + 128
    budget.consume("bytes", 4 * bound)
    budget.consume("work", bound)
    text = graphs.serialize(format="nquads")
    options["graphs"] = text
    encoded_bound = json_bound(options, budget)
    budget.consume("bytes", 2 * encoded_bound)
    return json.dumps(options).encode()
