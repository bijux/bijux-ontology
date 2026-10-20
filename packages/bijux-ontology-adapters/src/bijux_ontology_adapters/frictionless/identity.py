"""Content identity for the finite native package worker protocol."""

import hashlib
from pathlib import Path

from bijux_ontology.execution.limits import Budget
from bijux_ontology.resources.files import capture_file

PROFILE = "bijux-ontology/frictionless-v1"


def worker_identity(budget: Budget) -> dict[str, str]:
    """Bind admission to shipped worker semantics, independently of install paths."""
    digest = hashlib.sha256()
    for name in ("identity.py", "profile.py", "worker.py"):
        with capture_file(Path(__file__).with_name(name), budget) as (stream, size):
            budget.consume("bytes", size + 1)
            budget.consume("work", size + 1)
            digest.update(name.encode())
            digest.update(str(size).encode())
            digest.update(stream.read(size + 1))
    return {"profile": PROFILE, "sha256": digest.hexdigest()}
