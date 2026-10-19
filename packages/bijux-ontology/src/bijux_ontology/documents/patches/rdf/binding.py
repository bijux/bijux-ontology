"""Bind explicit ground RDF replacements to actual compact-source contributions."""

from collections.abc import Sequence
from dataclasses import replace
from typing import cast

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.documents.patches.operations import _Unsupported
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    bound_text,
    retain_report,
)
from bijux_ontology.json.paths import (
    SourcePath,
    at_path,
    bound_index,
    bound_path,
    bounded_location,
)
from bijux_ontology.jsonld.contracts.expectations import ContextExpectation
from bijux_ontology.jsonld.evidence.occurrences import ObservedOccurrence
from bijux_ontology.jsonld.selectors.types import EntitySelector
from bijux_ontology.jsonld.usages.checking import PreparedUsages
from bijux_ontology.rdf.patches.types import DatasetPatch, PatchLiteral, PatchQuad

from .types import SourceEditBinding


class AmbiguousSourceEdit(_Unsupported):
    """Multiple actual source aliases need the caller's explicit occurrence policy."""


def capture_bindings(
    bindings: Sequence[SourceEditBinding], patch: DatasetPatch, budget: Budget
) -> tuple[SourceEditBinding, ...]:
    """Capture each immutable request and require complete bijective delta pairing."""
    values = capture(bindings, budget)
    budget.consume("states", len(patch.removals) + len(patch.additions))
    budget.consume("bytes", 128 * (len(patch.removals) + len(patch.additions)))
    removed: set[int] = set()
    added: set[int] = set()
    owned: list[SourceEditBinding] = []
    for item in values:
        if type(item) is not SourceEditBinding:
            raise InputError("Source edits require ordinary SourceEditBinding records")
        for index in (item.removal, item.addition):
            bound_index(index, budget)
        if (
            item.removal >= len(patch.removals)
            or item.addition >= len(patch.additions)
            or item.removal in removed
            or item.addition in added
        ):
            raise InputError(
                "Source edit pairs must select distinct existing delta records"
            )
        paths = item.paths
        if isinstance(paths, tuple):
            budget.consume("states", len(paths))
            budget.consume("bytes", 64 * len(paths))
            for path in paths:
                bound_path(path, budget)
            if len(set(paths)) != len(paths):
                raise InputError("Source occurrence choices must be distinct")
        owned.append(replace(item))
        removed.add(item.removal)
        added.add(item.addition)
    if (
        not owned
        or len(removed) != len(patch.removals)
        or len(added) != len(patch.additions)
    ):
        raise _Unsupported(
            "Every removal and addition needs one explicit replacement pair"
        )
    if (
        patch.removed_graphs
        or patch.added_graphs
        or patch.base.kind not in ("graph", "dataset")
    ):
        raise _Unsupported(
            "Source edits require whole graph/dataset scope and fixed graph inventory"
        )
    return tuple(owned)


def _ground(quad: PatchQuad) -> None:
    """Do not interpret portable anonymous tokens as source processor identities."""
    if (
        not isinstance(quad.subject, URIRef)
        or not isinstance(quad.value, URIRef | PatchLiteral)
        or quad.graph is not None
        and not isinstance(quad.graph, URIRef)
    ):
        raise _Unsupported(
            "Edited anonymous endpoints or graph correspondence is unsupported"
        )


def _matches(item: ObservedOccurrence, quad: PatchQuad) -> bool:
    """Match exact supplied semantic roles, including actual reverse orientation."""
    if (
        item.predicate != str(quad.predicate)
        or item.graph != (str(quad.graph) if quad.graph is not None else None)
        or item.anonymous_graph
        or item.container != "unordered"
        or item.empty
    ):
        return False
    value = item.value
    if not isinstance(value, dict):
        return False
    if item.reverse:
        return (
            isinstance(quad.value, URIRef)
            and item.subject == str(quad.value)
            and value.get("@id") == str(quad.subject)
        )
    if item.subject != str(quad.subject):
        return False
    if isinstance(quad.value, URIRef):
        return value.get("@id") == str(quad.value)
    literal = quad.value
    if not isinstance(literal, PatchLiteral):
        return False
    traits = item.traits
    return (
        isinstance(value.get("@value"), str)
        and value["@value"] == literal.lexical
        and traits.datatype == literal.datatype
        and traits.language == literal.language
        and traits.direction is None
    )


def _paths(
    document: object,
    prepared: PreparedUsages,
    quad: PatchQuad,
    budget: Budget,
    issues: IssueCollector,
) -> tuple[SourcePath, ...]:
    """Reuse named entity/source associations, then inspect their actual fields."""
    owners: tuple[str, ...] = (str(quad.subject),)
    if isinstance(quad.value, URIRef) and quad.value != quad.subject:
        owners += (str(quad.value),)
    paths: list[SourcePath] = []
    for owner in owners:
        selection = prepared.select(
            EntitySelector(
                identifiers=(owner,),
                graph=str(quad.graph) if quad.graph is not None else None,
                coverage_unit="source_occurrence",
                minimum=0,
            )
        )
        retain_report(selection.report, budget, issues)
        if not selection.report.complete or selection.report.outcome not in (
            "agreement",
            "not_applicable",
        ):
            raise _Unsupported("Complete source occurrence ownership is unavailable")
        for parent in selection.paths:
            node = at_path(document, parent)
            if not isinstance(node, dict):
                raise _Unsupported(
                    "A source owner is not a supported describing object"
                )
            budget.consume("states", len(node))
            budget.consume("bytes", 64 * len(node))
            for key in node:
                if key == "@context":
                    continue
                budget.consume("work")
                bound_text(key, budget)
                path = (*parent, key)
                bound_path(path, budget)
                try:
                    usage = prepared.observe(path)
                except LimitExceeded:
                    raise
                except InputError as error:
                    raise _Unsupported(
                        "A possible source contribution cannot be associated"
                    ) from error
                if usage.limitation:
                    raise _Unsupported(
                        "A possible source contribution has ambiguous correspondence"
                    )
                if any(_matches(item, quad) for item in usage.occurrences):
                    budget.consume("work", len(paths) + 1)
                    if path not in paths:
                        budget.consume("states")
                        budget.consume("bytes", 64)
                        paths.append(path)
    return tuple(paths)


def _operation(
    document: object,
    prepared: PreparedUsages,
    path: SourcePath,
    before: PatchQuad,
    after: PatchQuad,
    budget: Budget,
) -> tuple[dict[str, object], ContextExpectation]:
    """Encode a narrow replacement, leaving the maintained processor authoritative."""
    usage = prepared.observe(path)
    budget.consume("states", len(usage.occurrences))
    budget.consume("bytes", 32 * len(usage.occurrences))
    matches = tuple(item for item in usage.occurrences if _matches(item, before))
    if len(usage.occurrences) != 1 or len(matches) != 1:
        raise _Unsupported(
            "A replacement requires one isolated source field contribution"
        )
    occurrence = matches[0]
    value = at_path(document, path)
    target = path
    if isinstance(before.value, PatchLiteral) and isinstance(after.value, PatchLiteral):
        if (
            before.subject != after.subject
            or before.value.datatype != after.value.datatype
            or before.value.language != after.value.language
            or occurrence.reverse
        ):
            raise _Unsupported("Literal edits preserve subject, datatype and language")
        replacement: object = after.value.lexical
        if isinstance(value, dict):
            if not isinstance(value.get("@value"), str) or set(value) - {
                "@value",
                "@type",
                "@language",
            }:
                raise _Unsupported(
                    "Value-object correspondence exceeds the supported profile"
                )
            target = (*path, "@value")
        elif not isinstance(value, str):
            raise _Unsupported("Typed source edits require exact lexical string values")
        expectation = ContextExpectation(
            path,
            str(after.predicate),
            value_kind="literal",
            datatype=after.value.datatype,
            language=after.value.language,
        )
    elif isinstance(before.value, URIRef) and isinstance(after.value, URIRef):
        if occurrence.reverse:
            if before.value != after.value:
                raise _Unsupported("Reverse edits retain the actual owning endpoint")
            replacement = str(after.subject)
        else:
            if before.subject != after.subject:
                raise _Unsupported("Forward edits retain the actual owning endpoint")
            replacement = str(after.value)
        if isinstance(value, dict):
            if set(value) != {"@id"} or not isinstance(value["@id"], str):
                raise _Unsupported(
                    "Named reference edits cannot rewrite a describing node"
                )
            target = (*path, "@id")
        elif not isinstance(value, str):
            raise _Unsupported(
                "Named source edits require a pure reference or IRI string"
            )
        expectation = ContextExpectation(
            path,
            str(after.predicate),
            value_kind="iri",
            identifier=replacement,
            reverse=occurrence.reverse,
        )
    else:
        raise _Unsupported("Replacement preserves literal versus named-reference roles")
    bound_text(cast(str, replacement), budget)
    pointer = bounded_location(target, budget)
    budget.consume("states", 4)
    budget.consume("bytes", 256)
    return {"op": "replace", "path": pointer, "value": replacement}, expectation


def project_operations(
    document: object,
    prepared: PreparedUsages,
    patch: DatasetPatch,
    bindings: tuple[SourceEditBinding, ...],
    budget: Budget,
    issues: IssueCollector,
) -> tuple[
    tuple[dict[str, object], ...],
    tuple[ContextExpectation, ...],
    tuple[SourceEditBinding, ...],
]:
    """Account for every requested edit and every selected actual occurrence."""
    operations: list[dict[str, object]] = []
    expectations: list[ContextExpectation] = []
    qualified: list[SourceEditBinding] = []
    edited: set[SourcePath] = set()
    for binding in bindings:
        before, after = (
            patch.removals[binding.removal],
            patch.additions[binding.addition],
        )
        _ground(before)
        _ground(after)
        if before.predicate != after.predicate or before.graph != after.graph:
            raise _Unsupported(
                "Source replacements retain predicate and physical graph"
            )
        candidates = _paths(document, prepared, before, budget, issues)
        if binding.paths is None:
            if len(candidates) > 1:
                raise AmbiguousSourceEdit(
                    "Several source aliases contribute the requested statement"
                )
            chosen = candidates
        elif binding.paths == "all":
            chosen = candidates
        else:
            chosen = binding.paths
            budget.consume("work", len(chosen) * (len(candidates) + 1))
            if any(path not in candidates for path in chosen):
                raise _Unsupported(
                    "Selected source occurrence does not establish "
                    "the requested RDF edge"
                )
        if not chosen:
            raise _Unsupported(
                "No supported source occurrence establishes the requested RDF edge"
            )
        budget.consume("states", 4 * len(chosen))
        budget.consume("bytes", 256 * len(chosen))
        for path in chosen:
            if path in edited:
                raise _Unsupported(
                    "A source field cannot receive competing semantic replacements"
                )
            operation, expectation = _operation(
                document, prepared, path, before, after, budget
            )
            edited.add(path)
            operations.append(operation)
            expectations.append(expectation)
        qualified.append(replace(binding, paths=chosen))
    return tuple(operations), tuple(expectations), tuple(qualified)
