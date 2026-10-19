"""Fresh list chains inside the existing exact dataset-patch representation."""

from dataclasses import replace

from rdflib.namespace import RDF

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.patches.types import DatasetPatch
from bijux_ontology.rdf.records.types import (
    PatchBlank,
    PatchQuad,
    PatchValue,
    SnapshotWitness,
)


def copy_chain(
    base: SnapshotWitness,
    incoming: PatchQuad,
    before: tuple[PatchValue, ...],
    after: tuple[PatchValue, ...],
    budget: Budget,
    *,
    force: bool = False,
) -> DatasetPatch:
    """Rewire one root edge; retain every original cell and sibling assertion."""
    if type(force) is not bool:
        raise InputError("List chain force must be a boolean")
    if before == after and not force:
        return DatasetPatch(base, base)
    budget.consume("states", 3 * len(after) + len(base.quads) + 2)
    budget.consume("work", 3 * len(after) + len(base.quads) + 2)
    budget.consume("triples", 2 * len(after) + 1)
    budget.consume("bytes", 64 * len(after))
    # Capture uses base: tokens; new cells occupy a disjoint explicit token space.
    cells = tuple(PatchBlank("list:" + str(index)) for index in range(len(after)))
    replacement = replace(incoming, value=cells[0] if cells else RDF.nil)
    additions = [replacement]
    for index, value in enumerate(after):
        budget.consume("work")
        additions.extend(
            (
                PatchQuad(cells[index], RDF.first, value, incoming.graph),
                PatchQuad(
                    cells[index],
                    RDF.rest,
                    cells[index + 1] if index + 1 < len(cells) else RDF.nil,
                    incoming.graph,
                ),
            )
        )
    target = replace(
        base,
        quads=tuple(quad for quad in base.quads if quad != incoming) + tuple(additions),
    )
    return DatasetPatch(base, target, removals=(incoming,), additions=tuple(additions))
