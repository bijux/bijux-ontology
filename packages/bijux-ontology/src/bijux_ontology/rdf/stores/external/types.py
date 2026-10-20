"""Concrete caller ownership with stability limited to each native read."""

from dataclasses import dataclass

import pyoxigraph as ox

from bijux_ontology.diagnostics.errors import InputError


@dataclass(frozen=True, init=False)
class ExternalStore:
    """Borrow a native store without claiming an immutable cross-call revision.

    Each supported operation uses exactly one maintained read transaction.
    The caller retains write ownership; later operations may see later commits.
    """

    _store: ox.Store
    isolation: str

    def __init__(self) -> None:
        raise InputError("Use prepare_external_store for a caller-managed store")

    @classmethod
    def _borrow(cls, store: ox.Store) -> "ExternalStore":
        """Retain the actual native owner instead of exporting mutable graphs."""
        result = object.__new__(cls)
        object.__setattr__(result, "_store", store)
        object.__setattr__(result, "isolation", "single-native-read")
        return result
