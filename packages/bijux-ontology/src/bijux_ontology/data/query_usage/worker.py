"""Finite role extraction from actual RDFLib query/update algebra; no evaluation."""

import json
import sys
from collections.abc import Mapping
from dataclasses import asdict
from typing import cast

from pyparsing import ParseBaseException
from rdflib import BNode, Literal, URIRef, Variable
from rdflib.namespace import RDF, XSD
from rdflib.paths import AlternativePath, InvPath, MulPath, NegatedPath, SequencePath
from rdflib.plugins.sparql.algebra import translateQuery, translateUpdate
from rdflib.plugins.sparql.parser import parseQuery, parseUpdate
from rdflib.plugins.sparql.parserutils import CompValue

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.documents.patches.operations import serialized
from bijux_ontology.execution.budgets import ControlBudget
from bijux_ontology.execution.limits import IssueCollector, Limits, bound_text
from bijux_ontology.json.values import decode_json, object_value
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import literal_datatype

from .reading import reserve
from .types import AlgebraPosition, QueryDynamicScope, QueryReference, QueryRole

Domains = dict[Variable, tuple[tuple[URIRef, AlgebraPosition], ...]]
_NODES = frozenset(
    (
        "SelectQuery",
        "AskQuery",
        "ConstructQuery",
        "DescribeQuery",
        "Project",
        "Distinct",
        "Reduced",
        "Slice",
        "OrderBy",
        "OrderCondition",
        "Filter",
        "Join",
        "LeftJoin",
        "Union",
        "Minus",
        "Graph",
        "ServiceGraphPattern",
        "GroupGraphPatternSub",
        "TriplesBlock",
        "ToMultiSet",
        "values",
        "Extend",
        "BGP",
        "Group",
        "AggregateJoin",
        "Function",
        "RelationalExpression",
        "ConditionalAndExpression",
        "ConditionalOrExpression",
        "AdditiveExpression",
        "MultiplicativeExpression",
        "UnaryNot",
        "UnaryPlus",
        "UnaryMinus",
        "Modify",
        "InsertClause",
        "DeleteClause",
        "InsertData",
        "DeleteData",
        "DeleteWhere",
        "Load",
        "Clear",
        "Drop",
        "Create",
        "Add",
        "Move",
        "Copy",
        "UsingClause",
        "DatasetClause",
    )
)
_CASTS = frozenset(
    (
        XSD.string,
        XSD.dateTime,
        XSD.float,
        XSD.double,
        XSD.decimal,
        XSD.integer,
        XSD.boolean,
    )
)


def field(value: CompValue, key: str) -> object:
    """CompValue.get uses missing keys as defaults; membership is required."""
    return dict.get(value, key)


class Inventory:
    """One owned traversal over native syntax positions and finite binding domains."""

    def __init__(self, source: str, budget: ControlBudget) -> None:
        """Own one supplied program inventory and its cumulative controls."""
        self.source, self.budget = source, budget
        self.references: list[QueryReference] = []
        self.dynamic: list[QueryDynamicScope] = []
        self.issues = IssueCollector(budget)

    def step(self, position: AlgebraPosition) -> None:
        """Prepay structural path ownership before descending native algebra."""
        reserve(self.budget, len(position) + 1)
        if len(position) > self.budget.limits.max_depth:
            raise LimitExceeded("Query dependency algebra exceeds max_depth")

    def reference(
        self,
        iri: URIRef,
        role: QueryRole,
        position: AlgebraPosition,
        binding: AlgebraPosition | None = None,
        reverse: bool = False,
    ) -> None:
        """Retain a resolved IRI with its actual role and algebra position."""
        self.step(position)
        bound_text(iri, self.budget)
        text = absolute_iri(str(iri))
        self.budget.consume(
            "bytes",
            4 * (len(self.source) + sum(len(str(p)) + 1 for p in position) + 16),
        )
        where = (
            self.source
            + "#algebra/"
            + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in position)
        )
        self.references.append(
            QueryReference(text, role, position, where, binding, reverse)
        )

    def unknown(
        self,
        role: str,
        position: AlgebraPosition,
        reason: str,
        variable: Variable | None = None,
    ) -> None:
        """Keep an unresolved dependency obligation beside established constants."""
        self.step(position)
        text = None if variable is None else str(variable)
        if text is not None:
            bound_text(text, self.budget)
        self.dynamic.append(QueryDynamicScope(role, position, reason, text))
        self.issues.append(
            Issue(
                "query.dependencies_unverified",
                reason,
                self.source,
                kind="unverified",
                actual=text or role,
            )
        )

    def domains(self, value: object, position: AlgebraPosition) -> Domains:
        """Extract only finite VALUES domains required in this binding scope."""
        self.step(position)
        if not isinstance(value, CompValue):
            return {}
        if value.name == "values":
            rows = field(value, "res")
            if not isinstance(rows, list) or not rows:
                return {}
            first = rows[0]
            if not isinstance(first, Mapping):
                raise InputError("Native VALUES rows are unavailable")
            result: Domains = {}
            reserve(self.budget, len(first) * (len(rows) + 1))
            for variable in first:
                self.budget.consume("work")
                if not isinstance(variable, Variable):
                    continue
                bound_text(variable, self.budget)
                captured = []
                for index, row in enumerate(rows):
                    self.budget.consume("work")
                    if not isinstance(row, Mapping) or not isinstance(
                        row.get(variable), URIRef
                    ):
                        break
                    captured.append(
                        (row[variable], (*position, "res", index, str(variable)))
                    )
                else:
                    result[variable] = tuple(captured)
            return result
        if value.name == "ToMultiSet":
            child = field(value, "p")
            return (
                self.domains(child, (*position, "p"))
                if isinstance(child, CompValue) and child.name == "values"
                else {}
            )
        if value.name in ("Filter", "Extend"):
            return self.domains(field(value, "p"), (*position, "p"))
        if value.name in ("Join", "LeftJoin"):
            left = self.domains(field(value, "p1"), (*position, "p1"))
            if value.name == "LeftJoin":
                return left
            right = self.domains(field(value, "p2"), (*position, "p2"))
            reserve(self.budget, len(left) + len(right))
            for key, items in right.items():
                # Either required finite binding bounds the shared variable.
                left.setdefault(key, items)
            return left
        return {}

    def term(
        self,
        value: object,
        role: QueryRole,
        position: AlgebraPosition,
        domains: Domains,
        reverse: bool = False,
    ) -> None:
        """Distinguish vocabulary positions from data and unresolved variables."""
        self.step(position)
        if isinstance(value, URIRef):
            self.reference(value, role, position, reverse=reverse)
        elif isinstance(value, Literal):
            self.reference(literal_datatype(value), "datatype", (*position, "datatype"))
        elif isinstance(value, Variable) and role in ("predicate", "class", "datatype"):
            if value in domains:
                for iri, origin in domains[value]:
                    self.reference(iri, role, position, origin, reverse)
            else:
                self.unknown(
                    role,
                    position,
                    "Variable vocabulary position lacks finite required VALUES",
                    value,
                )
        elif isinstance(value, BNode) and role in ("predicate", "class", "datatype"):
            self.unknown(
                role,
                position,
                "Anonymous query vocabulary position is not a constant IRI",
            )

    def path(
        self,
        value: object,
        position: AlgebraPosition,
        domains: Domains,
        reverse: bool = False,
    ) -> None:
        """Visit maintained property paths without inventing lost native terms."""
        self.step(position)
        if isinstance(value, URIRef | Variable):
            self.term(value, "predicate", position, domains, reverse)
        elif isinstance(value, InvPath):
            self.path(value.arg, (*position, "inverse"), domains, not reverse)
        elif isinstance(value, SequencePath | AlternativePath):
            for index, child in enumerate(value.args):
                self.path(child, (*position, "args", index), domains, reverse)
        elif isinstance(value, MulPath):
            self.path(value.path, (*position, "path"), domains, reverse)
        elif isinstance(value, NegatedPath):
            for index, child in enumerate(value.args):
                if isinstance(child, URIRef | InvPath):
                    self.path(child, (*position, "excluded", index), domains, reverse)
            self.unknown(
                "predicate",
                position,
                "Negated paths have open dependency coverage; "
                "native inverse exclusions may lack resolved terms",
            )
        else:
            self.unknown(
                "predicate",
                position,
                "Native property-path representation is unsupported",
            )

    def class_endpoint(
        self, value: object, subject: bool, position: AlgebraPosition
    ) -> bool:
        """Read trivial native endpoints while keeping hidden classes unknown."""
        self.step(position)
        if isinstance(value, URIRef):
            return value == RDF.type and not subject
        if isinstance(value, InvPath):
            return self.class_endpoint(value.arg, not subject, (*position, "inverse"))
        if isinstance(value, SequencePath) and value.args:
            index = 0 if subject else len(value.args) - 1
            return self.class_endpoint(
                value.args[index], subject, (*position, "args", index)
            )
        if isinstance(value, MulPath) and value.mod == "+":
            return self.class_endpoint(value.path, subject, (*position, "path"))
        return False

    def triples(
        self, rows: object, position: AlgebraPosition, domains: Domains
    ) -> None:
        """Associate class and predicate roles with each native triple pattern."""
        if not isinstance(rows, list | tuple):
            raise InputError("Native triple-pattern inventory is unavailable")
        for index, row in enumerate(rows):
            self.step((*position, index))
            if not isinstance(row, list | tuple) or len(row) != 3:
                raise InputError("Native triple pattern has invalid arity")
            subject, predicate, value = row
            at = (*position, index)
            first_reference = len(self.references)
            self.path(predicate, (*at, "predicate"), domains)
            choices = (
                tuple(iri for iri, _ in domains.get(predicate, ()))
                if isinstance(predicate, Variable)
                else (predicate,)
            )
            subject_class = self.class_endpoint(predicate, True, (*at, "predicate"))
            object_class = RDF.type in choices or self.class_endpoint(
                predicate, False, (*at, "predicate")
            )
            self.term(
                subject, "class" if subject_class else "data", (*at, "subject"), domains
            )
            if object_class:
                self.term(value, "class", (*at, "object"), domains)
            if (
                not object_class
                or any(choice != RDF.type for choice in choices)
                and isinstance(predicate, Variable)
            ):
                self.term(value, "data", (*at, "object"), domains)
            self.budget.consume("work", len(self.references) - first_reference + 1)
            contains_type = any(
                self.references[index].role == "predicate"
                and self.references[index].iri == str(RDF.type)
                for index in range(first_reference, len(self.references))
            )
            if (
                contains_type
                and not isinstance(predicate, URIRef | Variable)
                and not (isinstance(predicate, InvPath) and predicate.arg == RDF.type)
            ):
                self.unknown(
                    "class",
                    (*at, "predicate"),
                    "Composite rdf:type path has unimplemented class-binding coverage",
                )

    def walk(
        self,
        value: object,
        position: AlgebraPosition = (),
        domains: Domains | None = None,
    ) -> None:
        """Traverse supported native algebra while retaining unknown scope."""
        self.step(position)
        selected = {} if domains is None else domains
        if isinstance(value, CompValue):
            if value.name not in _NODES and not value.name.startswith(
                ("Builtin_", "Aggregate_")
            ):
                self.unknown(
                    "unsupported",
                    position,
                    "Native algebra node is outside the supported dependency profile: "
                    + value.name,
                )
            local = self.domains(value, position)
            reserve(self.budget, len(selected) + len(local) + 1)
            combined = dict(selected)
            combined.update(local)
            if value.name == "ToMultiSet" and not (
                isinstance(field(value, "p"), CompValue)
                and cast(CompValue, field(value, "p")).name == "values"
            ):
                combined = {}
            if value.name in ("BGP", "TriplesBlock"):
                self.triples(field(value, "triples"), (*position, "triples"), combined)
                return
            if value.name == "Function":
                iri = field(value, "iri")
                if isinstance(iri, URIRef):
                    self.reference(iri, "function", (*position, "iri"))
                    if iri in _CASTS:
                        self.reference(iri, "datatype", (*position, "iri"))
                else:
                    self.unknown(
                        "function",
                        position,
                        "Native function has no resolved constant IRI",
                    )
            if value.name in ("Builtin_IRI", "Builtin_URI"):
                self.unknown(
                    "constructed-iri",
                    position,
                    "IRI construction exceeds a finite constant dependency inventory",
                )
            if value.name == "Builtin_STRDT":
                datatype = field(value, "arg2")
                self.term(datatype, "datatype", (*position, "arg2"), combined)
                if not isinstance(datatype, URIRef | Variable):
                    self.unknown(
                        "datatype",
                        (*position, "arg2"),
                        "Expression datatype lacks an established finite IRI domain",
                    )
                    self.walk(datatype, (*position, "arg2"), combined)
            if value.name in ("DescribeQuery", "Clear", "Drop", "Add", "Move", "Copy"):
                self.unknown(
                    "graph-content",
                    position,
                    "Operation graph vocabulary is absent from the supplied program",
                )
            if value.name == "Load":
                self.unknown(
                    "update-source",
                    position,
                    "LOAD imports vocabulary use not present in the supplied program",
                )
            # A template may use only the required finite domains of its WHERE.
            if value.name in ("ConstructQuery", "Modify"):
                body_key = "p" if value.name == "ConstructQuery" else "where"
                body_position = (*position, body_key)
                body = field(value, body_key)
                if isinstance(body, CompValue) and body.name == "Project":
                    body = field(body, "p")
                    body_position = (*body_position, "p")
                combined.update(self.domains(body, body_position))
            for key, child in value.items():
                if key == "_vars" or key == "service_string":
                    continue
                child_position = (*position, key)
                if key in ("triples", "template"):
                    if child is not None:
                        self.triples(child, child_position, combined)
                elif key == "quads" and isinstance(child, Mapping):
                    for graph, rows in child.items():
                        self.term(
                            graph, "graph", (*child_position, str(graph)), combined
                        )
                        self.triples(
                            rows, (*child_position, str(graph), "triples"), combined
                        )
                elif (
                    key == "iri"
                    and value.name == "Function"
                    or key == "arg2"
                    and value.name == "Builtin_STRDT"
                ):
                    continue
                elif (
                    key == "term"
                    and value.name in ("Graph", "ServiceGraphPattern")
                    or key in ("graphiri", "withClause")
                    or value.name in ("DatasetClause", "UsingClause")
                    and key in ("default", "named")
                ):
                    self.term(child, "graph", child_position, combined)
                elif value.name == "Minus" and key == "p2":
                    self.walk(child, child_position, {})
                else:
                    self.walk(child, child_position, combined)
        elif isinstance(value, Mapping):
            for key, child in value.items():
                self.walk(child, (*position, str(key)), selected)
        elif isinstance(value, list | tuple):
            for index, child in enumerate(value):
                self.walk(child, (*position, index), selected)
        elif isinstance(value, URIRef | Literal):
            self.term(value, "data", position, selected)


def main() -> None:
    """Translate one supplied program without execution or retrieval."""
    cap = int(sys.argv[1])
    raw = sys.stdin.buffer.read(cap + 1)
    if len(raw) > cap:
        raise LimitExceeded("Query usage protocol input exceeds its bound")
    request = object_value(
        decode_json(raw, max_bytes=cap), location="query usage input"
    )
    if set(request) != {
        "text",
        "source",
        "kind",
        "base",
        "namespaces",
        "limits",
        "allowances",
    }:
        raise InputError("Malformed query usage request")
    limits = Limits(
        **cast(dict[str, int], object_value(request["limits"], location="limits"))
    )
    budget = ControlBudget(limits, request["allowances"])
    inventory = Inventory(cast(str, request["source"]), budget)
    complete = False
    try:
        text, kind = request["text"], request["kind"]
        if type(text) is not str or kind not in ("query", "update"):
            raise InputError("Malformed SPARQL source/kind")
        bound_text(text, budget)
        namespaces = {
            key: URIRef(cast(str, iri))
            for key, iri in object_value(
                request["namespaces"], location="namespaces"
            ).items()
        }
        try:
            native = (
                translateQuery(
                    parseQuery(text),
                    base=cast(str | None, request["base"]),
                    initNs=namespaces,
                ).algebra
                if kind == "query"
                else translateUpdate(
                    parseUpdate(text),
                    base=cast(str | None, request["base"]),
                    initNs=namespaces,
                ).algebra
            )
        except ParseBaseException as error:
            raise InputError("SPARQL syntax is invalid: " + str(error)) from error
        except RecursionError as error:
            raise LimitExceeded(
                "Native SPARQL translation exhausted recursion"
            ) from error
        except Exception as error:
            raise InputError(
                "Native SPARQL translation failed: " + str(error)
            ) from error
        inventory.walk(native)
        complete = not inventory.dynamic
    except LimitExceeded as error:
        inventory.issues.exhausted(str(error))
    except InputError as error:
        try:
            inventory.issues.append(
                Issue(
                    "query.usage_input",
                    str(error),
                    inventory.source,
                    kind="input_error",
                )
            )
        except LimitExceeded as exhausted:
            inventory.issues.exhausted(str(exhausted))
    report = Report(
        "query.inventory",
        checked=1 if complete else 0,
        requested=1,
        complete=complete,
        issues=tuple(inventory.issues),
        allow_empty=True,
        families=("query.static_dependencies",),
        regime="sparql-1.1-algebra",
    )
    try:
        reserve(
            budget,
            32
            + 16 * len(report.issues)
            + 16 * (len(inventory.references) + len(inventory.dynamic)),
        )
        references = []
        for reference in inventory.references:
            item = asdict(reference)
            item["position"] = list(reference.position)
            item["binding_position"] = (
                None
                if reference.binding_position is None
                else list(reference.binding_position)
            )
            references.append(item)
        dynamic = []
        for scope in inventory.dynamic:
            item = asdict(scope)
            item["position"] = list(scope.position)
            dynamic.append(item)
        response = {
            "references": references,
            "dynamic": dynamic,
            "report": report.to_dict(),
            "consumed": budget.allowances,
        }
        encoded = serialized(response, budget)
        budget.consume("bytes", 4 * len(encoded))
        budget.consume("work", len(encoded))
        response["consumed"] = budget.consumed()
        content = json.dumps(
            response, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except LimitExceeded as error:
        inventory.issues.retain_completed(report)
        inventory.issues.exhausted(str(error))
        report = Report(
            "query.inventory",
            requested=1,
            complete=False,
            issues=tuple(inventory.issues),
            families=("query.static_dependencies",),
            regime="sparql-1.1-algebra",
        )
        content = json.dumps(
            {
                "references": [],
                "dynamic": [],
                "report": report.to_dict(),
                "consumed": budget.consumed(),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
    if len(content) > cap:
        raise LimitExceeded("Query usage protocol output exceeds its bound")
    sys.stdout.buffer.write(content)


if __name__ == "__main__":
    main()
