"""Actual selected-profile execution using the shared owned native boundary."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import cast

from bijux_ontology._json import object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource

from ..backend.owlapi.bundles import PROFILES, qualify_backend
from ..backend.owlapi.execution import invoke_worker as _invoke
from ..backend.owlapi.execution import qualify_capability as _capability
from ..backend.owlapi.protocol import result_counter as _integer
from ..backend.owlapi.protocol import result_text as _text
from ..backend.types import (
    LoaderQualification,
    OwlProfile,
    OwlProfileBackend,
    ProfileDocument,
)
from ..documents.sources.rdf import stage_documents
from .types import ProfileCheck, ProfileViolation


def check_profile(
    backend: OwlProfileBackend,
    source: PreparedSource,
    *,
    profile: OwlProfile,
    imports: Mapping[str, RdfSource | Payload] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> ProfileCheck:
    """Check actual DL/EL/QL/RL membership through one pinned owned OWLAPI worker.

    Exact physical document scope and offline closure come from core preparation.
    Loader metadata gates input interpretation. No consistency, satisfiability,
    exact OWL/RDF roundtrip, hard RSS or native instruction-count claim is made.
    Heap and wall-clock limits apply to the disposable JVM; host work shares Budget.
    """
    if not isinstance(profile, str) or profile not in PROFILES:
        raise InputError("Choose one supported OWL2 profile")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    documents: tuple[ProfileDocument, ...] = ()
    loaders: list[LoaderQualification] = []
    violations: list[ProfileViolation] = []
    capability = None
    raw_answer = answer = None
    complete = False
    try:
        qualify_backend(backend, operation)
        backend.artifacts.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="profile-", dir=backend.artifacts) as job:
            documents, staged, _ = stage_documents(
                source, imports, Path(job), operation, issues, children
            )
            request = dict(
                staged,
                operation="profile",
                profile=profile,
                max_bytes=operation.limits.max_bytes,
                max_issues=min(
                    operation.remaining("issues"), operation.limits.max_states
                ),
                max_resources=operation.limits.max_resources,
            )
            if not request["max_issues"]:
                raise LimitExceeded("OWL profile has no remaining diagnostic allowance")
            data = _invoke(backend, request, Path(job), operation, heap_mb)
            operation.consume("states")
            capability = _capability(data, backend, operation)
            children.append(capability.report)
            if (
                data.get("profile") != profile
                or type(data.get("raw_in_profile")) is not bool
            ):
                raise ResourceError(
                    "OWL worker omitted its exact selected profile answer"
                )
            raw_answer = data["raw_in_profile"]
            actual_loaders = data.get("loaders")
            actual_violations = data.get("violations")
            if not isinstance(actual_loaders, list) or len(actual_loaders) != len(
                documents
            ):
                raise ResourceError("OWL worker omitted required loaded documents")
            if (
                not isinstance(actual_violations, list)
                or len(actual_violations) > operation.limits.max_issues
            ):
                raise ResourceError("OWL worker exceeded its violation inventory")
            operation.consume("states", len(actual_loaders) + len(actual_violations))
            seen = set()
            for record in actual_loaders:
                item = object_value(record, location="OWL worker record")
                index = _integer(item.get("document"), operation, len(documents) - 1)
                if index in seen or type(item.get("present")) is not bool:
                    raise ResourceError("OWL loader ownership is ambiguous")
                seen.add(index)
                present = cast(bool, item["present"])
                counts = tuple(
                    _integer(item.get(name), operation, operation.limits.max_triples)
                    if present
                    else None
                    for name in ("unparsed", "guessed", "triples")
                )
                header = _text(item.get("header"), operation, optional=not present)
                unparsed, guessed, triples = counts
                loader = LoaderQualification(
                    documents[index], present, unparsed, guessed, triples, header
                )
                loaders.append(loader)
                if not loader.qualified:
                    issues.append(
                        Issue(
                            "owl.loader_unverified",
                            "OWL parser metadata does not qualify the original RDF",
                            loader.document.document_iri or loader.document.identifier,
                            kind="unverified",
                        )
                    )
            qualified = all(loader.qualified for loader in loaders)
            if (
                raw_answer
                and actual_violations
                or not raw_answer
                and not actual_violations
                and data["status"] == "completed"
            ):
                raise ResourceError("OWL worker boolean contradicts its violations")
            if qualified and (data["status"] == "completed" or raw_answer is False):
                answer = raw_answer
            for record in actual_violations:
                item = object_value(record, location="OWL worker record")
                index = _integer(item.get("document"), operation, len(documents) - 1)
                kind = cast(str, _text(item.get("kind"), operation))
                axiom = _text(item.get("axiom"), operation, optional=True)
                expression = _text(item.get("expression"), operation, optional=True)
                violation = ProfileViolation(kind, documents[index], axiom, expression)
                violations.append(violation)
                if qualified:
                    issues.append(
                        Issue(
                            "owl.profile_violation",
                            "The ontology violates the selected OWL2 profile",
                            violation.document.document_iri
                            or violation.document.identifier,
                            expected=profile,
                            actual=kind,
                        )
                    )
            if data["status"] == "exhausted":
                raise LimitExceeded("OWL profile violation inventory is incomplete")
            complete = qualified
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.profile")
    except (InputError, OSError):
        answer = None
        try:
            issues.append(
                Issue(
                    "owl.profile_unverified",
                    "OWL profile execution or input interpretation is unavailable",
                    kind="unverified",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location="owl.profile")
    report = Report(
        "owl.profile",
        int(complete),
        tuple(issues),
        requested=1,
        complete=complete,
        children=tuple(children),
        regime="owlapi-selected-profile",
        families=(profile,),
    )
    return ProfileCheck(
        profile,
        capability,
        answer,
        raw_answer,
        tuple(violations),
        documents,
        tuple(loaders),
        report,
    )
