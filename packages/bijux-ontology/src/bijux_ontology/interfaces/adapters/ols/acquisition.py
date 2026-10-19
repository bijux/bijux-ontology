"""Opt-in OLS acquisition through the existing bounded HTTP resource boundary."""

import threading
import time
from urllib.parse import quote, urlencode

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.workflows.projects.acquisition.http import HttpAcquisition
from bijux_ontology.workflows.projects.acquisition.types import AcquisitionPolicy

from .reading import _base, _decode, _query, _reserve, _url, prepare_ols_snapshot
from .types import OlsPage, OlsQuery, OlsSnapshot


def acquire_ols_snapshot(
    base_url: str,
    *,
    query: OlsQuery,
    policy: AcquisitionPolicy,
    api_version: str = "v2",
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> OlsSnapshot:
    """Acquire a finite query and fresh data identity observations around each page.

    Ordinary snapshot preparation/lookup stays offline. Origins, redirects,
    transfer bounds and credentials policy are owned by HttpAcquisition. Each
    HTTP hop runs in an owned process under the inherited absolute deadline;
    cancellation interrupts blocked network I/O and reaps that owned process.
    The deployed software release remains unknown unless independently supplied
    in captured page receipts; it is never substituted for ontology data identity.
    """
    if type(policy) is not AcquisitionPolicy:
        raise InputError("OLS acquisition requires an explicit AcquisitionPolicy")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    pages: list[OlsPage] = []
    selected: OlsQuery | None = None
    base = ""
    snapshot: OlsSnapshot | None = None
    try:
        if api_version != "v2":
            raise ResourceError(
                "Only the explicitly selected OLS API v2 profile is supported"
            )
        selected = _query(query, operation)
        base = _base(base_url, operation)
        client = HttpAcquisition(policy, operation, accept="application/json")
        metadata_url = (
            base
            + "/ontologies/"
            + quote(selected.ontology, safe="")
            + "?"
            + urlencode({"lang": selected.language})
        )
        _reserve(operation)
        page = 0
        while True:
            url = _url(base, selected, page, operation)
            before = client.get(metadata_url, fresh=True)
            response = client.get(url)
            after = client.get(metadata_url, fresh=True)
            if any(
                r.resource.media_type not in ("application/json", "application/ld+json")
                for r in (before, response, after)
            ):
                raise ResourceError("OLS responses require JSON media types")
            _reserve(operation)
            pages.append(OlsPage(url, response.payload, before.payload, after.payload))
            data = _decode(response.payload, operation)
            total_pages = data.get("totalPages")
            if type(total_pages) is not int or total_pages < 0:
                raise InputError(
                    "OLS response lacks bounded native pagination metadata"
                )
            page += 1
            if page >= total_pages:
                break
        # Reserve final record/report references before nested preparation. Both
        # host acquisition and offline projection spend the same fixed allowance.
        _reserve(operation)
        snapshot = prepare_ols_snapshot(
            pages,
            query=selected,
            base_url=base,
            api_version=api_version,
            budget=operation,
            timeout=max(0.000001, operation.deadline - time.monotonic()),
            cancel=cancel,
        )
        issues.retain_completed(snapshot.report)
        retain_report(snapshot.report, operation, issues)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except ResourceError as error:
        try:
            issues.append(
                Issue("ols.acquisition_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as limit:
            issues.exhausted(str(limit))
    except InputError as error:
        try:
            issues.append(Issue("ols.input_error", str(error), kind="input_error"))
        except LimitExceeded as limit:
            issues.exhausted(str(limit))
    if snapshot is not None:
        return OlsSnapshot._owned(
            snapshot.query,
            snapshot.base_url,
            snapshot.data_version,
            snapshot.loaded,
            snapshot.pages,
            snapshot.entities,
            snapshot.vocabulary,
            snapshot.complete and not issues,
            Report(
                "ols.acquisition",
                1,
                tuple(issues),
                requested=1,
                regime=snapshot.report.regime,
                children=(snapshot.report,),
            ),
        )
    return OlsSnapshot._owned(
        selected,
        base,
        None,
        None,
        tuple(pages),
        (),
        None,
        False,
        Report(
            "ols.acquisition",
            0,
            tuple(issues),
            requested=1,
            regime="ols-v2-selected-service-projection",
        ),
    )
