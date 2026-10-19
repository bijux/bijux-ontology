"""Checked V2 entity pages and precise offline query completion boundaries."""

import json
import threading
from collections.abc import Sequence
from dataclasses import replace
from typing import Literal as Choice
from typing import cast
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF

from bijux_ontology._json import _measure, decode_json, object_value
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import bound_node, bound_text
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.inspection import VocabularyView, inspect_vocabulary
from bijux_ontology.vocabulary.loading import load_vocabulary
from bijux_ontology.workflows.projects.acquisition.types import _http_url

from .types import (
    OlsAnnotation,
    OlsEntity,
    OlsLookup,
    OlsPage,
    OlsQuery,
    OlsSnapshot,
    Role,
)

_ROLES = {
    "class": OWL.Class,
    "property": RDF.Property,
    "objectProperty": OWL.ObjectProperty,
    "dataProperty": OWL.DatatypeProperty,
    "annotationProperty": OWL.AnnotationProperty,
    "individual": OWL.NamedIndividual,
}


def _reserve(budget: Budget, count: int = 1) -> None:
    """Pay reference/container ownership before constructing retained records."""
    budget.consume("work", count + 1)
    budget.consume("states", count)
    budget.consume("bytes", 128 + 64 * count)


def _base(value: str, budget: Budget) -> str:
    """Select only the documented V2 root without credentials or hidden query."""
    _http_url(value, budget)
    parsed = urlsplit(value)
    if parsed.query or not parsed.path.endswith("/api/v2"):
        raise InputError("OLS base URL must end with the selected /api/v2 route")
    return value


def _query(value: OlsQuery, budget: Budget) -> OlsQuery:
    """Capture the actual finite selection before hashing or URL construction."""
    if type(value) is not OlsQuery:
        raise InputError("Select an ordinary typed OLS query")
    bound_text(value.ontology, budget)
    bound_text(value.language, budget)
    roles = capture_items(value.roles, str, budget)
    predicates = capture_items(value.annotation_predicates, URIRef, budget)
    for role in roles:
        bound_text(role, budget)
    for predicate in predicates:
        bound_node(predicate, budget)
        absolute_iri(str(predicate))
    if len(set(roles)) != len(roles) or len(set(predicates)) != len(predicates):
        raise InputError("OLS role and annotation selections must be distinct")
    for iri in (value.iri, value.hierarchical_ancestor):
        if iri is not None:
            bound_node(iri, budget)
            absolute_iri(str(iri))
    # Ontology IDs are one exact controller path segment; commas/OR filters and
    # path metacharacters are outside this finite selection profile.
    if not value.ontology.isascii() or any(
        not (c.isalnum() or c in "_-") for c in value.ontology
    ):
        raise InputError("OLS ontology IDs require a single explicit identifier")
    return replace(
        value, roles=cast(tuple[Role, ...], roles), annotation_predicates=predicates
    )


def _url(base: str, query: OlsQuery, page: int, budget: Budget) -> str:
    """Build only finite native filter parameters, never a search expression."""
    values = {
        "type": ",".join(query.roles),
        "size": str(query.page_size),
        "page": str(page),
        "includeObsoleteEntities": "true",
        "lang": query.language,
    }
    if query.iri is not None:
        values["iri"] = str(query.iri)
    if query.hierarchical_ancestor is not None:
        values["hierarchicalAncestor"] = str(query.hierarchical_ancestor)
    cost = (
        len(base)
        + len(query.ontology)
        + sum(len(k) + len(v) for k, v in values.items())
    )
    budget.consume("work", cost + 1)
    budget.consume("bytes", 128 + 12 * cost)
    result = (
        base
        + "/ontologies/"
        + quote(query.ontology, safe="")
        + "/entities?"
        + urlencode(values)
    )
    bound_text(result, budget)
    return result


def _decode(raw: bytes, budget: Budget) -> dict[str, object]:
    """Prepay native duplicate-aware JSON decoder containers and path workspace."""
    if type(raw) is not bytes:
        raise InputError("OLS captures require immutable bytes")
    if len(raw) > budget.limits.max_bytes:
        raise LimitExceeded("OLS response exceeds max_bytes")
    budget.consume("work", len(raw) + 1)
    budget.consume("states", len(raw))
    budget.consume("bytes", (128 + 4 * budget.limits.max_depth) * len(raw))
    result = object_value(
        decode_json(
            raw, max_bytes=budget.limits.max_bytes, max_depth=budget.limits.max_depth
        ),
        location="OLS",
    )
    _measure(result, budget)
    return result


def _same_request(actual: str, expected: str, budget: Budget) -> bool:
    """Query ordering is irrelevant; duplicate/hidden filters remain invalid."""
    _http_url(actual, budget)
    left, right = urlsplit(actual), urlsplit(expected)
    budget.consume("work", len(actual) + len(expected) + 1)
    _reserve(budget, 20)
    if left.query.count("&") >= 10:
        return False
    pairs = parse_qsl(left.query, keep_blank_values=True, max_num_fields=10)
    requested = dict(pairs)
    if len(requested) != len(pairs):
        return False
    requested.setdefault("lang", "en")
    return (left.scheme, left.netloc, left.path) == (
        right.scheme,
        right.netloc,
        right.path,
    ) and requested == dict(parse_qsl(right.query, max_num_fields=10))


def _dump(value: object, budget: Budget) -> bytes:
    """Retain exact qualified JSON structure with bounded serializer workspace."""
    amount = 128 + 12 * _measure(value, budget)
    budget.consume("bytes", 4 * amount + 256)
    budget.consume("work", amount + 1)
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _strings(value: object, budget: Budget) -> tuple[str, ...]:
    """Read a native string or string array without inventing RDF term kinds."""
    raw = [value] if isinstance(value, str) else value
    if type(raw) is not list or any(type(v) is not str for v in raw):
        raise InputError("Expected native OLS string values")
    _reserve(budget, len(raw))
    for item in raw:
        bound_text(item, budget)
    return tuple(raw)


def _version(
    data: dict[str, object], ontology: str, budget: Budget
) -> tuple[str | None, str | None]:
    """Ontology data identity is independent of the deployed software release."""
    if data.get("ontologyId") != ontology:
        raise InputError("OLS ontology metadata does not match the requested ontology")
    version, loaded = data.get(str(OWL.versionIRI)), data.get("loaded")
    if type(version) is not str or not version or type(loaded) is not str or not loaded:
        return None, None
    bound_text(version, budget)
    absolute_iri(version)
    bound_text(loaded, budget)
    return version, loaded


def _entity(data: object, query: OlsQuery, budget: Budget) -> OlsEntity:
    """Keep native role/status distinctions and original qualified attachments."""
    record = object_value(data, location="OLS entity")
    iri = record.get("iri")
    if type(iri) is not str or record.get("ontologyId") != query.ontology:
        raise InputError("OLS entity lacks its exact ontology and IRI identity")
    bound_text(iri, budget)
    absolute_iri(iri)
    roles = _strings(record.get("type"), budget)
    if not set(roles).intersection(query.roles):
        raise InputError("OLS entity does not have a requested native role")
    statuses = (
        record.get("imported"),
        record.get("isDefiningOntology"),
        record.get("isObsolete"),
    )
    if any(type(s) is not bool for s in statuses):
        raise InputError("OLS entity status fields require separate native booleans")
    annotations = []
    for predicate in query.annotation_predicates:
        budget.consume("work")
        if str(predicate) not in record:
            continue
        value = record[str(predicate)]
        values = value if type(value) is list else [value]
        _reserve(budget, len(values))
        for part in values:
            budget.consume("work")
            if type(part) is str:
                lexical = part
            elif (
                type(part) is dict
                and part.get("type") == ["reification"]
                and type(part.get("value")) is str
            ):
                lexical = cast(str, part["value"])
            else:
                raise ResourceError(
                    "Selected OLS annotation is outside the lexical projection profile"
                )
            bound_text(lexical, budget)
            raw = _dump(part, budget)
            _reserve(budget)
            annotations.append(OlsAnnotation(predicate, lexical, raw))
    ancestors = _strings(record.get("hierarchicalAncestor", []), budget)
    for ancestor in ancestors:
        absolute_iri(ancestor)
    raw_record = _dump(record, budget)
    _reserve(budget, len(ancestors) + 1)
    return OlsEntity(
        query.ontology,
        URIRef(iri),
        roles,
        cast(bool, statuses[0]),
        cast(bool, statuses[1]),
        cast(bool, statuses[2]),
        tuple(annotations),
        tuple(URIRef(a) for a in ancestors),
        raw_record,
    )


def prepare_ols_snapshot(
    pages: Sequence[OlsPage],
    *,
    query: OlsQuery,
    base_url: str,
    api_version: str = "v2",
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> OlsSnapshot:
    """Prepare an offline lexical projection of one explicit native entity query.

    Completeness means all observed controller pages were captured with consistent
    totals and unchanged versionIRI/loaded metadata around every page. It is an
    optimistic service observation, not an atomic database transaction. Generic
    ancestor fields remain separate; no subclass triples are manufactured. Raw
    response bytes retain all fields outside the selected lexical projection.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    selected: OlsQuery | None = None
    source: VocabularyView | None = None
    owned_pages: list[OlsPage] = []
    entities: list[OlsEntity] = []
    identity: tuple[str | None, str | None] = (None, None)
    expected_pages: int | None = None
    expected_total: int | None = None
    seen: set[URIRef] = set()
    checked = 0
    requested = 1
    complete = False
    chosen_base = ""
    try:
        if api_version != "v2":
            raise ResourceError(
                "Only the explicitly selected OLS API v2 profile is supported"
            )
        selected = _query(query, operation)
        chosen_base = _base(base_url, operation)
        inventory = capture_items(pages, OlsPage, operation)
        requested = max(1, len(inventory))
        count = 0
        for index, page in enumerate(inventory):
            operation.consume("work")
            if type(page) is not OlsPage:
                raise InputError("Use ordinary immutable OLS page receipts")
            bound_text(page.url, operation)
            if not _same_request(
                page.url, _url(chosen_base, selected, index, operation), operation
            ):
                raise InputError(
                    "Captured OLS URL differs from the selected query/page"
                )
            if page.software_release is not None:
                bound_text(page.software_release, operation)
            before = _version(
                _decode(page.ontology_before, operation), selected.ontology, operation
            )
            data = _decode(page.content, operation)
            after = _version(
                _decode(page.ontology_after, operation), selected.ontology, operation
            )
            _reserve(operation)
            owned_pages.append(replace(page))
            if before != after or index and before != identity:
                issues.append(
                    Issue(
                        "ols.data_changed",
                        "Ontology data identity changed across captured pages",
                        page.url,
                        kind="unverified",
                    )
                )
            if not index:
                identity = before
            if None in before or None in after:
                issues.append(
                    Issue(
                        "ols.data_unversioned",
                        "Negative answers require an ontology data versionIRI "
                        "and loaded observation",
                        page.url,
                        kind="unverified",
                    )
                )
            current, total_pages, total, num = (
                data.get(k)
                for k in ("page", "totalPages", "totalElements", "numElements")
            )
            if any(
                type(v) is not int or v < 0 for v in (current, total_pages, total, num)
            ):
                raise InputError(
                    "OLS pages require exact nonnegative controller counters"
                )
            values = data.get("elements")
            if (
                type(values) is not list
                or num != len(values)
                or current != index
                or num > selected.page_size
            ):
                raise InputError("OLS page counters do not match captured contents")
            if expected_pages is None:
                expected_pages, expected_total = (
                    cast(int, total_pages),
                    cast(int, total),
                )
                requested = max(requested, expected_pages)
                if (
                    expected_pages
                    != (expected_total + selected.page_size - 1) // selected.page_size
                ):
                    issues.append(
                        Issue(
                            "ols.page_incomplete",
                            "OLS total/page-size metadata is inconsistent",
                            page.url,
                            kind="unverified",
                        )
                    )
            elif total_pages != expected_pages or total != expected_total:
                issues.append(
                    Issue(
                        "ols.page_incomplete",
                        "OLS totals changed between pages",
                        page.url,
                        kind="unverified",
                    )
                )
            expected_num = min(
                selected.page_size,
                max(0, cast(int, total) - index * selected.page_size),
            )
            if num != expected_num:
                issues.append(
                    Issue(
                        "ols.page_incomplete",
                        "A required controller page was truncated",
                        page.url,
                        kind="unverified",
                    )
                )
            count += len(values)
            for value in values:
                entity = _entity(value, selected, operation)
                key = entity.iri
                operation.consume("work", len(entity.roles) + 1)
                _reserve(operation)
                if key in seen:
                    issues.append(
                        Issue(
                            "ols.page_incomplete",
                            "Duplicate entity identity appears across query pages",
                            page.url,
                            kind="unverified",
                        )
                    )
                    continue
                seen.add(key)
                if selected.iri is not None and entity.iri != selected.iri:
                    # Native filters are case-insensitive; a lexical lookalike
                    # cannot discharge an exact requested RDF IRI.
                    continue
                if (
                    selected.hierarchical_ancestor is not None
                    and selected.hierarchical_ancestor
                    not in entity.hierarchical_ancestors
                ):
                    issues.append(
                        Issue(
                            "ols.hierarchy_unverified",
                            "Exact selected service ancestor is not established",
                            page.url,
                            kind="unverified",
                        )
                    )
                    continue
                if (
                    not selected.include_imported
                    and entity.imported
                    or not selected.include_obsolete
                    and entity.obsolete
                    or selected.defining_only
                    and not entity.defining
                ):
                    continue
                _reserve(operation)
                entities.append(entity)
            checked += 1
        page_count = max(1, expected_pages or 0)
        if len(inventory) != page_count or count != expected_total:
            issues.append(
                Issue(
                    "ols.page_incomplete",
                    "Not every required query page/element was captured",
                    kind="unverified",
                )
            )
        complete = bool(inventory) and not issues
        if not entities:
            issues.append(
                Issue(
                    "ols.no_selected_entities",
                    "No selected exact entities were established; "
                    "no empty vocabulary is published",
                    kind="unverified",
                )
            )
        elif complete:
            graph = Graph()
            for entity in entities:
                for role in entity.roles:
                    operation.consume("work")
                    if role in _ROLES:
                        _reserve(operation)
                        operation.consume("triples")
                        graph.add((entity.iri, RDF.type, _ROLES[role]))
                for annotation in entity.annotations:
                    bound_text(annotation.lexical, operation)
                    operation.consume("bytes", 128 + 8 * len(annotation.lexical))
                    _reserve(operation)
                    operation.consume("triples")
                    graph.add(
                        (
                            entity.iri,
                            annotation.predicate,
                            Literal(annotation.lexical, normalize=False),
                        )
                    )
            vocabulary = load_vocabulary(graph, budget=operation)
            source = inspect_vocabulary(vocabulary, budget=operation)
    except LimitExceeded as error:
        complete = False
        source = None
        issues.exhausted(str(error))
    except ResourceError as error:
        complete = False
        try:
            issues.append(Issue("ols.unverified", str(error), kind="unverified"))
        except LimitExceeded as limit:
            issues.exhausted(str(limit))
    except InputError as error:
        complete = False
        try:
            issues.append(Issue("ols.input_error", str(error), kind="input_error"))
        except LimitExceeded as limit:
            issues.exhausted(str(limit))
    report = Report(
        "ols.snapshot",
        checked,
        tuple(issues),
        requested=requested,
        regime="ols-v2-selected-service-projection",
    )
    return OlsSnapshot._owned(
        selected,
        chosen_base,
        identity[0],
        identity[1],
        tuple(owned_pages),
        tuple(entities),
        source,
        complete,
        report,
    )


def lookup_ols(
    snapshot: OlsSnapshot,
    iri: URIRef,
    *,
    role: Role,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> OlsLookup:
    """Resolve one exact service membership question within its captured scope."""
    if (
        type(snapshot) is not OlsSnapshot
        or type(iri) is not URIRef
        or role not in _ROLES
    ):
        raise InputError(
            "OLS lookup needs a checked snapshot, exact IRI and native role"
        )
    operation = Operation(limits, budget, 30, None)
    issues = IssueCollector(operation)
    found: list[OlsEntity] = []
    status: Choice["present", "absent", "unknown"] = "unknown"
    try:
        bound_node(iri, operation)
        query = snapshot.query
        for entity in snapshot.entities:
            operation.consume("work")
            bound_node(entity.iri, operation)
            if entity.iri == iri and role in entity.roles:
                _reserve(operation)
                found.append(entity)
        if found:
            status = "present"
        elif (
            snapshot.complete
            and query is not None
            and role in query.roles
            and query.hierarchical_ancestor is None
            and (query.iri is None or query.iri == iri)
        ):
            status = "absent"
        if status == "unknown":
            issues.append(
                Issue(
                    "ols.membership_unverified",
                    "The exact membership question is outside complete captured scope",
                    str(iri),
                    kind="unverified",
                )
            )
        retain_report(snapshot.report, operation, issues)
    except LimitExceeded as error:
        status = "unknown"
        issues.exhausted(str(error))
    report = Report(
        "ols.membership",
        1,
        tuple(issues),
        requested=1,
        regime="ols-v2-selected-service-projection",
    )
    return OlsLookup(tuple(found), status, report)
