"""Shared streaming extraction of exact scoped application-key records."""

import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from rdflib import BNode, Literal
from rdflib.namespace import RDF, XSD
from rdflib.term import Node

from bijux_ontology.data.records.reading import RecordReader, read_records
from bijux_ontology.declarations.keys import KeyComponent, KeyContract
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget, IssueCollector, bound_text
from bijux_ontology.rdf.paths.traversal import (
    finite_path_steps,
)
from bijux_ontology.rdf.paths.types import capture_path
from bijux_ontology.rdf.terms import bound_node, term_key

from .types import KeyRecord


@dataclass(frozen=True)
class _Key:
    """Original associated values beside their qualified comparison tokens."""

    record: KeyRecord
    source_index: int
    values: tuple[Node, ...]
    tokens: tuple[tuple[str | None, ...], ...]
    reader: RecordReader


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


def _scope(reader: RecordReader) -> tuple[object, bool]:
    """Canonicalize key aliases from the qualified original source scope."""
    reader.selected.read()
    if not reader.metadata.is_dataset:
        return "@default", False
    if reader.selected.kind == "union":
        return None, True
    name = reader.selected.graph_name
    return name if name is not None else "@default", False


@dataclass
class _KeyExtraction:
    """Retain prefix progress while callers consume keys and prove their own facts."""

    records: Sequence[KeyRecord]
    contract: KeyContract
    operation: Budget
    issues: IssueCollector
    count: int
    checked: int = 0
    exclusions: list[str] = field(default_factory=list)

    def __iter__(self) -> Iterator[_Key]:
        """Extract once without delaying a caller's already provable findings."""
        records, contract, operation, issues, count = (
            self.records,
            self.contract,
            self.operation,
            self.issues,
            self.count,
        )
        operation.consume("states", count)
        captured = []
        for index in range(count):
            operation.consume("work")
            try:
                record = records[index]
            except IndexError as error:
                raise InputError("Key records changed during capture") from error
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
            if record.identifier in identifiers:
                raise InputError(
                    "Key records require unique identifiers and subject bindings"
                )
            operation.consume("states", 2)
            identifiers.add(record.identifier)
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
                self.checked += 1
                continue
            reader = read_records(
                record.source,
                operation,
                graph_name=record.graph_name,
                union=record.union,
            )
            if (
                isinstance(record.subject, BNode)
                and not reader.presence(record.subject, operation).owned
            ):
                issues.append(
                    Issue(
                        "data.key_binding_unverified",
                        "Blank record binding lacks exact snapshot ownership",
                        record.identifier,
                        kind="unverified",
                    )
                )
                self.checked += 1
                continue
            selection, union = _scope(reader)
            if selection is not None:
                bound_text(str(selection), operation)
            binding = (id(record.source), selection, union, record.subject)
            if binding in bindings:
                raise InputError(
                    "Key records require unique identifiers and subject bindings"
                )
            bindings.add(binding)
            values: list[Node] = []
            tokens: list[tuple[str | None, ...]] = []
            usable = True
            excluded = False
            for component, steps in zip(contract.components, paths, strict=True):
                operation.consume("work")
                if steps is None:
                    usable = False
                    continue
                observed = reader.values(record.subject, steps, operation)
                if not observed.complete:
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
                endpoints = observed.values
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
                    if isinstance(value, Literal) and not observed.exact_literals:
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
            self.checked += 1
            if excluded:
                operation.consume("states")
                self.exclusions.append(record.identifier)
            if not usable:
                continue
            operation.consume("states")
            operation.consume("bytes", 16)
            yield _Key(
                record,
                source_indexes[id(record.source)],
                tuple(values),
                tuple(tokens),
                reader,
            )
