"""Finite declaration-driven mapping assembly with maintained qualification."""

import json
from collections.abc import Sequence
from dataclasses import replace
from itertools import chain
from typing import cast

from rdflib.namespace import RDF

from bijux_ontology._json import own_json
from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.declarations.types import FieldDeclaration, ValueDeclaration
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.reports import Issue, Report

from ..context_probes.checking import probe_context
from ..context_probes.types import ContextProbe
from .types import CompiledContext

_CONTAINERS = {
    None: None,
    "@list": "@list",
    "list": "@list",
    "@set": "@set",
    "unordered": "@set",
}


class _Compilation:
    """Keep one operation's bounded mappings and unresolved scope diagnostics."""

    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        self.issues = IssueCollector(budget)

    def text(self, value: str | Absent | None) -> int:
        """Charge original mapping metadata before retaining generated copies."""
        self.budget.consume("work")
        if isinstance(value, str):
            if len(value) > self.budget.limits.max_bytes:
                raise LimitExceeded("Context metadata exceeds max_bytes")
            try:
                size = len(value.encode("utf-8"))
            except UnicodeEncodeError as error:
                raise InputError("Context metadata requires valid Unicode") from error
            if size > self.budget.limits.max_bytes:
                raise LimitExceeded("Context metadata exceeds max_bytes")
            self.budget.consume("bytes", size)
            return size
        return 0

    def unresolved(
        self, message: str, location: str, *, conflict: bool = False
    ) -> None:
        """Keep a required unsupported or conflicting mapping visible."""
        self.text(location)
        self.issues.append(
            Issue(
                "context.compilation_conflict"
                if conflict
                else "context.compilation_unverified",
                message,
                location,
                kind="finding" if conflict else "unverified",
            )
        )

    def ordinary(self, term: str, location: str) -> bool:
        """Support explicit ordinary names without inventing prefixes or keywords."""
        if term.startswith("@") or ":" in term or not term.strip():
            self.unresolved("Only ordinary compact term names are supported", location)
            return False
        return True

    def bound(self, probe: ContextProbe) -> None:
        """Bound selected declarations before rebuilding probes or mapping tables."""
        identifier_size = self.text(probe.model.identifier)
        explicit: set[tuple[str, int]] = set()
        self.budget.consume("states", len(probe.model.types) + len(probe.type_values))
        for type_text in chain(probe.model.types, probe.type_values):
            self.text(type_text)
        for sample in probe.samples:
            self.budget.consume("states")
            self.text(sample.field)
            explicit.add((sample.field, sample.branch))
        if not probe.model.fields:
            self.budget.consume("bytes", 2 * identifier_size + 128)
        for field in probe.model.fields:
            self.budget.consume("states")
            name_size = self.text(field.name)
            for field_text in (field.key, field.predicate):
                self.text(field_text)
            for branch, value in enumerate(field.values):
                self.budget.consume("states")
                for text in chain(
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
                    self.text(text)
                representatives = (
                    1
                    if (field.name, branch) in explicit
                    else max(1, len(value.samples))
                )
                if representatives > self.budget.limits.max_states:
                    raise LimitExceeded("Context representatives exceed max_states")
                self.budget.consume("work", representatives)
                # Probe scopes retain escaped model/field/container text per sample.
                # Reserve that repeated metadata before the existing checker runs.
                container_size = (
                    len(value.container.encode("utf-8")) if value.container else 0
                )
                scope_size = 2 * (identifier_size + name_size + container_size) + 128
                self.budget.consume("bytes", representatives * scope_size)

    def branch(
        self, field: FieldDeclaration, value: ValueDeclaration, location: str
    ) -> dict[str, object] | None:
        """Compile only explicit scalar/resource and finite container intent."""
        self.budget.consume("work")
        if value.kind == "unknown" or value.container not in _CONTAINERS:
            self.unresolved("Unknown value kind or unsupported container", location)
            return None
        if field.reverse and value.kind == "literal":
            self.unresolved(
                "Reverse properties require resource values", location, conflict=True
            )
            return None
        definition: dict[str, object] = {
            "@reverse" if field.reverse else "@id": field.predicate
        }
        if value.kind == "iri":
            definition["@type"] = "@id"
        elif value.kind == "literal":
            language = value.language
            datatype = value.datatype
            if datatype == str(RDF.langString):
                if not isinstance(language, str):
                    self.unresolved(
                        "Language-string coercion requires an explicit language",
                        location,
                    )
                    return None
            elif datatype is not None:
                if isinstance(language, str) or isinstance(value.direction, str):
                    self.unresolved(
                        "Typed literal coercion conflicts with language or direction",
                        location,
                        conflict=True,
                    )
                    return None
                definition["@type"] = datatype
            if datatype is None or datatype == str(RDF.langString):
                if language is not None:
                    definition["@language"] = None if language is ABSENT else language
                if value.direction is not None:
                    definition["@direction"] = (
                        None if value.direction is ABSENT else value.direction
                    )
        container = _CONTAINERS[value.container]
        if container is not None:
            definition["@container"] = container
        self.budget.consume("states", len(definition))
        return definition

    def fields(self, probe: ContextProbe) -> dict[str, object]:
        """Require all alternatives sharing one source name to agree on its map."""
        definitions: dict[str, object] = {}
        for field in probe.model.fields:
            self.budget.consume("work")
            location = f"{probe.model.identifier}.{field.name}"
            self.text(location)
            key = field.key or field.name
            if not self.ordinary(key, location):
                continue
            if field.predicate in {"@id", "@type"}:
                self.unresolved(
                    "Structural field aliases need a proven representation", location
                )
                continue
            selected = None
            valid = True
            for branch, value in enumerate(field.values):
                branch_location = f"{location}[{branch}]"
                self.text(branch_location)
                definition = self.branch(field, value, branch_location)
                if definition is None:
                    valid = False
                elif selected is not None and selected != definition:
                    self.unresolved(
                        "Alternative branches require incompatible context mappings",
                        location,
                        conflict=True,
                    )
                    valid = False
                else:
                    selected = definition
            if valid and selected is not None:
                self.add(definitions, key, selected, location)
        return definitions

    def add(
        self, target: dict[str, object], key: str, definition: object, location: str
    ) -> None:
        """Reject conflicting aliases rather than selecting one by input order."""
        self.budget.consume("work")
        if key in target and target[key] != definition:
            self.unresolved(
                "Conflicting definitions in the same context scope",
                location,
                conflict=True,
            )
        else:
            self.budget.consume("states")
            target[key] = definition

    def scope(
        self, probe: ContextProbe, scoped: bool
    ) -> tuple[ContextProbe, str | None] | None:
        """Bind one explicit type alias or use declared absolute types directly."""
        model = probe.model
        if not scoped and (
            not probe.type_values or set(probe.type_values) == set(model.types)
        ):
            return (replace(probe, type_values=model.types), None)
        if len(probe.type_values) != 1 or len(model.types) != 1:
            self.unresolved(
                "A compact type scope requires one selected term and one declared type",
                model.identifier,
            )
            return None
        term = probe.type_values[0]
        if not self.ordinary(term, model.identifier):
            return None
        return probe, term


def compile_context(
    probes: Sequence[ContextProbe],
    *,
    type_scoped: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> CompiledContext:
    """Compile explicit output terms and qualify every selected representative.

    Shared mode requires one common term definition. Type-scoped mode uses one
    explicit compact type term for one declared class per selected probe. No
    mappings arise from Python types, required fields or ontology inference.
    Map containers, structural field aliases and ambiguous scope choices remain
    unsupported. Actual serializer output requires a separate sampled check
    retaining this result's report; generated agreement is never universal.
    """
    if type(type_scoped) is not bool:
        raise InputError("type_scoped requires an explicit boolean")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Explicit limits conflict with the shared budget")
    operation = budget if budget is not None else Budget(limits)
    compilation = _Compilation(operation)
    context: dict[str, object] = {"@version": 1.1}
    selected: list[ContextProbe] = []
    aliases: set[str] = set()
    field_terms: set[str] = set()
    generated: Report | None = None
    requested = 1
    checked = 0
    complete = False
    try:
        if not isinstance(probes, Sequence) or isinstance(
            cast(object, probes), (str, bytes)
        ):
            raise InputError(
                "Compilation requires a finite sequence of ContextProbe values"
            )
        count = len(probes)
        requested = max(1, count)
        if count > operation.limits.max_states:
            raise LimitExceeded("Context compilation selection exceeds max_states")
        captured = []
        for index in range(count):
            operation.consume("work")
            operation.consume("states")
            try:
                probe = probes[index]
            except IndexError as error:
                raise InputError(
                    "Compilation selection changed during capture"
                ) from error
            if not isinstance(probe, ContextProbe):
                raise InputError("Compilation requires explicit ContextProbe values")
            captured.append(probe)
        if not captured:
            compilation.unresolved(
                "Compilation requires at least one selected scope", ""
            )
        for probe in captured:
            compilation.bound(probe)
            scope = compilation.scope(probe, type_scoped)
            if scope is None:
                checked += 1
                continue
            adjusted, term = scope
            definitions = compilation.fields(probe)
            if term is not None:
                if term in field_terms or term in definitions:
                    compilation.unresolved(
                        "Type aliases cannot also be field terms",
                        probe.model.identifier,
                        conflict=True,
                    )
                definition: dict[str, object] = {"@id": probe.model.types[0]}
                if type_scoped:
                    definition["@context"] = definitions
                operation.consume("states")
                aliases.add(term)
                compilation.add(context, term, definition, probe.model.identifier)
            if not type_scoped:
                for key, definition_value in definitions.items():
                    operation.consume("states")
                    field_terms.add(key)
                    if key in aliases:
                        compilation.unresolved(
                            "Type aliases cannot also be field terms",
                            probe.model.identifier,
                            conflict=True,
                        )
                    compilation.add(
                        context, key, definition_value, probe.model.identifier
                    )
            operation.consume("states")
            selected.append(adjusted)
            checked += 1
        if checked == requested and not compilation.issues:
            owned = own_json(
                context,
                max_bytes=operation.limits.max_bytes,
                max_depth=operation.limits.max_depth,
            )
            operation.consume(
                "bytes",
                len(
                    json.dumps(owned, ensure_ascii=False, separators=(",", ":")).encode(
                        "utf-8"
                    )
                ),
            )
            generated = probe_context(owned, selected, budget=operation)
            complete = generated.complete
    except LimitExceeded as error:
        compilation.issues.exhausted(str(error))
    except InputError as error:
        compilation.issues.append(
            Issue("context.input_error", str(error), kind="input_error")
        )
    report = Report(
        "context-compilation",
        checked,
        tuple(compilation.issues),
        requested=requested,
        complete=complete,
        regime="jsonld-generated-context",
        children=(generated,) if generated is not None else (),
    )
    return CompiledContext(context, report, limits=operation.limits)
