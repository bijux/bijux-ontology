"""Oriented category/predicate/qualifier contracts through an actual BMT batch."""

import threading
from collections.abc import Sequence
from dataclasses import asdict, replace
from typing import cast

from bijux_ontology._json import object_value
from bijux_ontology.errors import InputError, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, Report

from .preparation import dispatch, failure, identity, reserve, resource
from .types import (
    BiolinkAssociation,
    BiolinkCheck,
    BiolinkInterpretation,
    BiolinkModel,
    BiolinkPolicy,
    BiolinkQualifier,
)


def _associations(
    values: Sequence[BiolinkAssociation], budget: Budget
) -> tuple[tuple[BiolinkAssociation, ...], list[dict[str, object]]]:
    """Own finite source locations/ordered occurrences before native name lookup."""
    selected = capture_items(values, BiolinkAssociation, budget)
    result, native = [], []
    for item in selected:
        if type(item) is not BiolinkAssociation:
            raise InputError("Use ordinary typed Biolink associations")
        for text in (
            item.association,
            item.subject_category,
            item.predicate,
            item.object_category,
            item.location,
            item.subject,
            item.object,
        ):
            if text is not None:
                if type(text) is not str:
                    raise InputError(
                        "Biolink categories, predicates and locations require strings"
                    )
                bound_text(text, budget)
        if any(
            not s
            for s in (
                item.association,
                item.subject_category,
                item.predicate,
                item.object_category,
            )
        ):
            raise InputError("Biolink association components cannot be empty")
        qualifiers = capture_items(item.qualifiers, BiolinkQualifier, budget)
        owned = []
        for qualifier in qualifiers:
            if type(qualifier) is not BiolinkQualifier:
                raise InputError("Use ordinary typed Biolink qualifier occurrences")
            for text in (qualifier.type_id, qualifier.value):
                if type(text) is not str or not text:
                    raise InputError(
                        "Biolink qualifiers require nonempty type and value strings"
                    )
                bound_text(text, budget)
            reserve(budget)
            owned.append(replace(qualifier))
        reserve(budget)
        current = replace(item, qualifiers=tuple(owned))
        result.append(current)
        # All transitive fields were paid before this maintained dataclass copy.
        budget.consume("bytes", 128 + 64 * (len(qualifiers) + 8))
        native.append(cast(dict[str, object], asdict(current)))
    return tuple(result), native


def _interpretations(
    value: object, maximum: int, reports: tuple[Report, ...]
) -> tuple[BiolinkInterpretation, ...]:
    """Adopt only bounded prepaid typed records from this fixed owned worker."""
    if type(value) is not list or len(value) > maximum or len(value) != len(reports):
        raise ResourceError("Biolink worker returned unexpected selected coverage")
    result = []
    fields = {
        "association",
        "subject_category",
        "predicate",
        "object_category",
        "qualified_predicate",
        "qualifiers",
        "constraints_json",
    }
    for raw, report in zip(value, reports, strict=True):
        record = object_value(raw, location="Biolink result")
        if set(record) != {"effective"}:
            raise ResourceError("Malformed Biolink interpretation envelope")
        if record["effective"] is None:
            if report.verified:
                raise ResourceError(
                    "Biolink worker hid an empty interpretation behind agreement"
                )
            continue
        data = object_value(record["effective"], location="Biolink effective context")
        if set(data) != fields or any(
            type(data[k]) is not str for k in fields - {"qualifiers"}
        ):
            raise ResourceError("Malformed effective Biolink interpretation")
        qualifiers = data["qualifiers"]
        if type(qualifiers) is not list or any(
            type(p) is not list or len(p) != 2 or any(type(x) is not str for x in p)
            for p in qualifiers
        ):
            raise ResourceError("Biolink worker lost ordered qualifier occurrences")
        result.append(
            BiolinkInterpretation(
                cast(str, data["association"]),
                cast(str, data["subject_category"]),
                cast(str, data["predicate"]),
                cast(str, data["object_category"]),
                cast(str, data["qualified_predicate"]),
                tuple((p[0], p[1]) for p in qualifiers),
                cast(str, data["constraints_json"]),
                report,
            )
        )
    return tuple(result)


def check_biolink(
    model: BiolinkModel,
    associations: Sequence[BiolinkAssociation],
    *,
    policy: BiolinkPolicy | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: threading.Event | None = None,
) -> BiolinkCheck:
    """Check selected Biolink application semantics, retaining the full statement.

    Finite class/enum/slot hierarchy answers belong to this local model release.
    They are not OWL contradictions or external identifier/type verification.
    Contextual qualifiers are not discarded to make a basic predicate pass.
    Other association payload fields and unhandled relevant LinkML rules remain
    outside this profile; no edge orientation or inverse predicate is guessed.
    """
    policy = BiolinkPolicy() if policy is None else policy
    if type(model) is not BiolinkModel or type(policy) is not BiolinkPolicy:
        raise InputError("Select a prepared BiolinkModel and explicit policy")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    selected: tuple[BiolinkAssociation, ...] = ()
    interpretations: tuple[BiolinkInterpretation, ...] = ()
    native_report = None
    native_identity = None
    requested = 1
    try:
        if (
            not model.report.verified
            or model.model is None
            or model.predicate_map is None
            or model.identity is None
        ):
            issues.append(
                Issue(
                    "biolink.preparation_unverified",
                    "Required local toolkit preparation did not complete",
                    kind="unverified",
                )
            )
        else:
            selected, native = _associations(associations, operation)
            if not selected:
                raise InputError(
                    "Biolink checks require explicit nonempty association coverage"
                )
            requested = len(selected)
            _, first = resource(model.model, operation)
            _, second = resource(model.predicate_map, operation)
            imports = []
            for value in model.imports:
                _, item = resource(value, operation)
                reserve(operation)
                imports.append(item)
            response, native_report = dispatch(
                {
                    "mode": "check",
                    "model": first,
                    "predicate_map": second,
                    "imports": imports,
                    "associations": native,
                    "policy": asdict(policy),
                },
                operation,
                expected=8192 + sum(4096 + 2048 * len(a.qualifiers) for a in selected),
            )
            issues.retain_completed(native_report)
            native_identity = identity(response["identity"])
            if native_identity != model.identity:
                raise ResourceError(
                    "Biolink batch identity differs from its prepared resources"
                )
            interpretations = _interpretations(
                response["interpretations"], requested, native_report.children
            )
            if native_report.verified and len(interpretations) != requested:
                raise ResourceError(
                    "Biolink native agreement omitted a required interpretation"
                )
            operation.consume("work", 0)
    except InputError as error:
        failure(error, issues)
    report = Report(
        "biolink.contract",
        native_report.checked if native_report else 0,
        tuple(issues),
        requested=requested,
        children=(native_report,) if native_report else (),
        regime="biolink-selected-association",
        exclusions=("non-qualifier payload slots and full LinkML instance validation",),
    )
    return BiolinkCheck(selected, interpretations, native_identity, report)
