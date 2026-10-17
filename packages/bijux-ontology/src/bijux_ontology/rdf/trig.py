"""Maintained TriG graph-declaration inventory and lossless empty-graph writing."""

from collections.abc import MutableSequence
from io import BytesIO
from typing import Any

from rdflib import BNode, Dataset, Graph, URIRef
from rdflib.graph import DATASET_DEFAULT_GRAPH_ID
from rdflib.plugins.parsers.notation3 import RDFSink
from rdflib.plugins.parsers.trig import TrigSinkParser
from rdflib.plugins.serializers.trig import TrigSerializer
from rdflib.term import Identifier, Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget


class _InventorySink(RDFSink):
    """Observe maintained parser events without retaining its normalized triples."""

    def __init__(self, budget: Budget) -> None:
        super().__init__(Graph(bind_namespaces="none"))
        self.budget = budget
        self.declarations: dict[Identifier, bool] = {}
        self.labels: dict[BNode, str] = {}

    def newGraph(self, identifier: Identifier) -> Graph:
        self.budget.consume("work")
        if identifier == DATASET_DEFAULT_GRAPH_ID:
            raise InputError("Named graph uses the reserved default graph identifier")
        if identifier not in self.declarations:
            self.budget.consume("resources")
            self.budget.consume("states")
            self.declarations[identifier] = False
        return super().newGraph(identifier)

    def newBlankNode(
        self, arg: Any = None, uri: str | None = None, why: Any = None
    ) -> BNode:
        self.budget.consume("work")
        self.budget.consume("states")
        return super().newBlankNode(arg, uri, why)

    def makeStatement(self, quadruple: Any, why: Any = None) -> None:
        self.budget.consume("work")
        context = quadruple[0]
        if isinstance(context, Graph):
            self.declarations[context.identifier] = True


class _InventoryParser(TrigSinkParser):
    """Retain explicit source-label witnesses and bound recursive node parsing."""

    def __init__(self, sink: _InventorySink, base: str | None) -> None:
        super().__init__(sink, baseURI=base, turtle=True)
        self.sink = sink
        self.depth = 0

    def directiveOrStatement(self, argstr: str, i: int) -> int:
        self.sink.budget.consume("work")
        return super().directiveOrStatement(argstr, i)

    def bind(self, qn: str, uri: bytes) -> None:
        self.sink.budget.consume("states")
        super().bind(qn, uri)

    def anonymousNode(self, label: str) -> BNode:
        self.sink.budget.consume("work")
        if label not in self._anonymousNodes:
            self.sink.budget.consume("states")
        node = super().anonymousNode(label)
        self.sink.labels[node] = label
        return node

    def node(
        self,
        argstr: str,
        i: int,
        res: MutableSequence[Any],
        subjectAlready: Node | None = None,
    ) -> int:
        self.sink.budget.consume("work")
        self.depth += 1
        try:
            if self.depth > self.sink.budget.limits.max_depth:
                raise LimitExceeded("TriG inventory exceeds max_depth")
            return super().node(argstr, i, res, subjectAlready)
        finally:
            self.depth -= 1


def retain_trig_inventory(
    payload: bytes,
    dataset: Dataset,
    nodes: dict[str, BNode],
    *,
    base: str | None,
    budget: Budget,
) -> bool:
    """Complete one primary parse using graph events from the same byte document.

    Only explicit blank labels establish identity between parser passes. Existing
    anonymous populated graphs remain owned by the primary parser; an anonymous
    empty graph is fresh and cannot share an authored label with another term.
    No secondary literal or triple is copied into the result. Return False when
    the maintained metadata parser does not support otherwise valid primary RDF;
    no partial inventory is then copied. Limits and representation errors raise.
    """
    sink = _InventorySink(budget)
    try:
        _InventoryParser(sink, base).loadBuf(payload)
    except (InputError, LimitExceeded):
        raise
    except RecursionError as error:
        raise LimitExceeded(
            "TriG inventory exceeds parser nesting allowance"
        ) from error
    except MemoryError as error:
        raise LimitExceeded(
            "TriG inventory exceeds parser allocation allowance"
        ) from error
    except (SyntaxError, ValueError):
        return False
    for name, populated in sink.declarations.items():
        budget.consume("work")
        if name == sink.graph.identifier:
            continue
        if isinstance(name, URIRef):
            dataset.graph(name)
        elif isinstance(name, BNode):
            label = sink.labels.get(name)
            if label is not None:
                dataset.graph(nodes.setdefault(label, BNode()))
            elif not populated:
                dataset.graph(BNode())
        else:
            raise InputError("TriG graph names must be IRIs or blank nodes")
    return True


class _InventorySerializer(TrigSerializer):
    """Preserve blank graph names also used as subject/object terms."""

    def preprocess(self) -> None:
        super().preprocess()
        for context in self.contexts:
            if isinstance(context.identifier, BNode):
                self._references[context.identifier] += 2


class _BoundedOutput(BytesIO):
    """Stop maintained serializer output before retaining more than its cap."""

    def __init__(self, cap: int) -> None:
        super().__init__()
        self.cap = cap

    def write(self, data: Any) -> int:
        if self.tell() + len(data) > self.cap:
            raise LimitExceeded("RDF export exceeds max_bytes")
        return super().write(data)


def serialize_trig(dataset: Dataset, *, budget: Budget) -> bytes:
    """Serialize maintained TriG and explicit empty declarations without triples."""
    empty: list[URIRef | BNode] = []
    for graph in dataset.graphs():
        budget.consume("work")
        budget.consume("resources")
        if graph.identifier != dataset.default_graph.identifier and not graph:
            if not isinstance(graph.identifier, (URIRef, BNode)):
                raise InputError("TriG graph names must be IRIs or blank nodes")
            empty.append(graph.identifier)
    with _BoundedOutput(budget.limits.max_bytes) as stream:
        _InventorySerializer(dataset).serialize(stream, encoding="utf-8")
        for name in empty:
            stream.write((name.n3() + " {}\n").encode())
        return stream.getvalue()
