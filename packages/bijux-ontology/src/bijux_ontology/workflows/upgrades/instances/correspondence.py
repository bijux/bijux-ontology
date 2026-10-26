"""Bounded monotonic anonymous correspondence for one qualified release pair."""

from itertools import chain

from rdflib import BNode

from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.patches.correspondence import compare_witnesses
from bijux_ontology.rdf.patches.planning import Captured, anonymous, replace_witness
from bijux_ontology.rdf.patches.types import BlankNodePair, DatasetPatch
from bijux_ontology.rdf.records.types import (
    PatchBlank,
    PatchUnavailable,
    PatchValue,
    SnapshotWitness,
)

from .observations import EntityKey


def _reserve(budget: Budget, count: int = 1) -> None:
    """Prepay owned result/container references before constructing them."""
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)


def _normalize(
    values: tuple[PatchValue, ...],
    mapping: dict[PatchBlank, PatchBlank],
    budget: Budget,
) -> tuple[PatchValue, ...]:
    """Apply only established blank identity; exact IRI/literal spelling is retained."""
    _reserve(budget, len(values))
    budget.consume("work", len(values))
    return tuple(
        mapping.get(value, value) if isinstance(value, PatchBlank) else value
        for value in values
    )


class ReleaseCorrespondence:
    """Own shared identity evidence without turning field policy into history.

    Initial declared pairs may gain uniquely proved anonymous correspondences
    during field observation. Every later observation sees that same monotonic
    mapping; symmetric structures never establish a historical pairing.
    """

    __slots__ = (
        "_left",
        "_right",
        "_patch",
        "_mapping",
        "_created",
        "_deleted",
        "_operation",
        "_anonymous_edits",
    )

    def __init__(
        self,
        left: Captured,
        right: Captured,
        patch: DatasetPatch,
        pairs: tuple[BlankNodePair, ...],
        created: tuple[BNode, ...],
        deleted: tuple[BNode, ...],
        operation: Operation,
    ) -> None:
        operation.consume("states")
        operation.reserve_storage(256)
        mapping: dict[PatchBlank, PatchBlank] = {}
        for pair in pairs:
            _reserve(operation)
            mapping[right.reference(pair.after)] = left.reference(pair.before)
        _reserve(operation, len(deleted) + len(created))
        deleted_refs = {left.reference(node) for node in deleted}
        created_refs = {right.reference(node) for node in created}
        if not left.owned:
            _reserve(operation, len(right.owned))
            created_refs.update(ref for _, ref in right.owned.values())
        if not right.owned:
            _reserve(operation, len(left.owned))
            deleted_refs.update(ref for _, ref in left.owned.values())
        self._left, self._right, self._patch = left, right, patch
        self._mapping, self._created, self._deleted = (
            mapping,
            created_refs,
            deleted_refs,
        )
        self._operation = operation
        self._anonymous_edits: bool | None = None

    def require_entity_pair(self, before: EntityKey, after: EntityKey) -> None:
        """Require original subject and physical-graph identity for anonymous pairs."""
        if (isinstance(before[0], PatchBlank) or isinstance(after[0], PatchBlank)) and (
            not isinstance(before[0], PatchBlank)
            or not isinstance(after[0], PatchBlank)
            or self._mapping.get(after[0]) != before[0]
        ):
            raise PatchUnavailable(
                "Blank entities require established dataset patch correspondence"
            )
        if (isinstance(before[1], PatchBlank) or isinstance(after[1], PatchBlank)) and (
            not isinstance(before[1], PatchBlank)
            or not isinstance(after[1], PatchBlank)
            or self._mapping.get(after[1]) != before[1]
        ):
            raise PatchUnavailable(
                "Blank graph identity requires dataset patch correspondence"
            )

    def normalize_entity_key(self, key: EntityKey) -> EntityKey:
        """Apply only already established identity to a selected entity and graph."""
        _reserve(self._operation, 2)
        return (
            self._mapping.get(key[0], key[0])
            if isinstance(key[0], PatchBlank)
            else key[0],
            self._mapping.get(key[1], key[1])
            if isinstance(key[1], PatchBlank)
            else key[1],
        )

    def requires_removal_evidence(self, key: EntityKey) -> bool:
        """Keep unexplained anonymous disappearance distinct from entity removal."""
        return any(
            isinstance(part, PatchBlank) and part not in self._deleted for part in key
        )

    @property
    def anonymous_edits(self) -> bool:
        """Read the fixed exact patch once, without inventing anonymous matches."""
        if self._anonymous_edits is None:
            self._anonymous_edits = any(
                isinstance(term, PatchBlank)
                for quad in chain(self._patch.removals, self._patch.additions)
                for term in (quad.subject, quad.value, quad.graph)
            )
        return self._anonymous_edits

    def normalized_witness(self) -> SnapshotWitness:
        """Take the current exact target projection for the caller's selected rule."""
        return replace_witness(self._right.witness, self._mapping, self._operation)

    def normalize_field(
        self, previous: tuple[PatchValue, ...], current: tuple[PatchValue, ...]
    ) -> tuple[PatchValue, ...]:
        """Retain bounded proved enrichment before later selected fields run."""
        _reserve(self._operation, len(previous) + len(self._mapping))
        unresolved = (
            {value.token for value in previous if isinstance(value, PatchBlank)}
            - {value.token for value in self._mapping.values()}
            - {value.token for value in self._deleted}
        )
        if unresolved and not self.anonymous_edits:
            try:
                _, established = compare_witnesses(
                    anonymous(self._left.witness, self._operation),
                    anonymous(self._right.witness, self._operation),
                    self._operation,
                    required=tuple(unresolved),
                    fixed=tuple((v.token, k.token) for k, v in self._mapping.items()),
                )
                for old_token, new_token in established.items():
                    _reserve(self._operation)
                    self._mapping[PatchBlank(new_token)] = PatchBlank(old_token)
            except PatchUnavailable:
                pass
        normalized = _normalize(current, self._mapping, self._operation)
        unknown_blank = any(
            isinstance(value, PatchBlank)
            and value not in self._mapping.values()
            and value not in self._deleted
            for value in previous
        ) or any(
            isinstance(value, PatchBlank)
            and value not in self._mapping
            and value not in self._created
            for value in current
        )
        if unknown_blank:
            raise PatchUnavailable(
                "Selected anonymous endpoint identity is not established"
            )
        return normalized
