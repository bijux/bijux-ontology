"""Capture both ordinary input worlds before any reasoning callback executes."""

from dataclasses import dataclass
from hashlib import sha256

from bijux_ontology._json import _decode_json
from bijux_ontology.declarations.codecs import models_from_data
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.models.persistence.codecs import declaration_data, json_bytes

from ..axioms.codec.decoding import ProjectionDecoder
from ..axioms.codec.encoding import ConstructionEncoder
from ..axioms.types import DataProperty, NamedEntity, ObjectProperty
from ..documents.sources.capture import capture_document, capture_imports
from ..documents.types import OwlDocument
from ..relationships.types import RelationshipValue
from .types import (
    EntailmentContract,
    LogicalContract,
    LogicalSnapshot,
    ModelContract,
    SatisfiabilityContract,
    SeparationContract,
)


@dataclass(frozen=True)
class CapturedSide:
    """Plain owned records and bounded lookup metadata, with no stored verdicts."""

    source: OwlDocument
    imports: dict[str, OwlDocument] | None
    consumer: LogicalSnapshot
    contracts: dict[str, LogicalContract]
    fingerprints: dict[str, str]


def _members(value: object, encoder: ConstructionEncoder) -> tuple[object, ...]:
    """Require a finite immutable inventory before allocating retained references."""
    return encoder.sequence(value)


def _value(value: object, encoder: ConstructionEncoder) -> RelationshipValue:
    """Capture exact roles through the existing typed native projection decoder."""
    encoded = encoder.node(value)
    decoder = ProjectionDecoder(encoder.budget)
    if type(value) is NamedEntity and value.kind == "class":
        return decoder.entity(encoded, 1, ("class",))
    if type(value) in (ObjectProperty, DataProperty):
        result = decoder.property(encoded, 1)
        if isinstance(result, ObjectProperty) and result.inverse:
            raise InputError("Separation contracts require named forward properties")
        return result
    raise InputError("Separation members require named classes or properties")


def _snapshot(
    snapshot: LogicalSnapshot, budget: Budget
) -> tuple[LogicalSnapshot, dict[str, LogicalContract], dict[str, str]]:
    """Own inert obligations and declaration fingerprints without executing them."""
    if type(snapshot) is not LogicalSnapshot:
        raise InputError("Logical upgrades require ordinary LogicalSnapshot records")
    encoder = ConstructionEncoder(budget)
    contracts: dict[str, LogicalContract] = {}
    fingerprints: dict[str, str] = {}
    entailed: list[EntailmentContract] = []
    possible: list[SatisfiabilityContract] = []
    models: list[ModelContract] = []
    separate: list[SeparationContract] = []
    for records, expected in (
        (snapshot.entailments, EntailmentContract),
        (snapshot.satisfiability, SatisfiabilityContract),
        (snapshot.models, ModelContract),
        (snapshot.separate, SeparationContract),
    ):
        for raw in _members(records, encoder):
            if type(raw) is not expected:
                raise InputError("Logical snapshot member has the wrong contract type")
            item = raw
            identifier = encoder.text(item.identifier)
            location = encoder.text(item.location)
            if (
                not identifier
                or identifier == "ontology-consistency"
                or identifier in contracts
            ):
                raise InputError("Consumer stable IDs must be unique and nonempty")
            budget.consume("bytes", 512)
            budget.consume("states", 4)
            data: object
            owned: LogicalContract
            if isinstance(item, EntailmentContract):
                axioms, wire = encoder.axioms((item.axiom,))
                owned = EntailmentContract(identifier, axioms[0], location)
                data = wire
                entailed.append(owned)
            elif isinstance(item, SatisfiabilityContract):
                iri = encoder.text(item.class_iri, iri=True)
                owned = SatisfiabilityContract(identifier, iri, location)
                data = iri
                possible.append(owned)
            elif isinstance(item, ModelContract):
                _members(item.models, encoder)
                data = declaration_data(item.models, budget)
                encoded = json_bytes(data, budget)
                decoded = _decode_json(
                    encoded,
                    max_bytes=budget.limits.max_bytes,
                    max_depth=budget.limits.max_depth,
                )
                owned = ModelContract(identifier, models_from_data(decoded), location)
                if not owned.models:
                    raise InputError("A model contract requires a nonempty registry")
                models.append(owned)
            else:
                groups: list[tuple[RelationshipValue, ...]] = []
                seen: set[RelationshipValue] = set()
                role: type[RelationshipValue] | None = None
                for group in _members(item.groups, encoder):
                    members: list[RelationshipValue] = []
                    for member in _members(group, encoder):
                        value = _value(member, encoder)
                        budget.consume("bytes", 256)
                        budget.consume("states", 2)
                        if (
                            value in seen
                            or role is not None
                            and type(value) is not role
                        ):
                            raise InputError(
                                "Separation groups require disjoint same-role members"
                            )
                        seen.add(value)
                        role = type(value)
                        members.append(value)
                    if not members:
                        raise InputError("Separation groups must be nonempty")
                    groups.append(tuple(members))
                if len(groups) < 2:
                    raise InputError("Separation requires at least two groups")
                owned = SeparationContract(identifier, tuple(groups), location)
                data = [[encoder.node(value) for value in group] for group in groups]
                separate.append(owned)
            payload = json_bytes((type(owned).__name__, location, data), budget)
            budget.consume("work", len(payload))
            contracts[identifier] = owned
            fingerprints[identifier] = sha256(payload).hexdigest()
    return (
        LogicalSnapshot(
            tuple(entailed), tuple(possible), tuple(models), tuple(separate)
        ),
        contracts,
        fingerprints,
    )


def capture_side(
    source: OwlDocument,
    snapshot: LogicalSnapshot,
    imports: dict[str, OwlDocument] | None,
    budget: Budget,
) -> CapturedSide:
    """Capture ordinary native descriptors and inert consumer data exactly once."""
    if imports is not None and type(imports) is not dict:
        raise InputError("Logical upgrade imports require an ordinary finite dict")
    owned_source = capture_document(source, budget)
    consumer, contracts, fingerprints = _snapshot(snapshot, budget)
    owned_imports = capture_imports(imports, budget)
    if owned_imports is not None:
        budget.consume("bytes", 64 + 128 * len(owned_imports))
    return CapturedSide(
        owned_source,
        None if owned_imports is None else dict(owned_imports),
        consumer,
        contracts,
        fingerprints,
    )
