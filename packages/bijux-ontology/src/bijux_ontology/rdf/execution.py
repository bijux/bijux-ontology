"""Bounded owned-process transport for RDF engines and resource preparation."""

import sys
import threading
from typing import Literal

from bijux_ontology.errors import InputError
from bijux_ontology.processes.transport import checkpoint as checkpoint
from bijux_ontology.processes.transport import exchange_worker

WorkerModule = Literal[
    "bijux_ontology.rdf._comparison_worker",
    "bijux_ontology.rdf.stores.worker",
    "bijux_ontology.data.streams.worker",
    "bijux_ontology.data.analysis.worker",
    "bijux_ontology.rdf.queries.worker",
    "bijux_ontology.rdf.query_transforms.worker",
    "bijux_ontology.rdf.triple_terms.worker",
    "bijux_ontology.rdf.literal_values.markup.worker",
    "bijux_ontology.rdf.canonical.worker",
    "bijux_ontology.rdf.patches.correspondence",
    "bijux_ontology.workflows.projects.acquisition.worker",
    "bijux_ontology.workflows.projects.representations.worker",
    "bijux_ontology.workflows.projects.parallel.worker",
    "bijux_ontology.workflows.reproductions.worker",
    "bijux_ontology.vocabulary.mappings.worker",
    "bijux_ontology.shacl.rules.worker",
    "bijux_ontology.rdf.entailment.worker",
    "bijux_ontology.rdf.answers.worker",
    "bijux_ontology.data.query_usage.worker",
]


def execute_worker(
    module: WorkerModule,
    payload: bytes,
    *,
    cap: int,
    timeout: float,
    cancel: threading.Event | None = None,
) -> bytes:
    """Run an owned RDF worker and reap its process group.

    Payloads and replies are opaque bytes. Domain modules own their protocols;
    this transport owns bounded pipes, cancellation and the worker lifetime.
    """
    if module not in (
        "bijux_ontology.rdf._comparison_worker",
        "bijux_ontology.rdf.stores.worker",
        "bijux_ontology.data.streams.worker",
        "bijux_ontology.data.analysis.worker",
        "bijux_ontology.rdf.queries.worker",
        "bijux_ontology.rdf.query_transforms.worker",
        "bijux_ontology.rdf.triple_terms.worker",
        "bijux_ontology.rdf.literal_values.markup.worker",
        "bijux_ontology.rdf.canonical.worker",
        "bijux_ontology.rdf.patches.correspondence",
        "bijux_ontology.workflows.projects.acquisition.worker",
        "bijux_ontology.workflows.projects.representations.worker",
        "bijux_ontology.workflows.projects.parallel.worker",
        "bijux_ontology.workflows.reproductions.worker",
        "bijux_ontology.vocabulary.mappings.worker",
        "bijux_ontology.shacl.rules.worker",
        "bijux_ontology.rdf.entailment.worker",
        "bijux_ontology.rdf.answers.worker",
        "bijux_ontology.data.query_usage.worker",
    ):
        raise InputError("Unsupported RDF worker module")
    if type(cap) is not int or cap <= 0:
        raise InputError("RDF workers require a positive output cap")
    return exchange_worker(
        [sys.executable, "-I", "-B", "-m", module, str(cap)],
        payload,
        cap=cap,
        timeout=timeout,
        cancel=cancel,
    )
