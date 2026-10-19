"""Explicit bounded endpoint acquisition; ordinary RDF checking remains offline."""

from threading import Event
from urllib.parse import urlencode, urlsplit

from rdflib import BNode, Graph, Literal

from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, Report
from bijux_ontology.workflows.projects.acquisition.http import HttpAcquisition
from bijux_ontology.workflows.projects.acquisition.types import AcquisitionPolicy

from .preparation import capture_contract, capture_query, query_text, read_rows, receipt
from .types import EndpointCompleteness, EndpointExtraction, EndpointPage, EndpointQuery


def acquire_endpoint(
    endpoint: str,
    *,
    query: EndpointQuery,
    policy: AcquisitionPolicy,
    completeness: EndpointCompleteness | None = None,
    page_size: int = 1000,
    max_pages: int = 100,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> EndpointExtraction:
    """Acquire a selected default/named graph slice into a normal owned snapshot.

    The finite SELECT profile binds RDF parameters, not executable fragments.
    Complete multi-page extraction requires a declared immutable-version query
    parameter and identical per-page version/total/no-truncation receipts. HTTP
    success, short pages and row arithmetic cannot supply those guarantees.
    Blank-node labels are scoped independently per response; multi-page blank
    identity is unsupported and can never certify a joined slice. Useful captured
    prefixes remain available beside unknown completion. A socket read uses its
    real remaining timeout; cancellation is checked at bounded host checkpoints,
    without an immediate socket-interruption promise. No validation fetches occur.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    selected = None
    source = None
    graph = Graph(bind_namespaces="none")
    pages: list[EndpointPage] = []
    count = 0
    complete = consistent = False
    try:
        if type(policy) is not AcquisitionPolicy:
            raise InputError("Endpoint acquisition requires AcquisitionPolicy")
        for capacity in (page_size, max_pages):
            if type(capacity) is not int or capacity <= 0 or capacity.bit_length() > 31:
                raise InputError(
                    "Endpoint capacities require positive bounded integers"
                )
        selected = capture_query(query, operation)
        contract = capture_contract(completeness, operation)
        client = HttpAcquisition(
            policy, operation, accept="application/sparql-results+json"
        )
        endpoint = client.authorize(endpoint)
        if urlsplit(endpoint).query:
            raise InputError(
                "Endpoint URL parameters must be supplied by this operation"
            )
        total = None
        any_blank = False
        terminal = False
        for index in range(max_pages):
            operation.consume("states")
            offset = index * page_size
            parameter_size = sum(
                len(term)
                + (
                    len(term.datatype or "") + len(term.language or "")
                    if isinstance(term, Literal)
                    else 0
                )
                for term in (
                    selected.subject,
                    selected.predicate,
                    selected.object,
                    selected.graph,
                )
                if term is not None
            )
            operation.consume("bytes", 12 * parameter_size + 1024)
            text = query_text(selected, page_size, offset)
            bound_text(text, operation)
            parameters = {"query": text}
            if contract is not None:
                parameters[contract.snapshot_parameter] = contract.snapshot
            operation.consume(
                "bytes",
                12
                * (
                    len(text)
                    + len(endpoint)
                    + (
                        len(contract.snapshot) + len(contract.snapshot_parameter)
                        if contract is not None
                        else 0
                    )
                )
                + 1024,
            )
            url = endpoint + "?" + urlencode(parameters)
            response = client.get(url, fresh=True)
            if response.resource.effective_url != url:
                raise ResourceError("Endpoint redirects cannot qualify a query receipt")
            if response.resource.media_type != "application/sparql-results+json":
                raise InputError("Endpoint SELECT requires SPARQL JSON media")
            rows = read_rows(response.payload, operation)
            if len(rows) > page_size:
                raise InputError("Endpoint exceeded the requested row window")
            snapshot, page_total = None, None
            if contract is not None:
                operation.consume("work", 3 * len(response.headers) + 1)
                snapshot, page_total = receipt(response.headers, contract)
                if total is not None and page_total != total:
                    raise InputError("Endpoint total changed across snapshot pages")
                total = page_total
                if count + len(rows) > total:
                    raise InputError(
                        "Endpoint returned more rows than its total receipt"
                    )
            operation.consume("bytes", 256 + len(url))
            pages.append(
                EndpointPage(
                    url,
                    response.resource.sha256,
                    offset,
                    len(rows),
                    snapshot,
                    page_total,
                )
            )
            for triple in rows:
                operation.consume("work")
                operation.consume("states")
                if any(
                    parameter is not None and parameter != term
                    for parameter, term in zip(
                        (selected.subject, selected.predicate, selected.object),
                        triple,
                        strict=True,
                    )
                ):
                    raise InputError(
                        "Endpoint returned a row outside its bound selection"
                    )
                if triple in graph:
                    raise InputError(
                        "Endpoint repeated a distinct row across page boundaries"
                    )
                graph.add(triple)
                count += 1
                any_blank |= any(isinstance(node, BNode) for node in triple)
            if len(rows) < page_size or total is not None and count == total:
                terminal = True
                break
        # Materialization never grants acquisition completeness. A captured prefix
        # remains useful for explicitly local obligations when the server cannot
        # establish the declared whole selection or cross-page identity.
        source = prepare_source(graph, budget=operation)
        if contract is None:
            issues.append(
                Issue(
                    "endpoint.completeness_unverified",
                    "No immutable snapshot and completeness contract was supplied",
                    kind="unverified",
                )
            )
        elif any_blank and len(pages) > 1:
            issues.append(
                Issue(
                    "endpoint.blank_scope_unverified",
                    "Separate result documents cannot establish "
                    "cross-page blank identity",
                    kind="unverified",
                )
            )
        elif not terminal or count != total:
            issues.append(
                Issue(
                    "endpoint.slice_incomplete",
                    "Page capacity or short response did not cover the declared total",
                    kind="unverified",
                )
            )
        else:
            complete = consistent = True
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (InputError, ResourceError) as error:
        try:
            issues.append(
                Issue(
                    "endpoint.acquisition_unverified"
                    if isinstance(error, ResourceError)
                    else "endpoint.input_error",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded as stopped:
            issues.exhausted(str(stopped))
    return EndpointExtraction(
        source,
        selected,
        tuple(pages),
        count,
        complete,
        consistent,
        Report(
            "rdf.endpoint",
            1 if complete and consistent else 0,
            tuple(issues),
            requested=1,
            complete=complete and consistent,
            regime="declared-immutable-endpoint-triple-slice",
        ),
    )
