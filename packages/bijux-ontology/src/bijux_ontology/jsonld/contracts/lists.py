"""Owned list expectations and exact RDF member sequences from public processing."""

import json
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast

from pyoxigraph import Literal, NamedNode, RdfFormat, parse

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget
from bijux_ontology.json.paths import location
from bijux_ontology.json.values import decode_json, own_json
from bijux_ontology.jsonld.evidence.occurrences import ObservedOccurrence, ObservedUsage
from bijux_ontology.jsonld.processing.conversion import _convert_to_rdf_dataset
from bijux_ontology.rdf.identifiers import absolute_iri


@dataclass(frozen=True, init=False)
class ExpectedList:
    """One complete ordered sequence of JSON-LD RDF values, owned immutably.

    Expanded literal objects and absolute @id references express exact terms.
    Native JSON scalar conveniences use the maintained RDF conversion. Resource
    descriptions identify a member by its absolute @id; their other properties
    are not assertions about the member's description or cross-input blank nodes.
    """

    _payload: str

    def __init__(self, values: tuple[object, ...]) -> None:
        if not isinstance(values, tuple):
            raise InputError("ExpectedList values must be a tuple")
        owned = own_json(list(values))
        object.__setattr__(
            self,
            "_payload",
            json.dumps(
                owned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )

    @property
    def values(self) -> tuple[object, ...]:
        """Return independent values without exposing the owned expectation."""
        return tuple(cast(list[object], json.loads(self._payload)))

    def _read(self, budget: Budget) -> tuple[object, ...]:
        """Validate and charge the selected operation before decoding an owned copy."""
        budget.consume("work")
        size = len(self._payload.encode("utf-8"))
        if size > budget.limits.max_bytes:
            raise LimitExceeded("List expectation exceeds max_bytes")
        budget.consume("bytes", size)
        values = cast(
            list[object],
            decode_json(
                self._payload,
                max_bytes=budget.limits.max_bytes,
                max_depth=budget.limits.max_depth,
            ),
        )
        budget.consume("states", len(values))
        return tuple(values)


def _groups(
    usage: ObservedUsage, budget: Budget
) -> tuple[tuple[object, ...], ...] | None:
    """Require complete list boundaries and positions from the owned observation."""
    if not usage.occurrences:
        return None
    groups: dict[int, list[ObservedOccurrence]] = defaultdict(list)
    for item in usage.occurrences:
        budget.consume("work")
        budget.consume("states")
        if item.container != "list" or item.list_id is None or item.list_length is None:
            return None
        groups[item.list_id].append(item)
    result: list[tuple[object, ...]] = []
    for members in groups.values():
        length = members[0].list_length
        if any(item.list_length != length for item in members):
            return None
        if length == 0:
            if len(members) != 1 or not members[0].empty:
                return None
            result.append(())
            continue
        if length != len(members) or any(
            item.empty or type(item.list_position) is not int for item in members
        ):
            return None
        ordered = sorted(members, key=lambda item: cast(int, item.list_position))
        if any(item.list_position != position for position, item in enumerate(ordered)):
            return None
        values = []
        for item in ordered:
            budget.consume("work")
            size = len(item.value_json.encode("utf-8"))
            if size > budget.limits.max_bytes:
                raise LimitExceeded("Observed list member exceeds max_bytes")
            budget.consume("bytes", size)
            values.append(
                decode_json(
                    item.value_json,
                    max_bytes=budget.limits.max_bytes,
                    max_depth=budget.limits.max_depth,
                )
            )
        result.append(tuple(values))
    return tuple(result)


def _member(value: object) -> object:
    """Project a named resource's already expanded identity, not its description."""
    if isinstance(value, dict) and "@value" not in value:
        identifier = value.get("@id")
        if not isinstance(identifier, str) or identifier.startswith("_:"):
            raise InputError(
                "Anonymous member identity cannot be matched across inputs"
            )
        absolute_iri(identifier)
        return {"@id": identifier}
    return value


def _terms(
    groups: tuple[tuple[object, ...], ...],
    budget: Budget,
) -> tuple[tuple[str, ...], ...]:
    """Convert all members once and retain one exact RDF term per owned slot."""
    root = "urn:bijux:list-expectation:members"
    prefix = root + ":"
    document: dict[str, object] = {"@id": root}
    slots: list[tuple[str, ...]] = []
    for group in groups:
        keys = []
        for value in group:
            budget.consume("work")
            key = prefix + str(len(document))
            document[key] = _member(value)
            keys.append(key)
        slots.append(tuple(keys))
    if len(document) == 1:
        return tuple(() for _ in groups)
    converted = _convert_to_rdf_dataset(document, budget=budget)
    if converted.report.outcome == "exhausted":
        raise LimitExceeded(
            "; ".join(issue.message for issue in converted.report.issues)
        )
    if not converted.report.verified:
        raise InputError(
            "RDF term preservation was not established for the list members"
        )
    if len(converted.nquads.encode("utf-8")) > budget.limits.max_bytes:
        raise LimitExceeded("Converted list members exceed max_bytes")
    terms: dict[str, list[str]] = defaultdict(list)
    for quad in parse(input=converted.nquads, format=RdfFormat.N_QUADS):
        budget.consume("work")
        budget.consume("states")
        if quad.subject != NamedNode(root):
            continue
        term = quad.object
        if isinstance(term, NamedNode):
            term_key: tuple[str | None, ...] = ("iri", term.value)
        elif isinstance(term, Literal):
            term_key = (
                "literal",
                term.value,
                term.datatype.value,
                term.language.lower() if term.language is not None else None,
            )
        else:
            raise InputError(
                "Anonymous member identity cannot be matched across inputs"
            )
        encoded = json.dumps(term_key, ensure_ascii=False)
        budget.consume("bytes", len(encoded.encode("utf-8")))
        terms[quad.predicate.value].append(encoded)
    result = []
    for group in slots:
        values = []
        for key in group:
            budget.consume("work")
            if len(terms[key]) != 1:
                raise InputError("Each list member must establish exactly one RDF term")
            values.append(terms[key][0])
        result.append(tuple(values))
    return tuple(result)


def compare_lists(
    expected: tuple[ExpectedList, ...],
    usage: ObservedUsage,
    *,
    budget: Budget,
) -> Iterator[Issue]:
    """Compare list boundaries, ordered members and unordered outer occurrences."""
    budget.consume("states", len(expected))
    wanted = tuple(item._read(budget) for item in expected)
    yield from compare_list_values(wanted, usage, budget=budget)


def compare_list_values(
    wanted: tuple[tuple[object, ...], ...],
    usage: ObservedUsage,
    *,
    budget: Budget,
) -> Iterator[Issue]:
    """Compare already-owned member groups through the same RDF term conversion."""
    where = location(usage.path)
    if any(item.container != "list" for item in usage.occurrences):
        yield Issue(
            "context.list_group_mismatch", "Selected values are not RDF lists", where
        )
        return
    actual = _groups(usage, budget)
    if actual is None:
        yield Issue(
            "context.list_unverified",
            "Complete list boundaries were not established",
            where,
            kind="unverified",
        )
        return
    try:
        sequences = _terms((*wanted, *actual), budget)
    except LimitExceeded:
        raise
    except InputError as error:
        yield Issue("context.list_unverified", str(error), where, kind="unverified")
        return
    left = Counter(sequences[: len(wanted)])
    right = Counter(sequences[len(wanted) :])
    budget.consume("work", len(left) + len(right))
    if left == right:
        return
    unordered_left = Counter(
        tuple(sorted(sequence)) for sequence in sequences[: len(wanted)]
    )
    unordered_right = Counter(
        tuple(sorted(sequence)) for sequence in sequences[len(wanted) :]
    )
    code = (
        "context.list_order_mismatch"
        if unordered_left == unordered_right
        else "context.list_group_mismatch"
    )
    left_witness = min(left - right, default=())
    right_witness = min(right - left, default=())
    expected_text = f"{len(wanted)} lists; unmatched members: {left_witness!r}"
    actual_text = f"{len(actual)} lists; unmatched members: {right_witness!r}"
    budget.consume(
        "bytes", len(expected_text.encode("utf-8")) + len(actual_text.encode("utf-8"))
    )
    yield Issue(
        code,
        "Expanded lists differ in per-list order"
        if unordered_left == unordered_right
        else "Expanded lists differ in members, multiplicity or grouping",
        where,
        expected=expected_text,
        actual=actual_text,
    )
