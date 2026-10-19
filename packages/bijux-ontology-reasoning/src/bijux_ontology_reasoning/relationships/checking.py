"""Bounded actual named relationship enumeration over one qualified OWL closure."""

import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import cast

from bijux_ontology._json import object_value
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.patches.planning import capture_items
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, Report

from ..axioms.types import DataProperty, NamedEntity, ObjectProperty
from ..backend.bundles import qualify_backend
from ..backend.execution import (
    invoke_worker,
    qualify_capability,
    result_counter,
    result_text,
)
from ..backend.qualification import (
    _boolean,
    _records,
    qualify_reasoning_result,
    reasoning_header,
)
from ..backend.rdf_sources import stage_documents
from ..backend.types import LoaderQualification, OwlProfileBackend, ProfileDocument
from ..documents.preparation import stage_native_documents
from ..documents.types import OwlDocument
from .types import (
    InferredAnswer,
    InferredContracts,
    InferredQuery,
    RelationshipGroup,
    RelationshipMember,
    RelationshipValue,
)


def _text(value: object, budget: Budget) -> str:
    """Bound ordinary input IRIs before validation, comparison or hashing."""
    if type(value) is not str:
        raise InputError("Relationship query IRIs require plain text")
    bound_text(value, budget)
    return str(absolute_iri(value))


def _query(query: InferredQuery, budget: Budget) -> dict[str, object] | None:
    """Encode the maintained finite profile and preserve unsupported work."""
    if type(query) is not InferredQuery:
        raise InputError("Relationship requests require ordinary InferredQuery records")
    budget.consume("states", 3)
    budget.consume("bytes", 384)
    relation = cast(object, query.relation)
    if type(relation) is not str:
        raise InputError("Relationship query names require plain text")
    bound_text(relation, budget)
    if relation not in (
        "super",
        "sub",
        "equivalent",
        "inverse",
        "domain",
        "range",
    ):
        raise InputError("Choose a supported relationship query name")
    hierarchical = query.relation in ("super", "sub", "domain", "range")
    if (hierarchical and type(query.direct) is not bool) or (
        not hierarchical and query.direct is not None
    ):
        raise InputError(
            "Select explicit direct/indirect depth only for hierarchy queries"
        )
    value: dict[str, object]
    focus = query.focus
    if type(focus) is NamedEntity:
        role = cast(object, focus.kind)
        if type(role) is not str:
            raise InputError("Named query roles require plain text")
        bound_text(role, budget)
        iri = _text(focus.iri, budget)
        if focus.kind != "class":
            return None
        value = {"kind": "class", "iri": iri}
        if query.relation not in ("super", "sub", "equivalent"):
            return None
    elif type(focus) is ObjectProperty:
        if type(focus.inverse) is not bool:
            raise InputError("Object property inverse selection requires a boolean")
        value = {
            "kind": "object_property",
            "iri": _text(focus.iri, budget),
            "inverse": focus.inverse,
        }
    elif type(focus) is DataProperty:
        value = {"kind": "data_property", "iri": _text(focus.iri, budget)}
        if query.relation in ("range", "inverse"):
            return None
    else:
        raise InputError("Relationship focus requires an existing typed class/property")
    return {"focus": value, "relation": query.relation, "direct": query.direct}


def _value(raw: object, budget: Budget) -> RelationshipValue:
    """Retain exact named roles and inverse expressions rather than rendered strings."""
    item = object_value(raw, location="native relationship value")
    kind = result_text(item.get("kind"), budget)
    iri = result_text(item.get("iri"), budget)
    assert iri is not None
    absolute_iri(iri)
    budget.consume("bytes", 128)
    budget.consume("states")
    if kind == "class":
        return NamedEntity("class", iri)
    if kind == "object_property":
        return ObjectProperty(iri, _boolean(item.get("inverse")))
    if kind == "data_property":
        return DataProperty(iri)
    raise ResourceError("Native relationship member has an unknown role")


def _groups(
    raw: object,
    query: InferredQuery,
    documents: tuple[OwlDocument | ProfileDocument, ...],
    budget: Budget,
) -> tuple[RelationshipGroup, ...]:
    """Adopt partial native equivalence nodes and exact asserted ownership."""
    groups = []
    seen = set()
    class_results = type(query.focus) is NamedEntity or query.relation in (
        "domain",
        "range",
    )
    for raw_group in _records(raw, budget):
        item = object_value(raw_group, location="native relationship equivalence node")
        budget.consume("states", 2)
        budget.consume("bytes", 192)
        top, bottom, complete = (
            _boolean(item.get(name)) for name in ("top", "bottom", "complete")
        )
        members = []
        for raw_member in _records(item.get("members"), budget):
            member = object_value(raw_member, location="native relationship member")
            value = _value(member.get("value"), budget)
            if (
                (class_results and type(value) is not NamedEntity)
                or (not class_results and type(value) is not type(query.focus))
                or value in seen
            ):
                raise ResourceError(
                    "Native relationship node role or uniqueness is invalid"
                )
            budget.consume("states", 3)
            budget.consume("bytes", 192)
            seen.add(value)
            owners = []
            owned = set()
            for raw_owner in _records(member.get("asserted_in"), budget):
                index = result_counter(raw_owner, budget, len(documents) - 1)
                if index in owned:
                    raise ResourceError("Duplicate asserted relationship owner")
                budget.consume("bytes", 32)
                budget.consume("states")
                owned.add(index)
                owners.append(documents[index])
            members.append(RelationshipMember(value, tuple(owners)))
        if complete and not members:
            raise ResourceError("Native complete equivalence node cannot be empty")
        groups.append(RelationshipGroup(tuple(members), top, bottom, complete))
    return tuple(groups)


def query_inferred_contracts(
    backend: OwlProfileBackend,
    source: OwlDocument | PreparedSource,
    queries: Sequence[InferredQuery],
    *,
    imports: Mapping[str, OwlDocument | RdfSource | Payload] | None = None,
    max_results: int = 10_000,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> InferredContracts:
    """Enumerate supported native named relations under exact declared query depth.

    Source qualification and consistency precede every answer. Retained groups
    carry equivalence membership and direct structural assertion owners; absence
    of an assertion does not prove a negation. Result caps and host exhaustion
    retain completed prefixes while required enumeration remains incomplete.
    No graph is materialized or mutated. Class satisfiability is independent of
    whether its relationship enumeration completed correctly.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    requests: tuple[InferredQuery, ...] = ()
    requested_count = 0
    answers: list[InferredAnswer | None] = []
    children: list[Report] = []
    originals: tuple[OwlDocument | ProfileDocument, ...] = ()
    loaders: tuple[LoaderQualification, ...] = ()
    consistent = raw_consistent = in_profile = None
    native_error = None
    reasoner = capability = None
    consistency_report = Report("owl.consistency", requested=1, complete=False)
    finished = False
    try:
        if type(max_results) is not int or max_results <= 0:
            raise InputError("max_results requires a positive finite integer")
        selected = capture_items(queries, InferredQuery, operation)
        requested_count = len(selected)
        if not selected:
            raise InputError("No inferred relationship queries were supplied")
        operation.consume("states", len(selected) * 4)
        operation.consume("bytes", 512 * len(selected))
        wire = [_query(query, operation) for query in selected]
        # No raw request metadata enters the result before every scalar is paid.
        requests = selected
        answers = [None] * len(requests)
        qualify_backend(backend, operation)
        backend.artifacts.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="relationships-", dir=backend.artifacts) as job:
            directory = Path(job)
            if isinstance(source, OwlDocument):
                originals, request = stage_native_documents(
                    source,
                    cast(Mapping[str, OwlDocument] | None, imports),
                    directory,
                    operation,
                )
                kind = "native"
            elif isinstance(source, PreparedSource):
                originals, request, _ = stage_documents(
                    source,
                    cast(Mapping[str, RdfSource | Payload] | None, imports),
                    directory,
                    operation,
                    issues,
                    children,
                )
                kind = "rdf"
            else:
                raise InputError(
                    "Inferred contracts require an owned RDF/native source"
                )
            maximum = min(operation.remaining("issues"), operation.limits.max_states)
            if not maximum or not operation.remaining("states"):
                raise LimitExceeded("Inferred contracts have no result allowance")
            request.update(
                operation="relationships",
                input_kind=kind,
                queries=wire,
                max_results=min(max_results, operation.remaining("states")),
                max_nodes=operation.remaining("states"),
                max_issues=maximum,
                max_bytes=operation.limits.max_bytes,
                max_resources=operation.limits.max_resources,
                task_timeout_ms=max(
                    1,
                    min(2**31 - 1, int((operation.deadline - time.monotonic()) * 1000)),
                ),
            )
            data = invoke_worker(backend, request, directory, operation, heap_mb)
            capability = qualify_capability(data, backend, operation)
            children.append(capability.report)
            header = reasoning_header(data, operation)
            raw_consistent, in_profile, native_error = (
                header.raw_consistent,
                header.in_profile,
                header.native_error,
            )
            receipt = qualify_reasoning_result(
                data,
                originals,
                kind,
                operation,
                issues,
                header=header,
                scope="relationships",
            )
            raw_consistent, in_profile, native_error = (
                receipt.raw_consistent,
                receipt.in_profile,
                receipt.native_error,
            )
            loaders, reasoner = receipt.loaders, receipt.reasoner
            if not receipt.qualified:
                if data["status"] == "exhausted":
                    raise LimitExceeded(
                        "DL native input qualification did not complete"
                    )
                raise ResourceError("The selected DL reasoning scope is unqualified")
            if raw_consistent is not None:
                consistent = raw_consistent
                operation.consume("bytes", 256)
                operation.consume("states", 2)
                finding = (
                    None
                    if consistent
                    else Issue(
                        "owl.inconsistent", "The selected ontology is inconsistent"
                    )
                )
                if finding is not None:
                    issues.append(finding)
                consistency_report = Report(
                    "owl.consistency",
                    1,
                    () if finding is None else (finding,),
                    requested=1,
                )
                children.append(consistency_report)
            if consistent is not True:
                issues.append(
                    Issue(
                        "owl.relationship_consistency_unverified",
                        "An ordinary inferred view requires a consistent ontology",
                        kind="unverified",
                    )
                )
            else:
                seen = set()
                for raw in _records(data.get("answers"), operation):
                    item = object_value(
                        raw, location="native relationship query answer"
                    )
                    index = result_counter(
                        item.get("index"), operation, len(requests) - 1
                    )
                    if index in seen:
                        raise ResourceError("Duplicate native relationship answer")
                    operation.consume("states", 4)
                    operation.consume("bytes", 512)
                    seen.add(index)
                    complete = _boolean(item.get("complete"))
                    error = result_text(item.get("error"), operation, optional=True)
                    satisfiable = (
                        None
                        if item.get("satisfiable") is None
                        else _boolean(item["satisfiable"])
                    )
                    if (
                        type(requests[index].focus) is not NamedEntity
                        and satisfiable is not None
                    ) or (wire[index] is None and complete):
                        raise ResourceError(
                            "Native query answer contradicts its qualified role"
                        )
                    groups = _groups(
                        item.get("groups"), requests[index], originals, operation
                    )
                    if complete and (
                        error is not None or any(not group.complete for group in groups)
                    ):
                        raise ResourceError(
                            "Native relationship completeness contradicts evidence"
                        )
                    local = []
                    if not complete:
                        issue = Issue(
                            "owl.relationship_query_unverified",
                            "Relationship enumeration is incomplete or unsupported",
                            str(index),
                            actual=error,
                            kind="unverified",
                        )
                        issues.append(issue)
                        local.append(issue)
                    report = Report(
                        "owl.inferred_query",
                        int(complete),
                        tuple(local),
                        requested=1,
                        complete=complete,
                    )
                    answers[index] = InferredAnswer(
                        requests[index], groups, satisfiable, error, report
                    )
                    children.append(report)
                if data["status"] == "exhausted":
                    raise LimitExceeded(
                        "Native relationship enumeration exhausted its bound"
                    )
                if len(seen) != len(requests):
                    raise ResourceError("Native relationship inventory is incomplete")
                finished = all(
                    answer is not None and answer.report.complete for answer in answers
                )
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.inferred_relationships")
    except (InputError, OSError) as error:
        try:
            issues.append(
                Issue(
                    "owl.relationships_unverified"
                    if isinstance(error, ResourceError | OSError)
                    else "owl.relationships_input_error",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError | OSError)
                    else "input_error",
                )
            )
        except LimitExceeded as stopped:
            issues.exhausted(str(stopped), location="owl.inferred_relationships")
    pending = tuple(issue for issue in issues if issue.kind != "finding")
    completed = tuple(
        answer
        if answer is not None
        else InferredAnswer(
            requests[index],
            (),
            None,
            native_error,
            Report("owl.inferred_query", issues=pending, requested=1, complete=False),
        )
        for index, answer in enumerate(answers)
    )
    report = Report(
        "owl.inferred_relationships",
        int(consistent is not None)
        + sum(answer.report.complete for answer in completed),
        tuple(issues),
        requested=1 + requested_count,
        complete=finished,
        children=tuple(children),
        regime="owl2-dl-hermit-named-inferred-relationships",
    )
    return InferredContracts(
        completed,
        consistent,
        raw_consistent,
        in_profile,
        native_error,
        originals,
        loaders,
        reasoner,
        capability,
        consistency_report,
        report,
    )
