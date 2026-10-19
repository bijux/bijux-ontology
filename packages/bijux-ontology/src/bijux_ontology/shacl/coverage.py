"""Completed root-target coverage, separate from raw SHACL conformance."""

from dataclasses import dataclass
from typing import cast

from rdflib import URIRef
from rdflib.namespace import Namespace
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri

TARGET = Namespace("urn:bijux:shacl:coverage:")


@dataclass(frozen=True)
class ShaclCoverageContract:
    """Require distinct focus records or named (shape IRI, focus IRI) pairs.

    Coverage counts root evaluations, not nested constraint calls. Passing a
    contract with no requirements requests inspection of completed targets only.
    """

    minimum_focus: int = 0
    focus_nodes: tuple[str, ...] = ()
    pairs: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if type(self.minimum_focus) is not int or self.minimum_focus < 0:
            raise InputError("minimum_focus must be a nonnegative integer")
        if isinstance(cast(object, self.focus_nodes), (str, bytes)) or isinstance(
            cast(object, self.pairs), (str, bytes)
        ):
            raise InputError("Coverage selections require sequences")
        focus = tuple(dict.fromkeys(absolute_iri(iri) for iri in self.focus_nodes))
        pairs = []
        for pair in self.pairs:
            if isinstance(pair, (str, bytes)) or len(pair) != 2:
                raise InputError("Coverage pairs require a shape and focus IRI")
            pairs.append((absolute_iri(pair[0]), absolute_iri(pair[1])))
        object.__setattr__(self, "focus_nodes", focus)
        object.__setattr__(self, "pairs", tuple(dict.fromkeys(pairs)))


@dataclass(frozen=True)
class ShaclTarget:
    """One completed root obligation in the returned coverage graph's scope."""

    shape: Node
    focus_node: Node


@dataclass(frozen=True)
class ShaclCoverage:
    """Evaluated targets and deactivation evidence beside the coverage contract."""

    targets: tuple[ShaclTarget, ...]
    deactivated_shapes: tuple[Node, ...]
    report: Report


def read_coverage(
    value: object,
    contract: ShaclCoverageContract,
    limits: Limits,
    *,
    budget: Budget | None = None,
) -> ShaclCoverage:
    """Read bounded backend evidence and evaluate only the requested coverage."""
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("graph"), str)
        or type(value.get("complete")) is not bool
        or type(value.get("exhausted")) is not bool
    ):
        raise ResourceError("SHACL process returned invalid target coverage")
    targets: list[ShaclTarget] = []
    deactivated: tuple[Node, ...] = ()
    budget = budget if budget is not None else Budget(limits)
    issues = IssueCollector(budget)
    requested = 1 + len(contract.focus_nodes) + len(contract.pairs)
    checked = 0
    complete = value["complete"]
    try:
        graph = prepare_source(
            value["graph"], format="nt", limits=limits, budget=budget
        ).graph()
        for record in graph.subjects(TARGET.shape):
            budget.consume("work")
            budget.consume("states")
            shapes = tuple(graph.objects(record, TARGET.shape))
            focus_values = tuple(graph.objects(record, TARGET.focus))
            if len(shapes) != 1 or len(focus_values) != 1:
                raise ResourceError("SHACL process returned invalid target identity")
            targets.append(ShaclTarget(shapes[0], focus_values[0]))
        targets.sort(key=lambda item: (item.shape.n3(), item.focus_node.n3()))
        deactivated = tuple(
            sorted(
                graph.objects(TARGET.scope, TARGET.deactivated),
                key=lambda node: node.n3(),
            )
        )
        evaluated = {(target.shape, target.focus_node) for target in targets}
        focuses = {target.focus_node for target in targets}
        if len(focuses) < contract.minimum_focus:
            issues.append(
                Issue(
                    "shacl.coverage_minimum",
                    "Fewer distinct focus nodes were evaluated than required",
                    "shapes",
                    expected=str(contract.minimum_focus),
                    actual=str(len(focuses)),
                    kind="finding" if complete else "unverified",
                )
            )
        checked += int(complete)
        for iri in contract.focus_nodes:
            budget.consume("work")
            if URIRef(iri) not in focuses:
                issues.append(
                    Issue(
                        "shacl.coverage_missing",
                        "Required record has no completed root-shape evaluation",
                        iri,
                        kind="finding" if complete else "unverified",
                    )
                )
            checked += int(complete)
        for shape, focus in contract.pairs:
            budget.consume("work")
            if (URIRef(shape), URIRef(focus)) not in evaluated:
                issues.append(
                    Issue(
                        "shacl.coverage_pair_missing",
                        "Required shape/focus obligation was not evaluated",
                        focus,
                        expected=shape,
                        kind="finding" if complete else "unverified",
                    )
                )
            checked += int(complete)
        if not complete:
            issues.append(
                Issue(
                    "shacl.coverage_incomplete",
                    str(value.get("message") or "Target enumeration did not complete"),
                    "shapes",
                    kind="exhausted" if value["exhausted"] else "unverified",
                )
            )
    except LimitExceeded as error:
        issues.exhausted(str(error), location="shapes")
        complete = False
    return ShaclCoverage(
        tuple(targets),
        deactivated,
        Report(
            "shacl-target-coverage",
            checked,
            tuple(issues),
            requested=requested,
            complete=complete,
            regime="shacl-root-targets",
        ),
    )
