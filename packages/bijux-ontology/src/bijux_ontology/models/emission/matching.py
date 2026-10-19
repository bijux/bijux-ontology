"""Pure actual-value contract matching shared by RDF and JSON-LD outputs."""

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.declarations.types import (
    ModelDeclaration,
    SubjectIdentity,
    ValueDeclaration,
)
from bijux_ontology.jsonld.evidence.values import ValueTraits


def identity_matches(contract: SubjectIdentity, traits: ValueTraits) -> bool:
    """Compare explicit subject identity without inferring missing names."""
    if contract.expected_iri is not None and traits.identifier != contract.expected_iri:
        return False
    return contract.allow_blank or traits.identifier_kind == "iri"


def matches_value(
    value: ValueDeclaration,
    traits: ValueTraits,
    registry: dict[str, ModelDeclaration],
    *,
    container: str = "unordered",
    empty: bool = False,
    role_result: bool | None = None,
) -> bool | None:
    """Compare observed scalar traits without parsing or inferring RDF semantics."""
    if value.kind == "unknown" or value.container not in (None, "@set", "@list"):
        return None
    expected_container = "list" if value.container == "@list" else "unordered"
    if container != expected_container:
        return False
    if empty:
        return True if expected_container == "list" else None
    if value.kind == "literal":
        if traits.kind != "literal":
            return False
        if value.datatype is not None and value.datatype != traits.datatype:
            return False
        return trait_matches(
            value.language, traits.language, fold=True
        ) and trait_matches(value.direction, traits.direction)

    if value.kind == "iri" and traits.kind != "iri":
        return False
    if value.kind == "object" and traits.kind not in {"node", "iri", "blank"}:
        return False
    required = (
        set()
        if value.role is not None
        and value.role.kind == "instance"
        and value.role.bridge is None
        else set(value.types)
    )
    if value.model is not None:
        if value.model not in registry:
            return None
        model = registry[value.model]
        if not identity_matches(model.subject, traits):
            return False
        required.update(model.types)
    if required - set(traits.types):
        return None if traits.kind in {"iri", "blank"} and not traits.types else False
    if value.role is not None:
        return role_result
    return True


def trait_matches(
    required: str | Absent | None, actual: str | None, *, fold: bool = False
) -> bool:
    """Compare required, absent and unchecked metadata without rewriting values."""
    if required is None:
        return True
    if required is ABSENT:
        return actual is None
    return required.lower() == (actual or "").lower() if fold else required == actual
