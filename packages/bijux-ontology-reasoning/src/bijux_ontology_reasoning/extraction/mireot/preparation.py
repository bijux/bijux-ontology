"""Capture finite original documents, role boundaries and retention requirements."""

from dataclasses import dataclass

from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.limits import Budget

from ...axioms.codec.decoding import ProjectionDecoder
from ...axioms.codec.encoding import ConstructionEncoder
from ...axioms.types import NamedEntity, ProjectedAxiom
from ...documents.sources.capture import capture_document
from ...documents.types import OwlDocument
from .types import MireotBoundary, MireotRetention


@dataclass(frozen=True)
class MireotPlan:
    """Owned finite native inputs captured before any producer callback."""

    source: OwlDocument
    boundaries: tuple[MireotBoundary, ...]
    retention: MireotRetention
    signature: tuple[NamedEntity, ...]
    obligations: tuple[ProjectedAxiom, ...]
    module_iri: str
    module_version_iri: str | None


def prepare_import(
    source: OwlDocument,
    boundaries: tuple[MireotBoundary, ...],
    retention: MireotRetention,
    postconditions: tuple[ProjectedAxiom, ...],
    module_iri: str,
    module_version_iri: str | None,
    budget: Budget,
) -> MireotPlan:
    """Qualify exact ordinary records and prevent native term-file token injection."""
    encoder = ConstructionEncoder(budget)
    decoder = ProjectionDecoder(budget)
    original = capture_document(source, budget)
    if original.format != "functional":
        raise ResourceError("MIREOT requires the declared original Functional profile")
    identity = encoder.text(module_iri, iri=True)
    version = (
        None
        if module_version_iri is None
        else encoder.text(module_version_iri, iri=True)
    )
    if type(retention) is not MireotRetention:
        raise InputError("MIREOT requires an ordinary retention record")
    annotations = encoder.text(retention.entity_annotations)
    root_annotations = encoder.text(retention.ontology_annotations)
    if annotations != "retain-all" or root_annotations not in {"copy", "omit"}:
        raise InputError("Choose retain-all entity and copy/omit root annotations")

    def entity(value: object) -> NamedEntity:
        """Capture exact supported role identity before hashing or native staging."""
        if type(value) is not NamedEntity:
            raise InputError("MIREOT terms require ordinary named entity records")
        result = decoder.entity(
            encoder.node(value), 1, ("class", "object_property", "data_property")
        )
        if any(character.isspace() for character in result.iri):
            raise InputError("Native term-file identities cannot contain whitespace")
        return result

    selected = []
    signature: dict[NamedEntity, None] = {}
    seen: set[MireotBoundary] = set()
    for raw in encoder.sequence(boundaries):
        if type(raw) is not MireotBoundary:
            raise InputError("MIREOT needs exact explicit boundary records")
        budget.consume("bytes", 1024)
        lower, upper = entity(raw.lower), entity(raw.upper)
        item = MireotBoundary(lower, upper)
        if lower.kind != upper.kind or item in seen:
            raise InputError("Choose distinct same-role lower/upper boundaries")
        seen.add(item)
        selected.append(item)
        signature[lower] = signature[upper] = None
    if not selected:
        raise InputError("MIREOT requires nonempty explicit boundaries")
    properties = []
    for raw in encoder.sequence(retention.required_properties):
        property_entity = entity(raw)
        if (
            property_entity.kind not in {"object_property", "data_property"}
            or property_entity in properties
        ):
            raise InputError("Required properties need distinct object/data roles")
        budget.consume("bytes", 128)
        properties.append(property_entity)
        signature[property_entity] = None
    required, _ = encoder.axioms(retention.required_axioms)
    obligations, _ = encoder.axioms(postconditions)
    if not obligations:
        raise InputError("Select nonempty consumer entailments to recheck")
    budget.consume("bytes", 256 + len(signature) * 64)
    return MireotPlan(
        original,
        tuple(selected),
        MireotRetention(
            tuple(properties), required, "retain-all", retention.ontology_annotations
        ),
        tuple(signature),
        obligations,
        identity,
        version,
    )
