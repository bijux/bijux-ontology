"""Atomic registry composition retaining conflicting ownership evidence."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from bijux_ontology.diagnostics.reports import Report

from .types import IdentifierRegistry, NamespaceContribution, _resolve


@dataclass(frozen=True)
class RegistryMerge:
    """A complete registry, or conflict diagnostics without a partial policy."""

    registry: IdentifierRegistry | None
    report: Report


def merge_identifier_registry(
    contributions: Sequence[NamespaceContribution],
    *,
    canonical_prefixes: Mapping[str, str] | None = None,
) -> RegistryMerge:
    """Merge atomically; canonical choices cannot remap contributed aliases."""
    owned, canonical, issues = _resolve(contributions, canonical_prefixes)
    report = Report("vocabulary.registry", checked=len(owned), issues=issues)
    registry = (
        None
        if issues
        else IdentifierRegistry(owned, canonical_prefixes=dict(canonical))
    )
    return RegistryMerge(registry, report)
