"""Prepare the actual optional interface over one already owned selected view."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING, cast

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.identifiers import language_tag
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.registry.types import IdentifierRegistry

from .types import OakPreparation

if TYPE_CHECKING:
    from .adapter import BijuxBasicOntology


def as_oak(
    view: VocabularyView,
    *,
    registry: IdentifierRegistry,
    language: str | None = None,
    closed_subsets: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> OakPreparation[BijuxBasicOntology]:
    """Expose a prepared view through installed OAK, without loading resources.

    The existing explicit registry controls identifiers. A default language is
    an exact tag selection; it never reinterprets untagged text. Every native
    operation either completes its finite selection or raises OakOperationError.
    Subset calls require caller-selected closed_subsets, a finite contract over
    this selected source only, with no OWL or global completeness claim.
    """
    if not isinstance(view, VocabularyView) or not isinstance(
        registry, IdentifierRegistry
    ):
        raise InputError("OAK preparation requires an owned view and explicit registry")
    if (
        limits is not None
        and not isinstance(limits, Limits)
        or budget is not None
        and not isinstance(budget, Budget)
    ):
        raise InputError("OAK preparation requires Limits and Budget objects")
    if type(closed_subsets) is not bool:
        raise InputError("OAK subset closure requires an explicit boolean")
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("OAK limits conflict with the shared budget")
    selected = (
        limits
        if limits is not None
        else budget.limits
        if budget is not None
        else view.limits
    )
    operation = budget if budget is not None else Budget(selected)
    issues = IssueCollector(operation)
    adapter = None
    checked = 0
    try:
        if language is not None:
            if type(language) is not str:
                raise InputError("OAK language selection requires exact text")
            bound_text(language, operation)
            language_tag(language)
        operation.consume("states", len(registry.bindings))
        for prefix, namespace in registry.bindings:
            bound_text(prefix, operation)
            bound_text(namespace, operation)
        operation.consume("work")
        if not (3, 10) <= sys.version_info[:2] < (3, 15):
            issues.append(
                Issue(
                    "adapter.oak_python_unsupported",
                    "Maintained OAK 0.7.4 requires Python >=3.10,<3.15; "
                    "core Bijux remains available",
                    kind="unverified",
                )
            )
        else:
            module = import_module("bijux_ontology.interfaces.adapters.oak.adapter")
            adapter = cast(
                "BijuxBasicOntology",
                module.BijuxBasicOntology._from_owned(
                    view, registry, language, closed_subsets, selected, budget
                ),
            )
            checked = 1
    except ModuleNotFoundError:
        try:
            issues.append(
                Issue(
                    "adapter.oak_backend_unavailable",
                    "Install bijux-ontology[oak] for the maintained "
                    "OAK basic interface",
                    kind="unverified",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining missing OAK implementation evidence")
    except LimitExceeded as error:
        issues.exhausted(str(error))
    report = Report(
        "adapter.oak_preparation",
        checked,
        tuple(issues),
        requested=1,
        complete=not any(item.kind in {"unverified", "exhausted"} for item in issues),
        regime="prepared-asserted-oak-basic",
    )
    return OakPreparation(adapter, report)
