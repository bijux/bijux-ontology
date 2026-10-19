"""Shared precedence and truthful materialization of explicit output metadata."""

from bijux_ontology.declarations.types import FieldDeclaration, ValueDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.interfaces.adapters.contracts import RdfField
from bijux_ontology.limits import IssueCollector


def select_contract(
    metadata: object,
    sidecar: object,
    *,
    location: str,
    issues: IssueCollector,
) -> RdfField | None:
    """Prefer a supplied serializer contract while retaining contradictory mappings."""
    if metadata is not None and not isinstance(metadata, RdfField):
        raise InputError("Expected RdfField metadata", location=location)
    if sidecar is not None and not isinstance(sidecar, RdfField):
        raise InputError("Expected RdfField serializer contracts", location=location)
    if metadata is not None and sidecar is not None:
        if metadata.predicate != sidecar.predicate:
            issues.append(
                Issue(
                    "adapter.predicate_contract_conflict",
                    "Serializer predicate differs from field metadata",
                    location,
                    expected=metadata.predicate,
                    actual=sidecar.predicate,
                )
            )
        if (
            metadata.values != sidecar.values
            or metadata.reverse != sidecar.reverse
            or (
                metadata.emission != sidecar.emission
                and "unknown" not in (metadata.emission, sidecar.emission)
            )
        ):
            issues.append(
                Issue(
                    "adapter.field_contract_conflict",
                    "Serializer value contract differs from field metadata",
                    location,
                    expected=repr(
                        (metadata.values, metadata.reverse, metadata.emission)
                    ),
                    actual=repr((sidecar.values, sidecar.reverse, sidecar.emission)),
                )
            )
    return sidecar if sidecar is not None else metadata


def select_alias(
    metadata: object,
    sidecar: object,
    *,
    name: str,
    location: str,
    issues: IssueCollector,
) -> str:
    """Keep explicit output-key conflicts visible when a serializer sidecar wins."""
    for value in (metadata, sidecar):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise InputError(
                "Serialization aliases must be nonempty strings", location=location
            )
    if metadata is not None and sidecar is not None and metadata != sidecar:
        issues.append(
            Issue(
                "adapter.alias_contract_conflict",
                "Serializer alias differs from field metadata",
                location,
                expected=str(metadata),
                actual=str(sidecar),
            )
        )
    return str(
        sidecar if sidecar is not None else metadata if metadata is not None else name
    )


def declare_field(
    name: str,
    key: str,
    contract: RdfField | None,
    *,
    identifiers: frozenset[str],
    location: str,
    issues: IssueCollector,
) -> FieldDeclaration | None:
    """Use supplied RDF kinds only when actual serializer output was described."""
    if contract is None:
        issues.append(
            Issue(
                "adapter.mapping_unverified",
                "Serialized field has no explicit RDF contract",
                location,
                kind="unverified",
            )
        )
        return None
    if not contract.output_contract:
        issues.append(
            Issue(
                "adapter.output_unverified",
                "Serializer output has not been explicitly described",
                location,
                kind="unverified",
            )
        )
    for value in contract.values:
        if (
            contract.output_contract
            and value.model is not None
            and value.model not in identifiers
        ):
            issues.append(
                Issue(
                    "adapter.output_unverified",
                    "Nested output model identifier is not registered",
                    location,
                    kind="unverified",
                    actual=value.model,
                )
            )
    return FieldDeclaration(
        name,
        contract.predicate,
        contract.values if contract.output_contract else (ValueDeclaration(),),
        key=key,
        reverse=contract.reverse,
        emission=contract.emission if contract.output_contract else "unknown",
        location=location,
    )
