"""Bounded capture of explicit native inputs, typed signature and selected proofs."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from bijux_ontology.errors import InputError
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri

from ...axioms.codec.encoding import ConstructionEncoder
from ...axioms.types import NamedEntity, ProjectedAxiom
from ...documents.sources.capture import capture_document, capture_imports
from ...documents.types import OwlDocument
from .types import LocalityMethod


@dataclass(frozen=True)
class LocalityPlan:
    """One owned native closure and inert finite role selections."""

    source: OwlDocument
    imports: Mapping[str, OwlDocument] | None
    signature: tuple[NamedEntity, ...]
    signature_wire: tuple[dict[str, object], ...]
    obligations: tuple[ProjectedAxiom, ...]
    obligation_wire: tuple[dict[str, object], ...]
    method: LocalityMethod
    module_iri: str


def prepare_locality(
    source: OwlDocument,
    signature: Sequence[NamedEntity],
    obligations: Sequence[ProjectedAxiom],
    method: object,
    module_iri: str,
    imports: Mapping[str, OwlDocument] | None,
    import_policy: str,
    annotations: str,
    budget: Budget,
) -> LocalityPlan:
    """Capture exact ordinary descriptors before any native operation executes."""
    if type(method) is not str:
        raise InputError("Choose BOT, TOP or STAR syntactic locality explicitly")
    bound_text(method, budget)
    if method not in {"BOT", "TOP", "STAR"}:
        raise InputError("Choose BOT, TOP or STAR syntactic locality explicitly")
    if (
        type(import_policy) is not str
        or type(annotations) is not str
        or import_policy != "flatten"
        or annotations != "retain"
    ):
        raise InputError("Select explicit flatten imports and retain annotations")
    if type(module_iri) is not str:
        raise InputError("The derived module needs an explicit absolute identity")
    bound_text(module_iri, budget)
    absolute_iri(module_iri)
    owned = capture_document(source, budget)
    supplied = capture_imports(imports, budget)
    selected = capture_items(signature, NamedEntity, budget)
    if not selected:
        raise InputError("Locality extraction requires a nonempty typed signature")
    budget.consume("bytes", 64 + len(selected) * 64)
    budget.consume("states", len(selected))
    seen: set[tuple[str, str]] = set()
    wire = []
    encoder = ConstructionEncoder(budget)
    for entity in selected:
        if type(entity) is not NamedEntity:
            raise InputError("Locality signature requires exact NamedEntity records")
        item = encoder.node(entity)
        if (
            entity.kind
            not in {
                "class",
                "object_property",
                "data_property",
                "annotation_property",
                "datatype",
                "named_individual",
            }
            or (entity.kind, entity.iri) in seen
        ):
            raise InputError("Locality signature roles must be valid and distinct")
        seen.add((entity.kind, entity.iri))
        wire.append(item)
    requests, query_wire = encoder.axioms(obligations)
    if not requests:
        raise InputError("Select nonempty consumer entailments to recheck")
    return LocalityPlan(
        owned,
        supplied,
        selected,
        tuple(wire),
        requests,
        tuple(query_wire),
        cast(LocalityMethod, method),
        module_iri,
    )
