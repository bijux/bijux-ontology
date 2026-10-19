"""Pinned source records and explicit local identifier-policy replacements."""

import json
from dataclasses import dataclass
from typing import cast

from rdflib import BNode, Literal

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.accessions.types import AccessionRule
from bijux_ontology.vocabulary.registry.types import IdentifierRegistry


def _text(value: object, budget: Budget) -> str:
    """Qualify textual metadata before normalization, copies or dictionary keys."""
    if not isinstance(value, str) or isinstance(value, BNode | Literal):
        raise InputError(
            "Bioregistry metadata requires text, not RDF blank/literal terms"
        )
    bound_text(value, budget)
    return str(value)


@dataclass(frozen=True)
class BioregistryOverride:
    """An explicit application replacement, never inferred source equivalence.

    Namespace and canonical spelling replace the corresponding selected source
    policy. Aliases are additive. A rule explicitly replaces source validation
    and normalization semantics with the existing accession-rule semantics.
    Source records and all original fields remain available independently.
    """

    prefix: str
    namespace: str | None = None
    canonical_prefix: str | None = None
    aliases: tuple[str, ...] = ()
    rule: AccessionRule | None = None

    def __post_init__(self) -> None:
        """Own finite scalar choices without importing a registry or regex engine."""
        budget = Budget()
        for name in ("prefix", "namespace", "canonical_prefix"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _text(value, budget))
        if type(self.aliases) is not tuple:
            raise InputError("Local aliases require an immutable tuple")
        budget.consume("states", len(self.aliases))
        object.__setattr__(
            self, "aliases", tuple(_text(value, budget) for value in self.aliases)
        )
        if self.rule is not None and not isinstance(self.rule, AccessionRule):
            raise InputError("Local validation requires an AccessionRule")


@dataclass(frozen=True, init=False)
class BioregistrySnapshot:
    """Exact supplied bytes, retained fields and separately interpreted policy.

    A digest pins supplied content, not its authenticity or freshness. Registry
    metadata never declares an RDF entity or proves that an identifier exists.
    Unsupported rule semantics can leave a useful registry beside an unknown
    report; callers must inspect the required report before accepting policy.
    """

    payload: bytes
    revision: str
    sha256: str
    prefixes: tuple[str, ...]
    registry: IdentifierRegistry | None
    rules: tuple[AccessionRule, ...]
    overrides: tuple[BioregistryOverride, ...]
    unsupported: tuple[tuple[str, str], ...]
    uninterpreted: tuple[tuple[str, str], ...]
    report: Report
    _records_json: str

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Require the byte-pinned acquisition factory to bind source and policy."""
        raise InputError("Use read_bioregistry to acquire a pinned snapshot")

    @property
    def records(self) -> dict[str, object]:
        """Return all selected original fields as an independent JSON value."""
        return cast(dict[str, object], json.loads(self._records_json))

    @classmethod
    def _from_owned(
        cls,
        *,
        payload: bytes,
        revision: str,
        sha256: str,
        prefixes: tuple[str, ...],
        registry: IdentifierRegistry | None,
        rules: tuple[AccessionRule, ...],
        overrides: tuple[BioregistryOverride, ...],
        unsupported: tuple[tuple[str, str], ...],
        uninterpreted: tuple[tuple[str, str], ...],
        report: Report,
        records_json: str,
    ) -> "BioregistrySnapshot":
        """Adopt only metadata already qualified under the factory operation."""
        result = object.__new__(cls)
        for name, value in (
            ("payload", payload),
            ("revision", revision),
            ("sha256", sha256),
            ("prefixes", prefixes),
            ("registry", registry),
            ("rules", rules),
            ("overrides", overrides),
            ("unsupported", unsupported),
            ("uninterpreted", uninterpreted),
            ("report", report),
            ("_records_json", records_json),
        ):
            object.__setattr__(result, name, value)
        return result
