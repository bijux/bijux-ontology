"""Finite dataset layout checks over existing owned graph read boundaries."""

import json
from typing import cast

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import RDF
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, term_key

from .types import ContentPlacement, DatasetLayout, GraphSelector, qualify_layout

Name = URIRef | BNode | None
Triple = tuple[Node, Node, Node]


def _same(left: Node | None, right: Node | None, budget: Budget) -> bool:
    """Compare blank identity only inside its exact owned source scope."""
    budget.consume(
        "work", min(len(cast(str, left or "")), len(cast(str, right or ""))) + 1
    )
    if isinstance(left, BNode) or isinstance(right, BNode):
        return left is right
    return left == right


def _select(selector: GraphSelector, name: Name, budget: Budget) -> bool:
    """Apply bounded exact names or literal IRI boundary patterns."""
    budget.consume("work")
    if selector.names:
        return any(_same(name, candidate, budget) for candidate in selector.names)
    if selector.kind == "default":
        return name is None
    if selector.kind == "blank":
        return isinstance(name, BNode)
    if selector.kind == "iri":
        if not isinstance(name, URIRef):
            return False
        for pattern in (selector.iri_prefix, selector.iri_suffix):
            budget.consume("work", min(len(name), len(pattern or "")) + 1)
        return (
            selector.iri_prefix is None or name.startswith(selector.iri_prefix)
        ) and (selector.iri_suffix is None or name.endswith(selector.iri_suffix))
    return True


def _matches(rule: ContentPlacement, triple: Triple, budget: Budget) -> bool:
    """Select original asserted statements without class or graph entailment."""
    if not rule.predicates and not rule.types and not rule.subjects:
        return True
    subject, predicate, value = triple
    return (
        any(_same(predicate, item, budget) for item in rule.predicates)
        or predicate == RDF.type
        and any(_same(value, item, budget) for item in rule.types)
        or any(_same(subject, item, budget) for item in rule.subjects)
    )


def _term(node: Node | None) -> dict[str, object]:
    """Describe already bounded original term metadata without changing it."""
    if node is None:
        return {"kind": "default"}
    if isinstance(node, Literal):
        return {
            "kind": "literal",
            "lexical": str(node),
            "datatype": str(node.datatype) if node.datatype is not None else None,
            "language": node.language,
        }
    return {"kind": "blank" if isinstance(node, BNode) else "iri", "value": str(node)}


class _Layout:
    """Own one operation's finite evidence and cumulative source inspection."""

    def __init__(
        self, source: PreparedSource, contract: DatasetLayout, budget: Budget
    ) -> None:
        """Retain only the supplied snapshot, contract and bounded counters."""
        self.source, self.contract, self.budget = source, contract, budget
        self.issues = IssueCollector(budget)
        self.checked = 0
        self.complete = True
        self.names: list[Name] = []
        self.subject_blanks: set[int] = set()
        self.graph_blanks: set[int] = set()

    def unknown(self, message: str, anchor: str = "") -> None:
        """Keep required missing scope beside any already established findings."""
        self.complete = False
        self.issues.append(
            Issue("data.layout_unverified", message, anchor, kind="unverified")
        )

    def evidence(self, name: Name, triple: Triple | None = None) -> str:
        """Precharge bounded JSON storage before formatting graph-qualified facts."""
        nodes = (name,) if triple is None else (name, *triple)
        size = 512 + 12 * sum(
            len(text or "")
            for text in (
                self.source.document_iri,
                self.source.literal_fidelity,
                self.source.graph_fidelity,
            )
        )
        for node in nodes:
            self.budget.consume("states")
            if node is not None:
                bound_node(node, self.budget)
                size += 512 + 12 * len(cast(str, node))
                if isinstance(node, Literal):
                    size += 12 * (len(node.datatype or "") + len(node.language or ""))
        if size > self.budget.limits.max_bytes:
            raise LimitExceeded("Layout evidence exceeds max_bytes")
        self.budget.consume("work", size)
        self.budget.consume("bytes", size)
        encoded = json.dumps(
            {
                "document_iri": self.source.document_iri,
                "literal_fidelity": self.source.literal_fidelity,
                "graph_fidelity": self.source.graph_fidelity,
                "graph": _term(name),
                "statement": [_term(node) for node in triple] if triple else None,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded) > self.budget.limits.max_bytes:
            raise LimitExceeded("Layout evidence exceeds max_bytes")
        return encoded

    def selector_owned(self, selector: GraphSelector) -> bool:
        """Reject equal-labelled foreign graph selectors before RDF matching."""
        for name in selector.names:
            self.budget.consume("work")
            if isinstance(name, BNode) and id(name) not in self.graph_blanks:
                self.unknown("A blank graph selector is not an exact owned graph term")
                return False
        return True

    def counts(
        self, name: str, count: int, minimum: int, maximum: int | None, known: bool
    ) -> None:
        """Use known maxima witnesses and complete inventory for missing minima."""
        self.checked += 1
        self.budget.consume("work")
        if maximum is not None and count > maximum or known and count < minimum:
            self.issues.append(
                Issue(
                    "data.layout_count_mismatch",
                    "Selected layout count violates its declared bounds",
                    name,
                    expected="declared count bounds",
                    actual=str(count),
                )
            )
        elif not known and (count < minimum or maximum is not None):
            self.unknown(
                "Graph-inventory fidelity cannot establish complete count coverage",
                name,
            )

    def run(self) -> None:
        """Inspect one full dataset and retain exact graph contexts without copying."""
        qualify_layout(self.contract, self.budget)
        for text in (
            self.source.document_iri,
            self.source.effective_url,
            self.source.graph_name,
            self.source.graph_fidelity,
            self.source.literal_fidelity,
        ):
            if text is not None:
                bound_text(text, self.budget)
        if (
            not self.source.is_dataset
            or self.source.graph_name is not None
            or self.source.union
        ):
            self.unknown(
                "Layout checking requires an unpinned dataset with original partitions"
            )
            return
        if self.source.graph_fidelity not in ("complete", "nonempty-only"):
            raise InputError("Unknown dataset inventory fidelity")
        self.budget.consume("states")
        self.budget.consume("bytes", 16)
        self.names.append(None)
        for graph_name in self.source._graph_names(budget=self.budget):
            self.budget.consume("states")
            self.budget.consume("bytes", 16)
            self.names.append(graph_name)
            if isinstance(graph_name, BNode):
                self.budget.consume("states")
                self.budget.consume("bytes", 32)
                self.graph_blanks.add(id(graph_name))
        self.checked += 1
        self.budget.consume("states", len(self.contract.allowed))
        self.budget.consume("bytes", 16 * len(self.contract.allowed))
        allowed = [item for item in self.contract.allowed if self.selector_owned(item)]
        for requirement in self.contract.requirements:
            if not self.selector_owned(requirement.graphs):
                continue
            count = sum(
                _select(requirement.graphs, name, self.budget) for name in self.names
            )
            # The default graph always exists independently of empty named fidelity.
            known = (
                self.source.graph_fidelity == "complete"
                or requirement.graphs.names == (None,)
                or requirement.graphs.kind == "default"
            )
            self.counts(
                requirement.name, count, requirement.minimum, requirement.maximum, known
            )
        if self.contract.closed and self.source.graph_fidelity != "complete":
            self.unknown(
                "Closed graph inventory requires complete empty-graph fidelity"
            )
        self.budget.consume("states", 3 * len(self.contract.placements))
        self.budget.consume("bytes", 48 * len(self.contract.placements))
        placements = [
            rule
            for rule in self.contract.placements
            if self.selector_owned(rule.graphs)
        ]
        counts = [0] * len(placements)
        maximum_reported = [False] * len(placements)
        for name in self.names:
            self.budget.consume("work")
            if self.contract.closed and len(allowed) == len(self.contract.allowed):
                self.checked += 1
                if not any(_select(item, name, self.budget) for item in allowed):
                    self.issues.append(
                        Issue(
                            "data.layout_graph_forbidden",
                            "Graph is outside the closed allowed inventory",
                            evidence_json=self.evidence(name),
                        )
                    )
            graph = self.source._read(
                "@default" if name is None else name, budget=self.budget
            )
            self.budget.consume("states", len(placements))
            self.budget.consume("bytes", 16 * len(placements))
            inside = [_select(rule.graphs, name, self.budget) for rule in placements]
            seen: set[tuple[tuple[str | None, ...], ...]] = set()
            for triple in graph:
                self.budget.consume("triples")
                self.budget.consume("work")
                for node in triple:
                    bound_node(node, self.budget)
                # Intrinsic RDF 1.1 string typing cannot create two assertions.
                self.budget.consume("states", 4)
                self.budget.consume(
                    "bytes",
                    256
                    + 4
                    * sum(
                        len(cast(str, node))
                        + (
                            len(node.datatype or "") + len(node.language or "")
                            if isinstance(node, Literal)
                            else 0
                        )
                        for node in triple
                    ),
                )
                self.budget.consume(
                    "work", 1 + sum(len(cast(str, node)) for node in triple)
                )
                token = tuple(term_key(node) for node in triple)
                if token in seen:
                    continue
                self.budget.consume("states")
                seen.add(token)
                if isinstance(triple[0], BNode):
                    self.budget.consume("states")
                    self.subject_blanks.add(id(triple[0]))
                for index, rule in enumerate(placements):
                    self.checked += 1
                    self.budget.consume("work")
                    if not _matches(rule, triple, self.budget):
                        continue
                    if inside[index]:
                        counts[index] += 1
                        if (
                            rule.maximum is not None
                            and counts[index] == rule.maximum + 1
                        ):
                            self.counts(rule.name, counts[index], 0, rule.maximum, True)
                            maximum_reported[index] = True
                    else:
                        self.issues.append(
                            Issue(
                                "data.layout_content_misplaced",
                                "Selected asserted statement occurs outside its "
                                "declared partition",
                                rule.name,
                                evidence_json=self.evidence(name, triple),
                            )
                        )
        for index, rule in enumerate(placements):
            foreign = False
            for subject in rule.subjects:
                self.budget.consume("work")
                if (
                    isinstance(subject, BNode)
                    and id(subject) not in self.subject_blanks
                ):
                    self.unknown(
                        "A selected blank subject is not an exact owned subject term",
                        rule.name,
                    )
                    foreign = True
            if not foreign and not maximum_reported[index]:
                # Missing empty contexts cannot hide statements.
                # Populated contexts remain in nonempty-only inventory.
                self.counts(rule.name, counts[index], rule.minimum, rule.maximum, True)


def check_dataset_layout(
    source: PreparedSource,
    contract: DatasetLayout,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Report:
    """Check finite original partitions, never a union or a graph-IRI denotation.

    Complete named inventory is required for closed coverage and absent-graph
    conclusions. Known offending statements and graph/count witnesses survive a
    later unsupported scope or resource exhaustion. No parsing or public mutable
    graph exports occur here; callers prepare their dataset once beforehand.
    """
    if not isinstance(source, PreparedSource):
        raise InputError("Layout checking requires an owned PreparedSource")
    if (
        budget is not None
        and not isinstance(budget, Budget)
        or limits is not None
        and not isinstance(limits, Limits)
    ):
        raise InputError("Expected explicit layout Budget/Limits")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Layout limits conflict with the shared budget")
    operation = budget if budget is not None else Budget(limits)
    state = _Layout(source, contract, operation)
    try:
        state.run()
    except LimitExceeded:
        state.complete = False
        state.issues.exhausted("Dataset layout checking exceeded its limits")
    except InputError:
        state.complete = False
        try:
            state.issues.append(
                Issue(
                    "data.layout_input_error",
                    "Invalid dataset layout input",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            state.issues.exhausted("Dataset layout diagnostics exceeded their limits")
    return Report(
        "dataset-layout",
        state.checked,
        tuple(state.issues),
        requested=max(1, state.checked + int(not state.complete)),
        complete=state.complete,
        families=("dataset-layout",),
    )
