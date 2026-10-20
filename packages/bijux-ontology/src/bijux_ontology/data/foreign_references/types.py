"""Explicit foreign-key policy, owned lookup state and correlated actual records."""

from dataclasses import dataclass, field
from typing import Literal

from rdflib.term import Node

from bijux_ontology.data.keys.extraction import _Key
from bijux_ontology.data.keys.types import KeyRecord
from bijux_ontology.declarations.keys import KeyContract
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report


@dataclass(frozen=True)
class ForeignReferenceContract:
    """Join independently scoped records using aligned explicit key semantics."""

    name: str
    source: KeyContract
    target: KeyContract
    minimum_targets: int = field(default=1, kw_only=True)
    maximum_targets: int | None = field(default=1, kw_only=True)
    lookup: Literal["closed", "open"] = field(default="closed", kw_only=True)
    target_model: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Reject incompatible equality regimes and invented cardinality policies."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise InputError("Foreign references require a nonempty name")
        object.__setattr__(self, "name", str(self.name))
        if not isinstance(self.source, KeyContract) or not isinstance(
            self.target, KeyContract
        ):
            raise InputError("Foreign references require typed source and target keys")
        if len(self.source.components) != len(self.target.components) or any(
            (a.equality, a.normalization) != (b.equality, b.normalization)
            for a, b in zip(self.source.components, self.target.components, strict=True)
        ):
            raise InputError("Foreign key components require aligned equality policies")
        if (
            type(self.minimum_targets) is not int
            or self.minimum_targets < 0
            or (
                self.maximum_targets is not None
                and (
                    type(self.maximum_targets) is not int
                    or self.maximum_targets < self.minimum_targets
                )
            )
        ):
            raise InputError(
                "Foreign target counts require explicit nonnegative bounds"
            )
        if self.lookup not in ("closed", "open"):
            raise InputError("Foreign lookup policy must be closed or open")
        if self.target_model is not None:
            if not isinstance(self.target_model, str) or not self.target_model:
                raise InputError(
                    "Selected target models require an explicit identifier"
                )
            object.__setattr__(self, "target_model", str(self.target_model))


@dataclass(frozen=True, init=False)
class ForeignTargetIndex:
    """Operation-owned reusable target records; only preparation constructs indexes."""

    contract: ForeignReferenceContract
    report: Report
    complete: bool
    duplicates: tuple[tuple[KeyRecord, KeyRecord], ...]
    _keys: tuple[_Key, ...]

    def __new__(cls) -> "ForeignTargetIndex":
        """Require qualified preparation instead of uninitialized public indexes."""
        raise InputError("Use prepare_foreign_targets to acquire a target index")


@dataclass(frozen=True)
class ForeignResolution:
    """Original source key and distinct selected target records, never minted IDs."""

    source: KeyRecord
    values: tuple[Node, ...]
    targets: tuple[KeyRecord, ...]
    status: Literal["resolved", "missing", "ambiguous", "unverified"]


@dataclass(frozen=True)
class ForeignReferenceResult:
    """Raw required reports beside correlated completed source resolutions."""

    report: Report
    resolutions: tuple[ForeignResolution, ...]
