"""Transactional RDF edit declarations and explicit correspondence witnesses."""

from dataclasses import dataclass

from rdflib import BNode, URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.records import types as records


@dataclass(frozen=True)
class DatasetPatch:
    """Reviewable edits with mandatory complete structural pre/postconditions."""

    base: records.SnapshotWitness
    target: records.SnapshotWitness
    removals: tuple[records.PatchQuad, ...] = ()
    additions: tuple[records.PatchQuad, ...] = ()
    removed_graphs: tuple[records.PatchResource, ...] = ()
    added_graphs: tuple[records.PatchResource, ...] = ()

    def __post_init__(self) -> None:
        """Require immutable typed edits; application still verifies all conditions."""
        budget = Budget()
        records.validate_witness(self.base, budget)
        records.validate_witness(self.target, budget)
        if self.base.kind != self.target.kind:
            raise InputError("A patch cannot widen or change its snapshot scope")
        for values, expected in (
            (self.removals, records.PatchQuad),
            (self.additions, records.PatchQuad),
            (self.removed_graphs, URIRef | records.PatchBlank),
            (self.added_graphs, URIRef | records.PatchBlank),
        ):
            records._inventory(values, expected, budget)
            if len(set(values)) != len(values):
                raise InputError("Patch edit inventories cannot contain duplicates")
        if set(self.removals) & set(self.additions) or set(self.removed_graphs) & set(
            self.added_graphs
        ):
            raise InputError("A patch cannot both add and remove the same record")


@dataclass(frozen=True)
class BlankNodePair:
    """Declare correspondence between exact owned nodes in two input snapshots."""

    before: BNode
    after: BNode

    def __post_init__(self) -> None:
        """Reject parser-label strings masquerading as ownership evidence."""
        if not isinstance(self.before, BNode) or not isinstance(self.after, BNode):
            raise InputError("Blank correspondence requires actual BNode objects")


@dataclass(frozen=True)
class PatchBlankBinding:
    """Bind one base token to an exact owned node for this application."""

    token: str
    node: BNode

    def __post_init__(self) -> None:
        """Own bounded token text while preserving the actual node witness."""
        object.__setattr__(self, "token", records.PatchBlank(self.token).token)
        if not isinstance(self.node, BNode):
            raise InputError("Patch blank bindings require actual BNode objects")


@dataclass(frozen=True)
class PatchPlan:
    """A complete portable patch or required evidence explaining its absence."""

    patch: DatasetPatch | None
    report: Report
