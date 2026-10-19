"""Generate bounded representatives and retain each branch's actual outcome."""

import json
from collections.abc import Iterator, Sequence
from dataclasses import replace
from itertools import chain
from typing import cast

from rdflib.namespace import RDF, XSD

from bijux_ontology.absence import Absent
from bijux_ontology.declarations.types import FieldDeclaration, ValueDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.json.paths import location
from bijux_ontology.json.values import own_json
from bijux_ontology.jsonld.checking import check_context, prepare_usages
from bijux_ontology.jsonld.expectations import ContainerKind, ContextExpectation
from bijux_ontology.jsonld.resources import DocumentLoader, context_resources
from bijux_ontology.jsonld.values import ValueKind

from .types import ContextProbe, ProbeSample

_LEXICAL = {
    str(XSD.string): "bijux context probe",
    str(XSD.boolean): "true",
    str(XSD.integer): "0",
    str(XSD.decimal): "0.5",
    str(XSD.double): "1.0E0",
    str(XSD.date): "2000-01-01",
    str(XSD.dateTime): "2000-01-01T00:00:00Z",
    str(XSD.anyURI): "https://example.invalid/context-probe/value",
    str(RDF.langString): "bijux context probe",
}
_CONTAINERS: dict[str | None, ContainerKind | None] = {
    None: None,
    "@list": "list",
    "list": "list",
    "@set": "unordered",
    "unordered": "unordered",
}
_ROOT = "urn:bijux:context-probe:root"
_KINDS: dict[str, ValueKind] = {"literal": "literal", "iri": "iri"}


def _representatives(
    value: ValueDeclaration, sample: ProbeSample | None, limits: Limits
) -> Iterator[tuple[object, str]]:
    """Use explicit JSON shapes, authored literal lexical forms or neutral inputs."""
    if sample is not None:
        yield sample._read(limits), "authored-json"
        return
    if value.samples:
        for literal in value.samples:
            if len(literal) > limits.max_bytes:
                raise LimitExceeded("Probe lexical sample exceeds max_bytes")
            yield str(literal), "authored-lexical"
        return
    if value.kind == "literal":
        if value.datatype is not None and value.datatype not in _LEXICAL:
            return
        yield _LEXICAL.get(value.datatype or "", "bijux context probe"), "neutral"
    elif value.kind == "iri" and not value.types and value.model is None:
        yield "https://example.invalid/context-probe/value", "neutral"


def _expectation(
    field: FieldDeclaration, value: ValueDeclaration, *, typed: bool
) -> ContextExpectation:
    """Translate explicit declaration facts without interpreting context mappings."""
    return ContextExpectation(
        ((_ROOT,) if typed else ()) + (field.key or field.name,),
        field.predicate,
        value_kind=_KINDS.get(value.kind),
        datatype=value.datatype,
        language=value.language,
        direction=value.direction,
        types=value.types or None,
        container=_CONTAINERS[value.container],
        reverse=field.reverse,
    )


def _shape(value: object) -> str:
    """Describe only the finite authored source shape, never its JSON contents."""
    return (
        "object"
        if isinstance(value, dict)
        else "array"
        if isinstance(value, list)
        else "scalar"
    )


def _charge_text(text: str | Absent | None, budget: Budget) -> None:
    """Bound metadata used in generation and report paths before copying it."""
    budget.consume("work")
    if isinstance(text, str):
        if len(text) > budget.limits.max_bytes:
            raise LimitExceeded("Context probe metadata exceeds max_bytes")
        size = len(text.encode("utf-8"))
        if size > budget.limits.max_bytes:
            raise LimitExceeded("Context probe metadata exceeds max_bytes")
        budget.consume("bytes", size)


def _check_object(
    document: object,
    expectations: list[ContextExpectation],
    *,
    base: str | None,
    document_loader: DocumentLoader,
    budget: Budget,
) -> Report:
    """Qualify the resource union on the same maintained preparation as .check."""
    issues = IssueCollector(budget)
    actual: Report | None = None
    try:
        prepared = prepare_usages(document, base=base, document_loader=document_loader)
        actual = prepared.check(expectations)
        if actual.outcome in {"input_error", "exhausted"}:
            return actual
        usage = prepared.observe(expectations[0].path)
        if not (usage.limitation or usage.empty or usage.dropped):
            for item in usage.occurrences:
                budget.consume("work")
                kind = item.traits.kind
                if item.empty or kind == "unknown":
                    issues.append(
                        Issue(
                            "context.probe_unverified",
                            "No concrete resource value established for this sample",
                            item.location,
                            kind="unverified",
                        )
                    )
                elif kind not in {"iri", "blank", "node"}:
                    issues.append(
                        Issue(
                            "context.value_kind_mismatch",
                            "Object branches require resource values",
                            item.location,
                            expected="iri, blank or node",
                            actual=kind,
                        )
                    )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        issues.append(Issue("context.input_error", str(error), kind="input_error"))
    if actual is not None:
        return replace(actual, issues=actual.issues + tuple(issues))
    return Report(
        "contexts",
        issues=tuple(issues),
        requested=len(expectations),
        complete=False,
        regime="jsonld",
    )


def _empty_probe(
    probe: ContextProbe,
    context: object,
    scope: str,
    *,
    base: str | None,
    document_loader: DocumentLoader,
    budget: Budget,
) -> Report:
    """Retain required fieldless selections and qualify their actual selected types."""
    budget.consume("work")
    budget.consume("states")
    if not probe.type_values:
        issue = Issue(
            "context.probe_unverified",
            "A fieldless probe needs explicit type terms to establish a scope",
            scope,
            kind="unverified",
        )
        budget.consume("issues")
        return Report(
            scope,
            issues=(issue,),
            requested=1,
            complete=False,
            regime="jsonld-representative",
        )
    actual = check_context(
        {
            "@context": context,
            _ROOT: {
                "@id": "urn:bijux:context-probe:subject",
                "@type": list(probe.type_values),
            },
        },
        [ContextExpectation((_ROOT,), _ROOT, types=probe.model.types)],
        base=base,
        document_loader=document_loader,
    )
    return replace(actual, scope=scope, regime="jsonld-representative")


def _limitation(
    probe: ContextProbe, field: FieldDeclaration, value: ValueDeclaration
) -> str | None:
    """Require independent semantic evidence for metadata and map-key meanings."""
    if value.kind == "unknown" or value.container not in _CONTAINERS:
        return "Unknown value kind or unsupported container"
    if field.predicate in {"@id", "@type"}:
        return "Structural metadata fields need a proven representation"
    key = field.key or field.name
    if key == "@context" or key.startswith("@") and key != field.predicate:
        return "A structural source keyword cannot represent this field"
    if probe.type_values and key == "@type":
        return "The selected root type scope already owns @type"
    return None


def probe_context(
    context: object,
    probes: Sequence[ContextProbe],
    *,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Report:
    """Qualify finite representative branches through real check_context calls.

    A branch scope records model, field, branch, sample origin, source shape and
    requested container. Agreement concerns these generated inputs only. Unknown
    kinds, unrepresented object branches and unsupported container shapes remain
    unverified; this does not certify arbitrary documents or Python serializers.
    Referenced models and application output obligations require their own checks.
    Map-container kinds and structural field metadata are not qualified.
    """
    loader = context_resources(document_loader, limits=limits, budget=budget)
    operation = loader.budget
    issues = IssueCollector(operation)
    children: list[Report] = []
    checked = 0
    requested = 0
    inventory_complete = False
    try:
        if not isinstance(probes, Sequence) or isinstance(
            cast(object, probes), (str, bytes)
        ):
            raise InputError("Expected an explicit sequence of ContextProbe values")
        selection_size = len(probes)
        if selection_size > operation.limits.max_states:
            raise LimitExceeded("Context probe selection exceeds max_states")
        captured = []
        for index in range(selection_size):
            operation.consume("work")
            operation.consume("states")
            try:
                captured.append(probes[index])
            except IndexError as error:
                raise InputError("Probe sequence changed during capture") from error
        selected = tuple(captured)
        lookups: list[dict[tuple[str, int], ProbeSample]] = []
        for probe in selected:
            operation.consume("work")
            if not isinstance(probe, ContextProbe):
                raise InputError("Expected explicit ContextProbe values")
            if len(probe.type_values) > operation.limits.max_states:
                raise LimitExceeded("Selected context type terms exceed max_states")
            _charge_text(probe.model.identifier, operation)
            for term in chain(probe.model.types, probe.type_values):
                _charge_text(term, operation)
            lookup: dict[tuple[str, int], ProbeSample] = {}
            for sample in probe.samples:
                operation.consume("work")
                lookup[sample.field, sample.branch] = sample
            lookups.append(lookup)
            if not probe.model.fields:
                requested += 1
                if requested > operation.limits.max_states:
                    raise LimitExceeded("Context probe branches exceed max_states")
            for field in probe.model.fields:
                operation.consume("work")
                for text in (field.name, field.key, field.predicate):
                    _charge_text(text, operation)
                for branch, value in enumerate(field.values):
                    operation.consume("work")
                    for value_text in chain(
                        (
                            value.kind,
                            value.datatype,
                            value.language,
                            value.direction,
                            value.container,
                            value.model,
                        ),
                        value.types,
                    ):
                        _charge_text(value_text, operation)
                    explicit = (field.name, branch) in lookup
                    requested += 1 if explicit else max(1, len(value.samples))
                    if requested > operation.limits.max_states:
                        raise LimitExceeded("Context probe branches exceed max_states")
        inventory_complete = True
        owned = own_json(
            context,
            max_bytes=operation.limits.max_bytes,
            max_depth=operation.limits.max_depth,
        )
        operation.consume("bytes", len(json.dumps(owned).encode("utf-8")))
        for probe_index, probe in enumerate(selected):
            lookup = lookups[probe_index]
            if not probe.model.fields:
                actual = _empty_probe(
                    probe,
                    owned,
                    location(
                        ("context-probes", probe_index, probe.model.identifier, "types")
                    ),
                    base=base,
                    document_loader=loader,
                    budget=operation,
                )
                children.append(actual)
                checked += int(actual.complete)
                if actual.outcome == "exhausted":
                    raise LimitExceeded("Type representative exhausted its budget")
                continue
            for field in probe.model.fields:
                for branch, value in enumerate(field.values):
                    operation.consume("work")
                    scope = location(
                        (
                            "context-probes",
                            probe_index,
                            probe.model.identifier,
                            field.name,
                            branch,
                        )
                    )
                    limitation = _limitation(probe, field, value)
                    selected_sample = lookup.get((field.name, branch))
                    key = field.key or field.name
                    representatives = (
                        _representatives(value, selected_sample, operation.limits)
                        if limitation is None
                        else iter(())
                    )
                    seen = False
                    for sample_index, (raw, origin) in enumerate(representatives):
                        seen = True
                        operation.consume("states")
                        if value.container in {
                            "@list",
                            "list",
                            "@set",
                            "unordered",
                        } and not isinstance(raw, (dict, list)):
                            raw = [raw]
                        source_shape = _shape(raw)
                        node: dict[str, object] = {key: raw}
                        document: dict[str, object] = {"@context": owned, **node}
                        expectations = [
                            _expectation(field, value, typed=bool(probe.type_values))
                        ]
                        if probe.type_values:
                            node.setdefault("@id", "urn:bijux:context-probe:subject")
                            node["@type"] = list(probe.type_values)
                            document = {"@context": owned, _ROOT: node}
                            expectations.append(
                                ContextExpectation(
                                    (_ROOT,), _ROOT, types=probe.model.types
                                )
                            )
                        actual = (
                            _check_object(
                                document,
                                expectations,
                                base=base,
                                document_loader=loader,
                                budget=operation,
                            )
                            if value.kind == "object"
                            else check_context(
                                document,
                                expectations,
                                base=base,
                                document_loader=loader,
                            )
                        )
                        branch_scope = scope + location(
                            (
                                sample_index,
                                origin,
                                source_shape,
                                value.container or "unconstrained",
                            )
                        )
                        children.append(
                            replace(
                                actual,
                                scope=branch_scope,
                                regime="jsonld-representative",
                            )
                        )
                        checked += int(actual.complete)
                        if any(issue.kind == "exhausted" for issue in actual.issues):
                            raise LimitExceeded(
                                "Context representative checking exhausted its budget"
                            )
                    if not seen:
                        issue = Issue(
                            "context.probe_unverified",
                            limitation
                            or "This branch requires an explicit representative",
                            scope,
                            kind="unverified",
                        )
                        issues.append(issue)
                        children.append(
                            Report(
                                scope,
                                issues=(issue,),
                                requested=1,
                                complete=False,
                                regime="jsonld-representative",
                            )
                        )
    except LimitExceeded as error:
        if not any(
            issue.kind == "exhausted" for child in children for issue in child.issues
        ):
            message = str(error)
            if not inventory_complete:
                message += "; representative inventory was not fully enumerated"
            issues.exhausted(message)
    except InputError as error:
        issues.append(Issue("context.input_error", str(error), kind="input_error"))
    return Report(
        "context-probes",
        checked,
        tuple(issues),
        requested=max(requested, checked),
        complete=checked == requested,
        regime="jsonld-representatives",
        children=tuple(children),
    )
