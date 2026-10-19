"""Real record checks with qualified restart and fresh full-manifest key checks."""

import hashlib
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event

from rdflib import URIRef

from bijux_ontology.data.keys.checking import check_record_keys
from bijux_ontology.data.keys.types import KeyRecord
from bijux_ontology.declarations.keys import KeyContract
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.models.persistence.codecs import json_bytes
from bijux_ontology.models.persistence.identity import _source
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.patches.planning import capture_items
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.reports import Issue, Report, combine_reports
from bijux_ontology.workflows.revisions.management import RevisionLease

from .storage import Checkpoint, eligible
from .types import CorpusRecord, CorpusRecordResult, CorpusResult


@dataclass(frozen=True)
class _Record:
    """Exactly captured input and bindings shared by hashing and execution."""

    identifier: str
    source: RdfSource
    usages: tuple[RdfModelUsage, ...]
    key_subject: URIRef | None
    graph_name: str | None
    union: bool
    identity: str


def _capture(
    records: Sequence[CorpusRecord],
    lease: RevisionLease,
    operation: Operation,
    timeout: float,
) -> tuple[_Record, ...]:
    """Capture the entire finite manifest without trusting offsets or subclass reads."""
    values = capture_items(records, CorpusRecord, operation)
    if not values or len(values) > operation.limits.max_resources:
        raise InputError("Corpus requires a nonempty bounded stable record manifest")
    operation.consume("bytes", 1024 + 2048 * len(values))
    operation.consume("states", 32 * len(values))
    result = []
    identifiers: set[str] = set()
    for record in values:
        if type(record) is not CorpusRecord:
            raise InputError("Corpus records require ordinary immutable typed fields")
        identifier = record.identifier
        if type(identifier) is not str:
            raise InputError("Corpus identifiers require plain strings")
        bound_text(identifier, operation)
        if not identifier or identifier.isspace() or identifier in identifiers:
            raise InputError("Stable corpus identifiers must be nonempty and unique")
        identifiers.add(identifier)
        source, descriptor = _source(record.source, operation)
        if source.format is not None:
            operation.consume("work", len(source.format))
            operation.consume("bytes", 8 * len(source.format) + 64)
        if source.format is None or source.format.casefold() in ("json-ld", "jsonld"):
            raise InputError("Resume RDF records require explicit native RDF syntax")
        usages = capture_items(record.usages, RdfModelUsage, operation)
        if not usages:
            raise InputError("Each corpus record needs nonempty named model usages")
        data = []
        owned_usages = []
        operation.consume("bytes", 512 * len(usages))
        operation.consume("states", 8 * len(usages))
        for usage in usages:
            if type(usage) is not RdfModelUsage or type(usage.subject) is not URIRef:
                raise InputError("Restart bindings require ordinary named RDF subjects")
            model, subject, location = usage.model, str(usage.subject), usage.location
            if type(model) is not str or type(location) is not str:
                raise InputError("Restart usage metadata requires ordinary text")
            for text in (model, subject, location):
                bound_text(text, operation)
            operation.consume("bytes", 8 * len(subject) + 128)
            data.append({"model": model, "subject": subject, "location": location})
            owned_usages.append(
                RdfModelUsage(model, URIRef(subject), location=location)
            )
        graph = record.graph_name
        if graph is not None:
            if type(graph) is not str:
                raise InputError("Restart graph scope requires an exact named selector")
            bound_text(graph, operation)
            if graph != "@default":
                operation.consume("bytes", 8 * len(graph) + 128)
                absolute_iri(graph)
        union = record.union
        if type(union) is not bool or graph is not None and union:
            raise InputError("Choose an explicit physical graph or union")
        key = record.key_subject
        if key is not None:
            if type(key) is not str:
                raise InputError("Key subjects require explicit named IRIs")
            bound_text(key, operation)
            operation.consume("bytes", 8 * len(key) + 128)
            key = str(absolute_iri(key))
        raw = json_bytes(
            {
                "format": "bijux-ontology.corpus-rdf:1",
                "source": descriptor,
                "identifier": identifier,
                "usages": data,
                "key_subject": key,
                "graph_name": graph,
                "union": union,
                "revision_inputs": lease.identity.inputs_sha256,
                "revision_engine": lease.identity.engine_sha256,
                "limits": asdict(operation.limits),
                "timeout": timeout,
            },
            operation,
        )
        operation.consume("work", 1 + len(raw) // 4096)
        result.append(
            _Record(
                identifier,
                source,
                tuple(owned_usages),
                URIRef(key) if key is not None else None,
                graph,
                union,
                hashlib.sha256(raw).hexdigest(),
            )
        )
    return tuple(result)


def _failure(error: InputError, collector: IssueCollector, scope: str) -> Report:
    """Keep a malformed or unsupported record pending without inventing a finding."""
    unknown = isinstance(error, ResourceError)
    issue = Issue(
        "corpus.record_unverified" if unknown else "corpus.input_error",
        str(error),
        scope,
        kind="unverified" if unknown else "input_error",
    )
    collector.append(issue)
    return Report(scope, requested=1, issues=(issue,), complete=False)


class ResumeCorpus:
    """Resume actual independent RDF-model obligations in one local SQLite store.

    The supplied RevisionLease establishes the actual prepared semantic identity.
    Local checkpoints are trusted application state with corruption detection,
    not signed evidence from an untrusted publisher. Each record is committed
    separately. Complete negative results remain negative; unknown/exhausted work
    is pending. Selected application keys are rebuilt from all current resources
    on every run, even when every independent model check is reused.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_storage_bytes: int = 50_000_000,
        max_records: int = 128,
    ) -> None:
        """Select finite database and record capacities; no files open until run."""
        self._checkpoint = Checkpoint(path, max_storage_bytes, max_records)

    def run(
        self,
        lease: RevisionLease,
        records: Sequence[CorpusRecord],
        *,
        keys: Sequence[KeyContract] = (),
        limits: Limits | None = None,
        budget: Budget | None = None,
        timeout: float = 30,
        cancel: Event | None = None,
    ) -> CorpusResult:
        """Retain an actual live semantic owner for the entire resumed request."""
        if type(lease) is not RevisionLease:
            raise InputError("Resume requires an actual active revision lease")
        with lease._run():
            return self._check(
                lease,
                records,
                keys=keys,
                limits=limits,
                budget=budget,
                timeout=timeout,
                cancel=cancel,
            )

    def _check(
        self,
        lease: RevisionLease,
        records: Sequence[CorpusRecord],
        *,
        keys: Sequence[KeyContract] = (),
        limits: Limits | None = None,
        budget: Budget | None = None,
        timeout: float = 30,
        cancel: Event | None = None,
    ) -> CorpusResult:
        """Check a closed manifest with cumulative bounds and atomic record receipts.

        Limits must match the leased prepared model's fixed limits. Durability
        never changes raw model/key truth. The required aggregate includes both
        semantic completion and checkpoint uncertainty, while their two reports
        remain separately inspectable. No global result is ever checkpointed.
        """
        if type(lease) is not RevisionLease:
            raise InputError("Resume requires an actual active revision lease")
        operation = Operation(limits, budget, timeout, cancel)
        issues, checkpoint_issues = IssueCollector(operation), IssueCollector(operation)
        captured: tuple[_Record, ...] = ()
        results: list[CorpusRecordResult] = []
        observed: list[Report] = []
        key_records: list[KeyRecord] = []
        global_report = None
        checkpoint_ok = True
        checkpoint_checked = 0
        checkpoint_stopped = False
        required_globals = completed_globals = 0
        try:
            captured = _capture(records, lease, operation, timeout)
            contracts = capture_items(keys, KeyContract, operation)
            required_globals = len(contracts)
            if any(type(contract) is not KeyContract for contract in contracts):
                raise InputError("Global keys require ordinary typed KeyContracts")
            if contracts and any(record.key_subject is None for record in captured):
                raise InputError("Global keys require a named subject for every record")
            for record in captured:
                # Reserve publication references before any child can establish
                # a paid result; checkpoint failure cannot erase completed truth.
                operation.consume("work")
                operation.consume("states", 16)
                operation.consume("bytes", 1024)
                report = None
                reused = durable = False
                if checkpoint_ok:
                    try:
                        report = self._checkpoint.read(
                            record.identifier, record.identity, operation
                        )
                        if report is not None:
                            observed.append(report)
                            retain_report(report, operation, issues)
                            reused = durable = True
                    except LimitExceeded:
                        checkpoint_stopped = True
                        raise
                    except (InputError, OSError) as error:
                        checkpoint_ok = False
                        checkpoint_issues.append(
                            Issue(
                                "corpus.checkpoint_unverified",
                                str(error),
                                record.identifier,
                                kind="unverified",
                            )
                        )
                source = None
                if report is None or contracts:
                    try:
                        source = prepare_source(record.source, budget=operation)
                        if report is None:
                            report = lease.check_rdf(
                                source,
                                record.usages,
                                graph_name=record.graph_name,
                                union=record.union,
                                budget=operation,
                            ).report
                            observed.append(report)
                            retain_report(report, operation, issues)
                    except LimitExceeded:
                        raise
                    except InputError as error:
                        if report is None:
                            report = _failure(error, issues, record.identifier)
                        else:
                            issues.append(
                                Issue(
                                    "corpus.key_source_unverified",
                                    str(error),
                                    record.identifier,
                                    kind="unverified",
                                )
                            )
                assert report is not None
                result = CorpusRecordResult(
                    record.identifier, record.identity, report, reused, durable
                )
                results.append(result)
                if contracts and source is not None and record.key_subject is not None:
                    key_records.append(
                        KeyRecord(
                            record.identifier,
                            source,
                            record.key_subject,
                            graph_name=record.graph_name,
                            union=record.union,
                        )
                    )
                if not reused and checkpoint_ok and eligible(report):
                    try:
                        self._checkpoint.write(
                            record.identifier, record.identity, report, operation
                        )
                        results[-1] = CorpusRecordResult(
                            record.identifier, record.identity, report, False, True
                        )
                    except LimitExceeded:
                        checkpoint_stopped = True
                        raise
                    except (InputError, OSError) as error:
                        checkpoint_ok = False
                        checkpoint_issues.append(
                            Issue(
                                "corpus.checkpoint_unverified",
                                str(error),
                                record.identifier,
                                kind="unverified",
                            )
                        )
                checkpoint_checked += 1
            if contracts:
                global_results = []
                for contract in contracts:
                    operation.consume("bytes", 1024)
                    operation.consume("states", 8)
                    current = check_record_keys(
                        key_records,
                        contract,
                        complete=len(key_records) == len(captured),
                        budget=operation,
                    )
                    observed.append(current)
                    global_results.append(current)
                    completed_globals += 1
                    retain_report(current, operation, issues)
                global_report = combine_reports("corpus.keys", *global_results)
        except LimitExceeded as error:
            if checkpoint_stopped:
                checkpoint_issues.exhausted(str(error), location="corpus.checkpoint")
                if len(results) < len(captured) or completed_globals < required_globals:
                    issues.exhausted(str(error), location="corpus.validation")
            else:
                issues.exhausted(str(error), location="corpus.validation")
        except InputError as error:
            _failure(error, issues, "corpus.validation")
        for report in observed:
            issues.retain_completed(report)
        completed = {item.identifier for item in results if eligible(item.report)}
        pending = tuple(
            record.identifier
            for record in captured
            if record.identifier not in completed
        )
        semantic = Report(
            "corpus.validation",
            len(results),
            tuple(issues),
            requested=len(captured) or 1,
            complete=bool(captured)
            and not pending
            and completed_globals == required_globals,
            regime="record-independent-rdf-models-and-fresh-global-keys",
            children=tuple(item.report for item in results)
            + ((global_report,) if global_report is not None else ()),
        )
        checkpoint_report = Report(
            "corpus.checkpoint",
            checkpoint_checked,
            tuple(checkpoint_issues)
            or (tuple(issues) if not checkpoint_checked else ()),
            requested=len(captured) or 1,
            complete=checkpoint_ok
            and not checkpoint_stopped
            and checkpoint_checked == len(captured),
            regime="atomic-local-record-boundaries",
        )
        return CorpusResult(
            combine_reports("corpus.resume", semantic, checkpoint_report),
            semantic,
            checkpoint_report,
            tuple(results),
            pending,
            bool(captured),
            global_report,
            lease.revision,
            lease.identity,
        )
