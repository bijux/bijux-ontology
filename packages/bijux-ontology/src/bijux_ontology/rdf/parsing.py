"""Maintained RDF syntax parsing with bounded lexical-preserving conversion."""

from dataclasses import dataclass
from typing import Literal as TypingLiteral
from typing import cast

import pyoxigraph as ox
from rdflib import BNode, Dataset, URIRef
from rdflib.graph import DATASET_DEFAULT_GRAPH_ID

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.limits import Budget

from .terms import oxigraph_term
from .trig import retain_trig_inventory
from .xml.preparation import prepare_xml

RdfFormat = TypingLiteral["turtle", "nt", "xml", "trig", "nquads", "json-ld"]


RDF_FORMATS = {
    "turtle": ox.RdfFormat.TURTLE,
    "nt": ox.RdfFormat.N_TRIPLES,
    "xml": ox.RdfFormat.RDF_XML,
    "trig": ox.RdfFormat.TRIG,
    "nquads": ox.RdfFormat.N_QUADS,
}


@dataclass(frozen=True)
class ParsedDataset:
    """Owned primary terms with explicitly established graph-inventory fidelity."""

    dataset: Dataset
    graph_fidelity: TypingLiteral["complete", "nonempty-only"] = "complete"


def parse_dataset(
    payload: bytes,
    *,
    syntax: str,
    base: str | None,
    budget: Budget,
    max_triples: int,
    preserve_literals: bool,
    max_bytes: int | None = None,
) -> ParsedDataset:
    """Parse bounded RDF syntax while retaining shared blank-node relations."""
    dataset = Dataset()
    if syntax == "xml":
        payload = prepare_xml(payload, budget=budget, max_bytes=max_bytes).payload
    try:
        parser = ox.parse(input=payload, format=RDF_FORMATS[syntax], base_iri=base)
        nodes: dict[str, BNode] = {}
        for count, quad in enumerate(parser, 1):
            budget.consume("triples")
            if count > max_triples:
                raise LimitExceeded("RDF document exceeds max_triples")
            if isinstance(
                quad.graph_name, ox.NamedNode
            ) and quad.graph_name.value == str(DATASET_DEFAULT_GRAPH_ID):
                raise InputError(
                    "Named graph uses the reserved default graph identifier"
                )
            target = (
                dataset.default_graph
                if isinstance(quad.graph_name, ox.DefaultGraph)
                else dataset.graph(
                    cast(
                        URIRef | BNode,
                        oxigraph_term(quad.graph_name, nodes, preserve_literals),
                    )
                )
            )
            target.add(
                (
                    oxigraph_term(quad.subject, nodes, preserve_literals),
                    oxigraph_term(quad.predicate, nodes, preserve_literals),
                    oxigraph_term(quad.object, nodes, preserve_literals),
                )
            )
        for prefix, namespace in parser.prefixes.items():
            dataset.bind(prefix, URIRef(namespace))
    except (SyntaxError, ValueError, OSError) as error:
        if isinstance(error, InputError):
            raise
        raise ResourceError(f"Cannot parse {syntax} RDF source: {error}") from error
    if syntax == "trig":
        # RDFLib requires an initial base even for a document's first absolute
        # @base. The primary parser's final base is an authored fallback only
        # when no caller base exists; earlier relative IRIs were already rejected.
        complete = retain_trig_inventory(
            payload, dataset, nodes, base=base or parser.base_iri, budget=budget
        )
        if not complete:
            return ParsedDataset(dataset, "nonempty-only")
    return ParsedDataset(dataset)
