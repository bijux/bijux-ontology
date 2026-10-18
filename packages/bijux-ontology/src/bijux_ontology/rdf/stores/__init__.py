"""Sealed disk-backed RDF snapshots and explicitly bounded native reads."""

from .materialization import materialize_snapshot
from .preparation import open_snapshot, snapshot_rdf
from .queries import query_snapshot
from .types import MaterializedRdf, StoredRdf, StoredRdfResult

__all__ = [
    "MaterializedRdf",
    "StoredRdf",
    "StoredRdfResult",
    "materialize_snapshot",
    "open_snapshot",
    "query_snapshot",
    "snapshot_rdf",
]
