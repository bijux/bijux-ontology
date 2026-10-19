"""Owned maintained query generation, separate from caller ontology evaluation."""

import json
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.processes.transport import exchange_worker
from bijux_ontology.rdf.stores.seals import verify_files
from bijux_ontology.rdf.terms import bound_text

from .bundles import MAIN_CLASS
from .types import DosdpPattern, DosdpQueryBackend


def generate_query(
    backend: DosdpQueryBackend,
    pattern: DosdpPattern,
    operation: Operation,
    heap_mb: int,
) -> tuple[str, str]:
    """Require native exit zero and retain its actual generated logical query.

    DOSDP's print-query option also unconditionally executes the query. A
    separate empty owned ontology satisfies that CLI requirement; its auxiliary
    TSV is never a match witness. Caller RDF is evaluated independently by the
    existing scoped maintained SPARQL adapter.
    """
    if type(backend) is not DosdpQueryBackend:
        raise InputError("Patterns require an explicitly prepared DOSDP backend")
    if type(heap_mb) is not int or not 32 <= heap_mb <= 65536:
        raise InputError("DOSDP JVM heap requires an integer from 32 to 65536 MiB")
    verify_files(backend.directory, backend.files, operation)
    bound_text(str(backend.java), operation)
    bound_text(str(backend.artifacts), operation)
    operation.consume("work", 0)
    backend.artifacts.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="pattern-query-", dir=backend.artifacts) as job:
        directory = Path(job)
        template, prefixes, ontology, output = (
            directory / name
            for name in ("pattern.json", "prefixes.json", "empty.ofn", "auxiliary.tsv")
        )
        operation.consume("bytes", len(pattern.content))
        template.write_bytes(pattern.content)
        mapping = dict(pattern.prefixes)
        operation.consume("bytes", json_bound(mapping, operation))
        prefixes.write_text(json.dumps(mapping, ensure_ascii=True), encoding="utf-8")
        operation.consume("bytes", 128)
        ontology.write_text(
            "Ontology(<urn:bijux:pattern-query:auxiliary>)", encoding="utf-8"
        )
        size = sum(len(str(backend.directory)) + len(name) + 2 for name in backend.jars)
        operation.consume("work", size)
        operation.consume("bytes", 4 * size)
        classpath = os.pathsep.join(
            str(backend.directory / name) for name in backend.jars
        )
        command = [
            str(backend.java),
            f"-Xmx{heap_mb}m",
            "-cp",
            classpath,
            MAIN_CLASS,
            "query",
            "--template=" + str(template),
            "--prefixes=" + str(prefixes),
            "--ontology=" + str(ontology),
            "--outfile=" + str(output),
            "--print-query=true",
            "--obo-prefixes=false",
            "--restrict-axioms-to=logical",
        ]
        for argument in command:
            bound_text(argument, operation)
        cap = min(
            operation.limits.max_bytes,
            65536,
            operation.remaining("bytes") // 32,
            operation.remaining("work") // 8,
        )
        if cap < 1024:
            raise LimitExceeded("DOSDP query has no bounded native output allowance")
        # Both pipes and host decoding/copying are reserved before native entry.
        operation.consume("bytes", 24 * cap)
        operation.consume("work", 4 * cap)
        operation.consume("states", 16)
        identity_raw = exchange_worker(
            command[:5] + ["--version"],
            b"",
            cap=cap,
            timeout=operation.deadline - time.monotonic(),
            cancel=operation.cancel,
        )
        try:
            identity = identity_raw.decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise ResourceError("DOSDP native identity is not UTF-8") from error
        if not identity.startswith(
            "name: dosdp-tools, version: " + backend.version + ","
        ):
            raise ResourceError(
                "DOSDP native identity differs from its selected version"
            )
        raw = exchange_worker(
            command,
            b"",
            cap=cap,
            timeout=operation.deadline - time.monotonic(),
            cancel=operation.cancel,
        )
        verify_files(backend.directory, backend.files, operation)
        if (
            not template.is_file()
            or not output.is_file()
            or output.stat().st_size > operation.limits.max_bytes
        ):
            raise ResourceError(
                "DOSDP auxiliary execution did not finish within its output profile"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ResourceError("DOSDP printed query is not UTF-8") from error
        header = "**** SPARQL query for " + str(template) + " ****"
        if not text.startswith(header + "\n"):
            raise ResourceError("DOSDP omitted the expected local pattern query header")
        query = text[len(header) :].strip()
        if not query.startswith("PREFIX ") or not query:
            raise ResourceError("DOSDP omitted the actual generated SPARQL query")
        bound_text(query, operation)
        return query, identity
