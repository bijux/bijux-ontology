"""Join GO assertions to companion records through exact native foreign keys."""

from collections.abc import Sequence
from dataclasses import replace

from rdflib import Graph, Literal, URIRef

from bijux_ontology.data.foreign_references.checking import check_foreign_references
from bijux_ontology.data.foreign_references.preparation import prepare_foreign_targets
from bijux_ontology.data.foreign_references.types import (
    ForeignReferenceContract,
    ForeignReferenceResult,
)
from bijux_ontology.data.keys.types import KeyRecord
from bijux_ontology.declarations.keys import KeyComponent, KeyContract
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.budgets import Operation

from .types import (
    GoAnnotation,
    GoGeneProduct,
)

SUBJECT_KEY = URIRef("urn:bijux:ontology:go:subject-key")


def join_gene_products(
    annotations: list[GoAnnotation],
    entities: list[GoGeneProduct],
    complete: bool,
    source_complete: bool,
    operation: Operation,
) -> tuple[list[GoAnnotation], ForeignReferenceResult]:
    """Reuse actual exact-key extraction and correlated foreign-reference checking."""
    graph = Graph()
    tables: tuple[tuple[str, Sequence[GoAnnotation | GoGeneProduct]], ...] = (
        ("assertion", annotations),
        ("entity", entities),
    )
    for prefix, items in tables:
        for index, item in enumerate(items):
            operation.consume("states", 2)
            operation.consume("bytes", 256)
            operation.consume("triples")
            graph.add(
                (
                    URIRef(f"urn:bijux:ontology:go:{prefix}:{index}"),
                    SUBJECT_KEY,
                    Literal(
                        item.subject
                        if isinstance(item, GoAnnotation)
                        else item.identifier
                    ),
                )
            )
    operation.consume("states", 4 * (len(annotations) + len(entities)) + 8)
    operation.consume("bytes", 512 * (len(annotations) + len(entities)) + 256)
    prepared = prepare_source(graph, budget=operation)
    sources = tuple(
        KeyRecord(str(i), prepared, URIRef(f"urn:bijux:ontology:go:assertion:{i}"))
        for i in range(len(annotations))
    )
    targets = tuple(
        KeyRecord(str(i), prepared, URIRef(f"urn:bijux:ontology:go:entity:{i}"))
        for i in range(len(entities))
    )
    key = KeyContract("GO subject identifier", (KeyComponent(SUBJECT_KEY),))
    contract = ForeignReferenceContract("GO annotation companion", key, key)
    operation.consume("states", 2 * len(annotations) + len(entities))
    operation.consume("bytes", 256 * (len(annotations) + len(entities)))
    target_index = prepare_foreign_targets(
        targets, contract, complete=complete, budget=operation
    )
    result = check_foreign_references(
        sources, target_index, source_complete=source_complete, budget=operation
    )
    joined = list(annotations)
    for resolution in result.resolutions:
        if resolution.status == "resolved":
            number = int(resolution.source.identifier)
            entity = entities[int(resolution.targets[0].identifier)]
            joined[number] = replace(
                joined[number],
                entity=entity,
                entity_taxon=entity.taxon
                if joined[number].entity_taxon is None
                else joined[number].entity_taxon,
            )
    return joined, result
