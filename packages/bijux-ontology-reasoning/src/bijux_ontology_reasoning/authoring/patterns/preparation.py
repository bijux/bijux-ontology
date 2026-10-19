"""Bounded source capture for an explicit native DOSDP JSON profile."""

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path

from rdflib.namespace import OWL

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import Budget, Limits, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri

from .types import DosdpPattern


def _text(value: object, operation: Budget) -> str:
    """Bound ordinary source text before parsing identifiers or copying keys."""
    if type(value) is not str or not value:
        raise InputError("DOSDP profile requires nonempty ordinary text")
    bound_text(value, operation)
    return value


def _iri(value: object, operation: Budget) -> str:
    """Retain an absolute identifier without CURIE guesses or normalization."""
    return str(absolute_iri(_text(value, operation)))


def prepare_dosdp_pattern(
    content: Path | str | bytes,
    *,
    prefixes: Mapping[str, str],
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> DosdpPattern:
    """Capture JSON DOSDP, a native YAML subset, with explicit finite capabilities.

    Only logical equivalentTo patterns with ordinary named variables constrained
    to an explicitly bound owl:Thing alias are qualified. Other valid fields or
    constraints remain in the original source and yield required unknown work.
    No YAML fallback, pattern URL lookup or implicit OBO prefixes are attempted.
    """
    if (
        limits is not None
        and not isinstance(limits, Limits)
        or budget is not None
        and not isinstance(budget, Budget)
    ):
        raise InputError("Pattern capture requires Limits and Budget objects")
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Pattern capture limits conflict with the shared budget")
    operation = budget if budget is not None else Budget(limits)
    operation.consume("resources")
    if isinstance(content, Path):
        bound_text(str(content), operation)
        if content.is_symlink() or not content.is_file():
            raise ResourceError("The supplied local DOSDP pattern is unavailable")
        allowance = min(operation.limits.max_bytes, operation.remaining("bytes"))
        try:
            with content.open("rb") as reader:
                raw = reader.read(allowance + 1)
        except OSError as error:
            raise ResourceError(
                "The supplied local DOSDP pattern cannot be read"
            ) from error
        if len(raw) > allowance:
            raise LimitExceeded("DOSDP pattern exceeds its remaining byte allowance")
    elif type(content) is str:
        bound_text(content, operation)
        raw = content.encode("utf-8")
    elif isinstance(content, bytes):
        raw = content
    else:
        raise InputError("DOSDP source requires JSON bytes, text or a local Path")
    if len(raw) > operation.limits.max_bytes:
        raise LimitExceeded("DOSDP pattern exceeds max_bytes")
    operation.consume("bytes", len(raw))
    operation.consume("work", len(raw))
    data = object_value(
        decode_json(
            raw,
            max_bytes=operation.limits.max_bytes,
            max_depth=operation.limits.max_depth,
        ),
        location="DOSDP pattern",
    )
    operation.consume("bytes", json_bound(data, operation))
    if not isinstance(prefixes, Mapping):
        raise InputError("DOSDP prefixes require an explicit mapping")
    captured: dict[str, str] = {}
    for name, value in prefixes.items():
        operation.consume("states")
        key = _text(name, operation)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key) is None:
            raise InputError("DOSDP prefixes require ordinary explicit prefix names")
        captured[key] = _iri(value, operation)
    identity = _iri(data.get("pattern_iri"), operation)
    unsupported = []
    supported = {
        "pattern_name",
        "pattern_iri",
        "classes",
        "relations",
        "vars",
        "equivalentTo",
    }
    for key in data:
        operation.consume("work")
        if key not in supported:
            unsupported.append("Unsupported DOSDP field: " + key)
    if "pattern_name" in data:
        _text(data["pattern_name"], operation)
    resolved: dict[str, str] = {}
    for field in ("classes", "relations"):
        values = object_value(data.get(field, {}), location=field)
        for raw_name, raw_value in values.items():
            operation.consume("states")
            name = _text(raw_name, operation)
            value = _text(raw_value, operation)
            prefix, separator, suffix = value.partition(":")
            expanded = (
                captured[prefix] + suffix if separator and prefix in captured else value
            )
            # Explicit absolute IRIs remain usable; unknown prefix-like strings
            # cannot become an implicit OBO binding in the native invocation.
            if (
                separator
                and prefix not in captured
                and not value.startswith(("http://", "https://", "urn:"))
            ):
                raise ResourceError("DOSDP class/relation has no supplied prefix")
            bound_text(expanded, operation)
            iri = str(absolute_iri(expanded))
            if field == "classes":
                resolved[name] = iri
    variables = object_value(data.get("vars"), location="DOSDP vars")
    if not variables:
        raise InputError("DOSDP profile requires a nonempty named variable inventory")
    names = []
    for name, constraint in variables.items():
        operation.consume("states")
        name = _text(name, operation)
        constraint = _text(constraint, operation)
        if (
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
            or name == "defined_class"
            or name.endswith("_label")
        ):
            raise ResourceError(
                "DOSDP variable name conflicts with native binding columns"
            )
        names.append(name)
        alias = (
            constraint[1:-1]
            if constraint.startswith("'") and constraint.endswith("'")
            else None
        )
        if alias is None or resolved.get(alias) != str(OWL.Thing):
            unsupported.append("Unsupported named filler constraint: " + name)
    expression = object_value(data.get("equivalentTo", {}), location="equivalentTo")
    if set(expression) != {"text", "vars"}:
        unsupported.append(
            "Only one explicit equivalentTo text/vars expression is qualified"
        )
    else:
        template = _text(expression["text"], operation)
        selected = expression["vars"]
        if type(selected) is not list or any(
            type(value) is not str for value in selected
        ):
            raise InputError("DOSDP equivalentTo vars require an explicit text array")
        for value in selected:
            _text(value, operation)
        if set(selected) != set(names):
            unsupported.append(
                "Every named variable must occur in the logical expression inventory"
            )
        if template.count("%s") != len(selected) or "%" in template.replace("%s", ""):
            unsupported.append(
                "Logical placeholders must exactly match the variable inventory"
            )
    operation.consume("states", len(names) + len(captured) + len(unsupported))
    return DosdpPattern._owned(
        iri=identity,
        content=raw,
        prefixes=tuple(captured.items()),
        variables=tuple(names),
        sha256=hashlib.sha256(raw).hexdigest(),
        unsupported=tuple(unsupported),
    )
