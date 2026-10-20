"""Conservative shared-value joins for finite named-individual OWL keys."""

from collections.abc import Iterator

from rdflib import Literal, URIRef
from rdflib.namespace import OWL

from .context import AssertionContext, _Row
from .indexing import PropertyRows
from .patterns import KeyPattern, Resource
from .types import Statement, TermKey


class KeyJoin:
    """Select only eligible subject pairs sharing an actual value per property.

    Resource values use established individual classes. Data values remain a
    conservative candidate family: the authoritative datatype comparison must
    decide equality, including unsupported and non-transitive profiles.
    """

    def __init__(self, state: AssertionContext, key: KeyPattern) -> None:
        self.state = state
        self.members: list[tuple[Resource, tuple[Statement, ...]]] = []
        retained: set[TermKey] = set()
        for subject, actual, premise in state.types:
            state.operation.consume("work")
            ancestry = state.member(actual, key.klass)
            named = state.named_proof(subject)
            if ancestry is not None and named is not None:
                self._member(subject, (premise, *ancestry, *named), retained)
        if key.klass == OWL.Thing:
            for predicate in key.properties:
                for row in state.rows(predicate):
                    state.operation.consume("work")
                    named = state.named_proof(row.subject)
                    if named is not None:
                        self._member(row.subject, (row.premise, *named), retained)
        classes: dict[TermKey, list[int]] = {}
        for index, (subject, _) in enumerate(self.members):
            state.operation.consume("states")
            state.operation.reserve_storage(128)
            representative = state.identity.find(state.key(subject))
            classes.setdefault(representative, []).append(index)
        self.rows: dict[URIRef, PropertyRows] = {}
        self.values: list[dict[int, set[TermKey]]] = []
        self.subjects: list[dict[TermKey, set[int]]] = []
        for predicate in key.properties:
            kind, _ = state.kind(predicate)
            if kind is None:
                state.unknown(
                    "A key property lacks an unambiguous object/data declaration"
                )
            indexed = state.indexed_rows(predicate)
            self.rows[predicate] = indexed
            by_subject: dict[int, set[TermKey]] = {}
            by_value: dict[TermKey, set[int]] = {}
            self.values.append(by_subject)
            self.subjects.append(by_value)
            state.operation.consume("states", 3)
            state.operation.reserve_storage(384)
            for row in state.rows(predicate):
                representative = state.identity.find(state.key(row.subject))
                members = classes.get(representative, ())
                value: TermKey
                if isinstance(row.value, Literal):
                    value = ("semantic-literal",)
                else:
                    value = state.identity.find(state.key(row.value))
                for member in members:
                    state.operation.consume("work")
                    state.operation.consume("states", 2)
                    state.operation.reserve_storage(256)
                    by_subject.setdefault(member, set()).add(value)
                    by_value.setdefault(value, set()).add(member)

    def _member(
        self, subject: Resource, proof: tuple[Statement, ...], retained: set[TermKey]
    ) -> None:
        token = self.state.identity.find(self.state.key(subject))
        if token not in retained:
            self.state.operation.consume("states", 2 + len(proof))
            self.state.operation.reserve_storage(128 + 16 * len(proof))
            retained.add(token)
            self.members.append((subject, proof))

    def candidates(self, position: int) -> Iterator[int]:
        """Intersect every required property, starting with its smallest join."""
        selected: tuple[dict[TermKey, set[int]], set[TermKey]] | None = None
        smallest: int | None = None
        for values, subjects in zip(self.values, self.subjects, strict=True):
            actual = values.get(position)
            if not actual:
                return
            size = sum(len(subjects[value]) for value in actual)
            self.state.operation.consume("work", len(actual))
            if smallest is None or size < smallest:
                smallest = size
                selected = subjects, actual
        if selected is None:
            return
        subjects, actual = selected
        seen: set[int] = set()
        for value in actual:
            matches = subjects[value]
            self.state.operation.consume(
                "work", len(matches) * max(1, len(matches).bit_length())
            )
            self.state.operation.consume("states", len(matches))
            self.state.operation.reserve_storage(16 * len(matches))
            for candidate in sorted(matches):
                self.state.operation.consume("work")
                if candidate <= position or candidate in seen:
                    continue
                self.state.operation.consume("states")
                self.state.operation.reserve_storage(64)
                seen.add(candidate)
                matched = True
                for values in self.values:
                    first = values[position]
                    second = values.get(candidate)
                    self.state.operation.consume(
                        "work", len(first) + (len(second) if second else 0)
                    )
                    if second is None or first.isdisjoint(second):
                        matched = False
                        break
                if matched:
                    yield candidate

    def subject_rows(self, predicate: URIRef, subject: Resource) -> list[_Row]:
        """Read actual original assertions of one established subject class."""
        representative = self.state.identity.find(self.state.key(subject))
        return self.rows[predicate].subjects.get(representative, [])

    def value_rows(
        self, predicate: URIRef, subject: Resource, value: Resource
    ) -> list[_Row]:
        """Select object fillers only after both endpoint identities are proved."""
        first = self.state.identity.find(self.state.key(subject))
        second = self.state.identity.find(self.state.key(value))
        return self.rows[predicate].edges.get((first, second), [])
