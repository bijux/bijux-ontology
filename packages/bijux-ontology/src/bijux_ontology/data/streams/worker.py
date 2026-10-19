"""Owned native stream parsing and existing per-statement validation only."""

import hashlib
import json
import re
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import IO, Any, BinaryIO, cast

import pyoxigraph as ox
from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology._json import decode_json
from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.errors import (
    InputError,
    LimitExceeded,
    ResourceError,
    attach_input_failure,
)
from bijux_ontology.execution.budgets import ControlBudget
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import IssueCollector, Limits
from bijux_ontology.rdf.literals import check_literal
from bijux_ontology.rdf.queries.protocol import QueryTerm, term_data
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.stores.native import DeclarationReader
from bijux_ontology.rdf.terms import bound_node, bound_text, oxigraph_term
from bijux_ontology.reports import Issue, Location, Report
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.types import OwnedSource
from bijux_ontology.vocabulary.sources.view import Vocabulary

from .protocol import read_stored

_LINES = re.compile(rb"[\r\n]")


class _Reader:
    """Bound actual reads and physical records without interpreting RDF syntax."""

    def __init__(self, stream: BinaryIO, budget: ControlBudget, maximum: int) -> None:
        """Retain only fixed reader state and a raw observed-byte digest."""
        self.stream, self.budget, self.maximum = stream, budget, maximum
        self.count = self.record_size = 0
        self.eof = False
        self.pending_record_limit = False
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        """Return bounded short reads; only the maintained parser decides syntax."""
        self.budget.consume("work")
        if self.pending_record_limit:
            raise LimitExceeded("RDF stream physical record exceeds max_bytes")
        remaining = self.maximum - self.count
        amount = min(
            65536,
            remaining if remaining else 1,
            size if size >= 0 else 65536,
            self.budget.limits.max_bytes,
            self.budget.remaining("bytes"),
            self.budget.remaining("work"),
        )
        if not amount:
            raise LimitExceeded("RDF stream exhausted its read allowance")
        chunk = self.stream.read(amount)
        if not chunk:
            self.eof = True
            return b""
        self.count += len(chunk)
        if len(chunk) > amount or self.count > self.maximum:
            raise LimitExceeded("RDF stream exceeds its total byte allowance")
        self.budget.consume("work", len(chunk))
        self.budget.consume("bytes", len(chunk))
        self.digest.update(chunk)
        previous = 0
        for match in _LINES.finditer(chunk):
            self.record_size += match.start() - previous
            if self.record_size > self.budget.limits.max_bytes:
                self.pending_record_limit = True
                if previous:
                    return chunk[:previous]
                raise LimitExceeded("RDF stream physical record exceeds max_bytes")
            self.record_size = 0
            previous = match.end()
        self.record_size += len(chunk) - previous
        if self.record_size > self.budget.limits.max_bytes:
            self.pending_record_limit = True
            if previous:
                return chunk[:previous]
            raise LimitExceeded("RDF stream physical record exceeds max_bytes")
        return chunk


def _term(value: object, budget: ControlBudget) -> QueryTerm | None:
    """Convert one bounded term with existing lexical-preserving RDF machinery."""
    budget.consume("states")
    budget.consume("bytes", 64)
    if isinstance(value, ox.DefaultGraph):
        return None
    if not isinstance(value, ox.NamedNode | ox.BlankNode | ox.Literal):
        raise InputError("Stream validation supports ordinary RDF terms only")
    bound_text(value.value, budget)
    if isinstance(value, ox.Literal):
        bound_text(value.datatype.value, budget)
        bound_text(value.language or "", budget)
    # One parser owns these IDs. No all-data blank map is retained; the existing
    # result codec binds retained occurrences jointly within this stream only.
    node = (
        BNode(value.value)
        if isinstance(value, ox.BlankNode)
        else oxigraph_term(value, {}, True)
    )
    bound_node(node, budget)
    return cast(QueryTerm, node)


def _projected_vocabulary(graph: Graph, budget: ControlBudget) -> Vocabulary:
    """Adopt a tiny derived selection without acquiring another input resource."""
    budget.consume("states", 3)
    budget.consume("bytes", 192)
    dataset = Dataset()
    for triple in graph:
        budget.consume("work")
        budget.consume("triples")
        budget.consume("states")
        budget.consume("bytes", 88)
        dataset.default_graph.add(triple)
    prepared = PreparedSource._from_owned(
        dataset,
        is_dataset=False,
        document_iri=None,
        effective_url=None,
        literal_fidelity="supplied_terms",
        max_triples=budget.limits.max_triples,
    )
    selected = prepared._read(budget=budget)
    return Vocabulary._from_owned(
        OwnedSource(prepared, selected),
        (),
        (),
        budget.limits,
    )


def evaluate(request: dict[str, Any]) -> dict[str, object]:
    """Read through EOF or return a bounded completed diagnostic prefix."""
    budget = ControlBudget(Limits(**request["limits"]), request["allowances"])
    held = IssueCollector(budget)
    records: list[dict[str, object]] = []
    parse_issues: list[dict[str, object]] = []
    used = 2048
    statements = selected = checks = 0
    complete = False
    reader = None
    stopped = ""
    vocabulary = None
    declarations_reader = None

    def keep(issue: Issue, statement: dict[str, object] | None) -> None:
        """Qualify each wire record before it can enter the returned prefix."""
        nonlocal used
        if issue.kind == "exhausted":
            raise LimitExceeded(issue.message)
        held.append(issue)
        item: dict[str, object] = {"issue": issue.to_dict()}
        if statement is not None:
            item["statement"] = statement
        amount = json_bound(item, budget)
        budget.consume("bytes", amount)
        encoded_size = len(
            json.dumps(item, ensure_ascii=True, separators=(",", ":")).encode()
        )
        if used + encoded_size > request["output_cap"]:
            raise LimitExceeded(
                "Stream diagnostics exceed the reserved output allowance"
            )
        budget.consume("states")
        used += encoded_size
        (records if statement is not None else parse_issues).append(item)

    def adopt(report: Report, statement: dict[str, object]) -> None:
        """Retain original validator findings without duplicating issue slots."""
        nonlocal checks
        checks += report.checked
        for issue in report.issues:
            keep(issue, statement)

    try:
        with ExitStack() as ownership:
            budget.consume("states", len(request["exclude"]))
            for excluded_iri in request["exclude"]:
                bound_text(excluded_iri, budget)
            excluded = frozenset(request["exclude"])
            if request["stored"] is not None:
                stored, scope = read_stored(request["stored"], budget)
                declarations_reader = ownership.enter_context(
                    DeclarationReader(stored, scope=scope, budget=budget)
                )
            else:
                graph = Graph(bind_namespaces="none")
                for iri, kinds in request["vocabulary"]:
                    bound_text(iri, budget)
                    for kind in kinds:
                        budget.consume("work")
                        budget.consume("triples")
                        budget.consume("states")
                        budget.consume("bytes", 88)
                        bound_text(kind, budget)
                        graph.add((URIRef(iri), RDF.type, URIRef(kind)))
                vocabulary = load_vocabulary(graph, budget=budget)
                vocabulary._kind_index(("local", "imports", "lookup"), budget)
            with Path(request["path"]).open("rb") as stream:
                reader = _Reader(stream, budget, request["max_stream_bytes"])
                parser = ox.parse(
                    input=cast(IO[bytes], reader),
                    format=ox.RdfFormat.N_TRIPLES
                    if request["syntax"] == "nt"
                    else ox.RdfFormat.N_QUADS,
                )
                for quad in parser:
                    budget.consume("triples")
                    budget.consume("work")
                    statements += 1
                    if request["graph"] is not None:
                        matches = (
                            isinstance(quad.graph_name, ox.DefaultGraph)
                            if request["graph"] == "@default"
                            else isinstance(quad.graph_name, ox.NamedNode)
                            and quad.graph_name.value == request["graph"]
                        )
                        if not matches:
                            continue
                    selected += 1
                    terms = tuple(
                        _term(term, budget)
                        for term in (
                            quad.subject,
                            quad.predicate,
                            quad.object,
                            quad.graph_name,
                        )
                    )
                    subject, predicate, value, _ = terms
                    if (
                        not isinstance(subject, URIRef | BNode)
                        or not isinstance(predicate, URIRef)
                        or value is None
                    ):
                        raise InputError("Unsupported RDF stream statement roles")
                    budget.consume("states", 5)
                    statement = {
                        "index": statements,
                        "terms": [term_data(term) for term in terms],
                    }
                    location = "statement/" + str(statements)
                    declarations = []
                    if str(predicate) not in excluded:
                        declarations.append(
                            TermDeclaration(
                                str(predicate), (str(RDF.Property),), location
                            )
                        )
                    if request["check_types"] and predicate == RDF.type:
                        if isinstance(value, URIRef):
                            if str(value) not in excluded:
                                declarations.append(
                                    TermDeclaration(
                                        str(value),
                                        (str(OWL.Class), str(RDFS.Class)),
                                        location,
                                    )
                                )
                        else:
                            checks += 1
                            keep(
                                Issue(
                                    "data.type_unverified",
                                    "Non-IRI type references require comparison",
                                    location,
                                    kind="unverified",
                                    actual=str(value),
                                ),
                                statement,
                            )
                    if declarations:
                        current = vocabulary
                        if declarations_reader is not None:
                            projection = declarations_reader.graph(
                                tuple(term.iri for term in declarations)
                            )
                            current = _projected_vocabulary(projection, budget)
                        assert current is not None
                        adopt(
                            check_vocabulary(current, declarations, budget=budget),
                            statement,
                        )
                    if request["check_literals"] and isinstance(value, Literal):
                        adopt(
                            check_literal(value, location=location, budget=budget),
                            statement,
                        )
                complete = reader.eof
    except LimitExceeded as error:
        stopped = str(error)
    except (SyntaxError, OSError, InputError) as error:
        wrapped: InputError
        if isinstance(error, SyntaxError | OSError):
            wrapped = ResourceError("Cannot parse or read the selected RDF stream")
            wrapped.__cause__ = error
        else:
            wrapped = error
        attach_input_failure(
            wrapped,
            document_iri=request["document_iri"],
            location=request["location"],
            role=request["role"],
        )
        try:
            keep(
                Issue(
                    "input.invalid",
                    "RDF stream parsing or selected validation did not complete",
                    kind="input_error",
                    details=Location.from_error(wrapped),
                ),
                None,
            )
        except LimitExceeded as exhausted:
            stopped = str(exhausted)
    result: dict[str, object] = {
        "status": "result",
        "complete": complete and not stopped,
        "statements": statements,
        "selected": selected,
        "checks": checks,
        "bytes_read": reader.count if reader is not None else 0,
        "sha256": reader.digest.hexdigest()
        if reader is not None and reader.eof
        else None,
        "records": records,
        "parse_issues": parse_issues,
        "stopped": stopped,
        "consumed": budget.consumed(),
    }
    return result


def main() -> int:
    """Read only a bounded metadata request, never the whole RDF stream payload."""
    try:
        cap = int(sys.argv[1])
        request = decode_json(sys.stdin.buffer.read(cap + 1), max_bytes=cap)
        if not isinstance(request, dict):
            raise InputError("RDF stream request must be an object")
        response = evaluate(request)
        payload = json.dumps(
            response, ensure_ascii=True, separators=(",", ":")
        ).encode()
        if len(payload) > request["output_cap"]:
            raise LimitExceeded("RDF stream reply exceeds its output cap")
        sys.stdout.buffer.write(payload)
    except (InputError, MemoryError, RecursionError):
        sys.stdout.buffer.write(b'{"status":"exhausted"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
