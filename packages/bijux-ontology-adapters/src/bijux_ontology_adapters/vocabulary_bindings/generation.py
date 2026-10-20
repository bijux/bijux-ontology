"""Generate ordinary constants without evaluating vocabulary text or inventing IRIs."""

import keyword
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Literal

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView

from .types import GeneratedBindings, PythonBinding

_RESERVED = frozenset(
    (
        "TERMS",
        "INVENTORY",
        "__all__",
        "_Mapping",
        "_MappingProxyType",
        "_URIRef",
        "_TermDeclaration",
    )
)
_HEADER = '''"""Finite vocabulary bindings; unknown names never expand to IRIs."""

from collections.abc import Mapping as _Mapping
from types import MappingProxyType as _MappingProxyType
from rdflib import URIRef as _URIRef
from bijux_ontology.declarations.types import TermDeclaration as _TermDeclaration

'''


def generate_vocabulary_bindings(
    vocabulary: VocabularyView,
    iris: Sequence[str],
    *,
    names: Mapping[str, str] | None = None,
    aliases: Mapping[str, str] | None = None,
    invalid_names: Literal["reject", "normalize"] = "reject",
    reserved: Sequence[str] = (),
    annotations: Sequence[str] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> GeneratedBindings:
    """Select declared terms and explicit names, retaining unresolved collisions.

    ``names`` maps selected IRIs to primary names; ``aliases`` maps additional
    names to selected IRIs. Normalization replaces nonidentifier characters,
    prefixes digits and suffixes keywords. Python's NFKC identifier equivalence
    applies in both policies. Collisions never acquire arbitrary suffixes.
    Annotation predicates explicitly select documentation, never executable text.
    """
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Explicit limits conflict with the shared operation budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    bindings: list[PythonBinding] = []
    checked = 0
    requested = 0
    source: str | None = None

    def text(value: str) -> str:
        """Qualify caller metadata before normalization, hashing or retention."""
        bound_text(value, operation)
        return str(value)

    def identifier(value: str) -> str:
        """Reserve error literal storage before maintained IRI validation."""
        operation.consume("bytes", 12 * len(value) + 2)
        return absolute_iri(value)

    def sequence(values: Sequence[str]) -> tuple[str, ...]:
        """Own a finite inventory without accepting unbounded iterators."""
        if not isinstance(values, Sequence) or isinstance(values, str | bytes):
            raise InputError("Binding inventories require finite text sequences")
        operation.consume("states", len(values))
        return tuple(text(value) for value in values)

    def mapping(values: Mapping[str, str] | None) -> dict[str, str]:
        """Capture exact author overrides under the shared operation allowance."""
        if values is None:
            return {}
        if not isinstance(values, Mapping):
            raise InputError("Binding overrides require explicit mappings")
        operation.consume("states", len(values))
        return {text(key): text(value) for key, value in values.items()}

    def unresolved(message: str, location: str) -> None:
        """Retain unresolved naming intent without silently choosing a winner."""
        issues.append(
            Issue(
                "vocabulary.binding_name_unresolved",
                message,
                location,
                kind="input_error",
            )
        )

    def literal(value: str) -> str:
        """Encode bounded data as a Python literal, never executable syntax."""
        # Python repr is a maintained literal encoder. Reserve its worst-case
        # escaped storage before allocating it; never interpolate raw RDF text.
        text(value)
        operation.consume("bytes", 12 * len(value) + 2)
        operation.consume("work", len(value) + 1)
        return repr(value)

    try:
        if not isinstance(vocabulary, VocabularyView) or invalid_names not in (
            "reject",
            "normalize",
        ):
            raise InputError(
                "Expected an owned vocabulary view and explicit naming policy"
            )
        selected = sequence(iris)
        requested = len(selected)
        overrides, additional = mapping(names), mapping(aliases)
        forbidden = _RESERVED | {
            unicodedata.normalize("NFKC", value) for value in sequence(reserved)
        }
        predicates = tuple(identifier(value) for value in sequence(annotations))
        selected_set = set(selected)
        if len(selected_set) != len(selected):
            raise InputError("Selected vocabulary IRIs must be distinct")
        if overrides.keys() - selected_set or set(additional.values()) - selected_set:
            raise InputError("Name overrides and aliases must refer to selected IRIs")
        candidates: list[PythonBinding] = []
        by_name: dict[str, list[PythonBinding]] = {}
        operation.consume("states", len(selected) + len(additional))
        by_iri: dict[str, list[str]] = {}
        for name, target in additional.items():
            operation.consume("work")
            by_iri.setdefault(target, []).append(name)
        for iri in selected:
            identifier(iri)
            operation.consume("work")
            term = vocabulary.lookup(iri)
            if term.status != "declared":
                unresolved(
                    "Selected IRI has no declaration in the owned vocabulary", iri
                )
                checked += 1
                continue
            operation.consume("states", len(term.kinds))
            kinds = tuple(text(kind) for kind in term.kinds)
            docs: tuple[str, ...] = ()
            if predicates:
                result = vocabulary.annotations(
                    iri, predicates=predicates, budget=operation
                )
                retain_report(result.report, operation, issues)
                for annotation in result.items:
                    bound_node(annotation.term, operation)
                docs = tuple(annotation.value for annotation in result.items)
            proposals = [overrides.get(iri, re.split(r"[/#:]", iri)[-1])]
            proposals.extend(by_iri.get(iri, ()))
            operation.consume("work", len(proposals))
            for proposed in proposals:
                name = unicodedata.normalize("NFKC", proposed)
                if invalid_names == "normalize":
                    name = re.sub(r"\W", "_", name)
                    if name and name[0].isdigit():
                        name = "_" + name
                    if keyword.iskeyword(name):
                        name += "_"
                text(name)
                if (
                    not name.isidentifier()
                    or keyword.iskeyword(name)
                    or name in forbidden
                    or name.startswith("__")
                ):
                    unresolved(
                        "Name is invalid or reserved under the selected naming policy",
                        iri,
                    )
                    continue
                binding = PythonBinding(name, iri, kinds, docs)
                candidates.append(binding)
                by_name.setdefault(name, []).append(binding)
            checked += 1
        for binding in candidates:
            operation.consume("work")
            if len(by_name[binding.name]) != 1:
                unresolved(
                    "Multiple bindings normalize to the same Python name", binding.iri
                )
            else:
                bindings.append(binding)
        chunks: list[str] = []
        size = 0

        def emit(chunk: str) -> None:
            """Bound cumulative candidate storage before retaining each fragment."""
            nonlocal size
            bound_text(chunk, operation)
            size += len(chunk.encode("utf-8"))
            if size > operation.limits.max_bytes:
                raise LimitExceeded("Generated Python module exceeds max_bytes")
            chunks.append(chunk)

        emit(_HEADER)
        for binding in bindings:
            emit(f"{binding.name} = _URIRef({literal(binding.iri)})\n")
            for documentation in binding.documentation:
                emit(literal(documentation) + "\n")
        emit("\nTERMS: _Mapping[str, _URIRef] = _MappingProxyType({\n")
        for binding in bindings:
            emit(f"    {literal(binding.name)}: {binding.name},\n")
        emit("})\nINVENTORY: _Mapping[str, _TermDeclaration] = _MappingProxyType({\n")
        for binding in bindings:
            roles = ", ".join(literal(kind) for kind in binding.kinds)
            if len(binding.kinds) == 1:
                roles += ","
            emit(
                f"    {literal(binding.name)}: _TermDeclaration("
                f"{literal(binding.iri)}, ({roles}), {literal(binding.name)}),\n"
            )
        emit(
            "})\n__all__ = ["
            + ", ".join(
                [literal(binding.name) for binding in bindings]
                + ["'TERMS'", "'INVENTORY'"]
            )
            + "]\n"
        )
        source = "".join(chunks)
        bound_text(source, operation)
    except LimitExceeded as error:
        issues.exhausted(str(error))
        source = None
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "vocabulary.binding_name_unresolved", str(error), kind="input_error"
                )
            )
        except LimitExceeded as limit:
            issues.exhausted(str(limit))
    return GeneratedBindings(
        source,
        tuple(bindings),
        Report(
            "Python vocabulary bindings",
            checked,
            tuple(issues),
            requested=max(requested, checked),
            complete=source is not None and not issues,
        ),
    )
