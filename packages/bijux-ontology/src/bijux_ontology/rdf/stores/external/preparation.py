"""Explicit borrowing of the maintained transaction-capable native handle."""

import pyoxigraph as ox

from bijux_ontology.errors import InputError

from .types import ExternalStore


def prepare_external_store(store: ox.Store) -> ExternalStore:
    """Accept Pyoxigraph's native per-query isolation without scanning its data.

    RDFLib graphs and readonly wrappers have no corresponding isolation contract
    here. This does not open, close, copy or change the caller's native store.
    """
    if not isinstance(store, ox.Store):
        raise InputError("External reads require a caller-managed Pyoxigraph Store")
    return ExternalStore._borrow(store)
