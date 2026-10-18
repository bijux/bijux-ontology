"""Finite application uniqueness over explicitly associated RDF records."""

import json
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.term import Node

from bijux_ontology.declarations.keys import KeyComponent, KeyContract
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.path_traversal import (
    PathIdentityUnavailable,
    finite_path_steps,
    path_values,
)
from bijux_ontology.rdf.paths import capture_path
from bijux_ontology.rdf.terms import bound_node, bound_text, term_key
from bijux_ontology.rdf.value_comparison.comparing import compare_literal_values
from bijux_ontology.reports import Issue, Report

from .types import KeyRecord


@dataclass(frozen=True)
class _Key:
    """Original associated values beside their qualified comparison tokens."""

    record: KeyRecord
    source_index: int
    values: tuple[Node, ...]
    tokens: tuple[tuple[str | None, ...], ...]


def _token(
    value: Node, rule: KeyComponent, budget: Budget
) -> tuple[str | None, ...] | None:
    """Normalize only supported string lexical values, retaining RDF identity."""
    bound_node(value, budget)
    if rule.normalization == "none":
        return term_key(value)
    if not isinstance(value, Literal) or value.datatype not in (
        None,
        XSD.string,
        RDF.langString,
    ):
        return None
    text = str(value)
    budget.consume("work", len(text) + 1)
    if rule.normalization in ("nfc", "nfc-casefold"):
        text = unicodedata.normalize("NFC", text)
    if rule.normalization in ("casefold", "nfc-casefold"):
        text = text.casefold()
    bound_text(text, budget)
    token = term_key(value)
    return (token[0], text, *token[2:])


def _scope(record: KeyRecord) -> tuple[object, bool]:
    """Canonicalize aliases for the same explicit selected snapshot scope."""
    name = (
        record.source.graph_name
        if record.source.graph_name is not None
        else record.graph_name
    )
    union = record.source.union or record.union
    if not record.source.is_dataset:
        return "@default", False
    if union:
        return None, True
    if name is None or name == "@default":
        return "@default", False
    return name if isinstance(name, BNode) else URIRef(str(name)), False


def _evidence(records: tuple[_Key, ...], budget: Budget) -> str:
    """Retain original values and source associations, never normalized claims."""
    data = []
    allowance = 512
    for key in records:
        name, union = _scope(key.record)
        source = key.record.source
        texts = (
            key.record.identifier,
            str(name or ""),
            source.document_iri or "",
            source.effective_url or "",
        )
        allowance += 6 * sum(len(text) for text in texts)
        allowance += 6 * sum(
            len(part or "")
            for value in (*key.values, key.record.subject)
            for part in term_key(value)
        )
        budget.consume("states", 10 + len(key.values))
        data.append(
            {
                "identifier": key.record.identifier,
                "source": {
                    "snapshot": key.source_index,
                    "document_iri": source.document_iri,
                    "effective_url": source.effective_url,
                },
                "subject": term_key(key.record.subject),
                "graph": term_key(name) if isinstance(name, URIRef | BNode) else name,
                "union": union,
                "values": [term_key(value) for value in key.values],
            }
        )
    budget.consume("work", allowance)
    budget.consume("bytes", allowance)
    text = json.dumps({"records": data}, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > budget.limits.max_bytes:
        raise LimitExceeded("Key evidence exceeds max_bytes")
    return text


def _relation(
    left: _Key, right: _Key, contract: KeyContract, budget: Budget
) -> tuple[str, tuple[Issue, ...]]:
    """A known unequal component defeats uncertainty in other components."""
    uncertain = False
    pending: list[Issue] = []
    for index, component in enumerate(contract.components):
        budget.consume("work")
        a, b = left.values[index], right.values[index]
        if isinstance(a, BNode) or isinstance(b, BNode):
            if not isinstance(a, BNode) or not isinstance(b, BNode):
                return "different", ()
            if left.record.source is not right.record.source:
                uncertain = True
                continue
            if a != b:
                return "different", ()
        elif (
            component.equality == "rdf-term"
            or not isinstance(a, Literal)
            or not isinstance(b, Literal)
        ):
            budget.consume(
                "work",
                sum(
                    len(part or "")
                    for token in (left.tokens[index], right.tokens[index])
                    for part in token
                )
                + 1,
            )
            if left.tokens[index] != right.tokens[index]:
                return "different", ()
        else:
            result = compare_literal_values(
                a, b, regime=component.equality, budget=budget
            )
            if result.report.outcome == "exhausted":
                raise LimitExceeded(
                    "Literal key comparison exhausted its shared budget"
                )
            if result.relation == "different":
                return "different", ()
            if result.relation != "equal":
                uncertain = True
                pending.extend(result.report.issues)
    return ("indeterminate" if uncertain else "equal"), tuple(pending)


def check_record_keys(
    records: Sequence[KeyRecord],
    contract: KeyContract,
    *,
    complete: bool,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Report:
    """Check supplied application keys; a partial corpus never proves uniqueness.

    Rejecting absence/multiplicity produces findings. Explicit exclusions narrow
    the declared domain; unverified policies retain uncertainty. No key implies
    sameAs, and separate records never borrow missing values from one another.
    """
    if not isinstance(contract, KeyContract):
        raise InputError("Expected a typed KeyContract")
    if not isinstance(records, Sequence) or isinstance(
        cast(object, records), str | bytes
    ):
        raise InputError("Key records require a finite typed sequence")
    if type(complete) is not bool:
        raise InputError("Declare whether the selected key corpus is complete")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Expected explicit Limits")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Explicit key limits conflict with the shared Budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    checked = 0
    exclusions: list[str] = []
    finished = True
    count = len(records)
    anchor = ""
    try:
        bound_text(contract.name, operation)
        anchor = contract.name
        operation.consume("states", count)
        captured = []
        for index in range(count):
            operation.consume("work")
            record = records[index]
            if not isinstance(record, KeyRecord):
                raise InputError("Expected typed KeyRecord bindings")
            captured.append(record)
        paths = []
        for component in contract.components:
            operation.consume("work")
            path = capture_path(component.path, budget=operation)
            steps = finite_path_steps(path, operation)
            if steps is None:
                issues.append(
                    Issue(
                        "data.key_path_unverified",
                        "Key paths require finite sequences and inverse predicates",
                        contract.name,
                        kind="unverified",
                    )
                )
            paths.append(steps)
        identifiers: set[str] = set()
        bindings: set[tuple[int, object, bool, Node]] = set()
        source_indexes: dict[int, int] = {}
        keys: list[_Key] = []
        groups: dict[tuple[tuple[str | None, ...], ...], list[_Key]] = {}
        uncertain_keys: list[_Key] = []
        semantic = any(c.equality != "rdf-term" for c in contract.components)
        for record in captured:
            operation.consume("work")
            bound_text(record.identifier, operation)
            bound_node(record.subject, operation)
            if record.graph_name is not None:
                bound_text(str(record.graph_name), operation)
            for text in (
                record.source.document_iri,
                record.source.effective_url,
                record.source.literal_fidelity,
            ):
                if text is not None:
                    bound_text(text, operation)
            selection, union = _scope(record)
            if selection is not None:
                bound_text(str(selection), operation)
            binding = (id(record.source), selection, union, record.subject)
            if record.identifier in identifiers or binding in bindings:
                raise InputError(
                    "Key records require unique identifiers and subject bindings"
                )
            operation.consume("states", 2)
            identifiers.add(record.identifier)
            bindings.add(binding)
            if id(record.source) not in source_indexes:
                operation.consume("resources")
                operation.consume("states")
                source_indexes[id(record.source)] = len(source_indexes)
            if isinstance(record.graph_name, BNode) and not any(
                name is record.graph_name
                for name in record.source._graph_names(budget=operation)
            ):
                issues.append(
                    Issue(
                        "data.key_binding_unverified",
                        "Blank graph selection lacks exact snapshot ownership",
                        record.identifier,
                        kind="unverified",
                    )
                )
                checked += 1
                continue
            graph = record.source._read(
                record.graph_name, union=record.union, budget=operation
            )
            if isinstance(record.subject, BNode):
                owned = False
                for triple in graph:
                    operation.consume("work")
                    if any(node is record.subject for node in triple):
                        owned = True
                        break
                if not owned:
                    issues.append(
                        Issue(
                            "data.key_binding_unverified",
                            "Blank record binding lacks exact snapshot ownership",
                            record.identifier,
                            kind="unverified",
                        )
                    )
                    checked += 1
                    continue
            values: list[Node] = []
            tokens: list[tuple[str | None, ...]] = []
            usable = True
            excluded = False
            for component, steps in zip(contract.components, paths, strict=True):
                operation.consume("work")
                if steps is None:
                    usable = False
                    continue
                try:
                    endpoints = path_values(
                        graph,
                        record.subject,
                        steps,
                        operation,
                        literal_identity=record.source.literal_fidelity
                        in ("source_terms", "supplied_terms"),
                    )
                except PathIdentityUnavailable:
                    issues.append(
                        Issue(
                            "literal.lexical_unverified",
                            "Key path requires exact intermediate literal identity",
                            record.identifier,
                            kind="unverified",
                        )
                    )
                    usable = False
                    continue
                if not endpoints:
                    if contract.absence == "exclude":
                        excluded = True
                    else:
                        issues.append(
                            Issue(
                                "data.key_absent",
                                "A record has no value for a required key component",
                                record.identifier,
                                kind="finding"
                                if contract.absence == "reject"
                                else "unverified",
                            )
                        )
                    usable = False
                elif len(endpoints) != 1:
                    issues.append(
                        Issue(
                            "data.key_multiple",
                            "A record has multiple RDF values for a key component",
                            record.identifier,
                            kind="finding"
                            if contract.multiplicity == "reject"
                            else "unverified",
                        )
                    )
                    usable = False
                else:
                    value = endpoints[0]
                    if isinstance(
                        value, Literal
                    ) and record.source.literal_fidelity not in {
                        "source_terms",
                        "supplied_terms",
                    }:
                        issues.append(
                            Issue(
                                "literal.lexical_unverified",
                                "Exact source literal identity was not retained",
                                record.identifier,
                                kind="unverified",
                                actual=record.source.literal_fidelity,
                            )
                        )
                        usable = False
                        continue
                    token = _token(value, component, operation)
                    if token is None:
                        issues.append(
                            Issue(
                                "data.key_normalization_unverified",
                                "Normalization requires an ordinary string literal",
                                record.identifier,
                                kind="unverified",
                            )
                        )
                        usable = False
                    else:
                        operation.consume("states", 2)
                        values.append(value)
                        tokens.append(token)
            checked += 1
            if excluded:
                operation.consume("states")
                exclusions.append(record.identifier)
            if not usable:
                continue
            key = _Key(
                record, source_indexes[id(record.source)], tuple(values), tuple(tokens)
            )
            has_blank = any(isinstance(value, BNode) for value in values)
            if semantic or has_blank:
                candidates = keys
            else:
                operation.consume(
                    "work",
                    sum(len(part or "") for token in key.tokens for part in token) + 1,
                )
                matches = groups.get(key.tokens, ())
                operation.consume("states", len(matches) + len(uncertain_keys))
                candidates = [*matches, *uncertain_keys]
            for prior in candidates:
                relation, pending = _relation(prior, key, contract, operation)
                if relation == "different":
                    continue
                issues.extend(pending)
                issues.append(
                    Issue(
                        "data.key_collision"
                        if relation == "equal"
                        else "data.key_comparison_unverified",
                        "Selected records share the declared compound key"
                        if relation == "equal"
                        else "Selected record keys cannot be distinguished",
                        contract.name,
                        kind="finding" if relation == "equal" else "unverified",
                        evidence_json=_evidence((prior, key), operation),
                    )
                )
            operation.consume("states", 2)
            keys.append(key)
            if has_blank:
                uncertain_keys.append(key)
            else:
                groups.setdefault(key.tokens, []).append(key)
        if not keys and exclusions:
            issues.append(
                Issue(
                    "data.key_corpus_unverified",
                    "No eligible record keys remain in the declared domain",
                    contract.name,
                    kind="unverified",
                )
            )
        if not complete:
            issues.append(
                Issue(
                    "data.key_corpus_unverified",
                    "The supplied record corpus is not declared complete",
                    contract.name,
                    kind="unverified",
                )
            )
    except LimitExceeded as error:
        finished = False
        issues.exhausted(str(error), location=anchor)
    return Report(
        "record-keys",
        checked,
        tuple(issues),
        requested=count,
        complete=finished,
        exclusions=tuple(exclusions),
        regime="application-keys",
        families=("application-keys",),
    )
