"""Operation-owned assertion facts, exact literal checks and proof storage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal as Choice
from typing import cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import IssueCollector, retain_report
from bijux_ontology.owl.obligations import OntologyObligation, _NativeProofs
from bijux_ontology.rdf.literals.checking import check_literal
from bijux_ontology.rdf.roles.classification import declaration_role
from bijux_ontology.rdf.terms import bound_node, literal_datatype, term_key
from bijux_ontology.rdf.value_comparison.observations import ScalarComparisons

from .identity import Identity, retain_premises
from .indexing import PropertyRows
from .patterns import Patterns, Resource, Value
from .types import AssertionWitness, Statement, TermKey


@dataclass(frozen=True)
class _Row:
    """An original property assertion and its already qualified proof premise."""

    subject: Resource
    value: Value
    premise: Statement


class AssertionContext:
    """Finite selected-source state shared by fixed identity and witness rules."""

    def __init__(self, graph: Graph, operation: Operation, faithful: bool) -> None:
        """Keep all proof/counter state local to this one authorized graph view."""
        self.graph, self.operation, self.faithful = graph, operation, faithful
        self.issues = IssueCollector(operation)
        self.patterns = Patterns(graph, operation, self.issues)
        self.identity = Identity(operation)
        self.proofs = _NativeProofs(graph, operation)
        self.witnesses: list[AssertionWitness] = []
        self.children: list[Report] = []
        self.seen: set[tuple[str, frozenset[Statement]]] = set()
        self.unknowns: set[str] = set()
        self.properties: dict[
            URIRef, tuple[Choice["object", "data"] | None, tuple[Statement, ...]]
        ] = {}
        self.assertions: dict[URIRef, tuple[_Row, ...]] = {}
        self.row_indexes: dict[URIRef, tuple[int, PropertyRows]] = {}
        self.named: set[TermKey] = set()
        self.types: list[tuple[Resource, URIRef, Statement]] = []
        self.literal_relations: dict[
            tuple[TermKey, TermKey], Choice["equal", "different"] | None
        ] = {}
        self.validated: set[TermKey] = set()
        self.comparisons = ScalarComparisons(operation)
        self.memberships: dict[tuple[URIRef, URIRef], tuple[Statement, ...] | None] = {}

    def unknown(self, message: str) -> None:
        """Keep one bounded required diagnostic per unsupported pattern boundary."""
        self.operation.consume("work")
        if message not in self.unknowns:
            self.operation.consume("states")
            self.unknowns.add(message)
            self.patterns.unknown(message)

    def key(self, value: Node) -> TermKey:
        """Bound original RDF metadata before constructing exact identity keys."""
        bound_node(value, self.operation)
        self.operation.consume("bytes", 64)
        return term_key(value)

    def mark_named(self, value: Resource) -> None:
        """Only actual individual positions establish syntactic named individuals."""
        if isinstance(value, URIRef):
            key = self.key(value)
            self.operation.consume("states")
            self.named.add(key)

    def equal(self, left: Resource, right: Resource) -> tuple[Statement, ...] | None:
        """An absent equality proof never asserts that two resources differ."""
        return self.identity.proof(self.key(left), self.key(right))

    def named_proof(self, value: Resource) -> tuple[Statement, ...] | None:
        """A blank individual is named only through established equality to a name."""
        key = self.key(value)
        if key in self.named:
            return ()
        for named in self.named:
            self.operation.consume("work")
            proof = self.identity.proof(key, named)
            if proof is not None:
                return proof
        return None

    def witness(self, rule: str, premises: tuple[Statement, ...]) -> None:
        """Retain complete original premises before a later diagnostic limit stops."""
        proof = retain_premises(premises, self.operation)
        self.operation.consume("states", len(proof) + 1)
        self.operation.consume("bytes", 128 + 16 * len(proof))
        signature = rule, frozenset(proof)
        if signature in self.seen:
            return
        self.seen.add(signature)
        self.witnesses.append(AssertionWitness(rule, proof))
        self.issues.append(
            Issue(
                "owl.assertion_contradiction",
                "Original selected premises establish a finite contradiction",
                expected=rule,
            )
        )

    def kind(
        self, predicate: URIRef
    ) -> tuple[Choice["object", "data"] | None, tuple[Statement, ...]]:
        """Property categories need explicit supplied logical declarations."""
        cached = self.properties.get(predicate)
        if cached is not None:
            return cached
        for relation in (
            RDFS.subPropertyOf,
            OWL.equivalentProperty,
            OWL.inverseOf,
            OWL.propertyChainAxiom,
        ):
            self.operation.consume("work", 2)
            outgoing = next(self.graph.triples((predicate, relation, None)), None)
            incoming = next(self.graph.triples((None, relation, predicate)), None)
            if outgoing is not None or incoming is not None:
                self.unknown(
                    "Relevant property entailments exceed direct assertion scope"
                )
        kinds = self.patterns.objects(predicate, RDF.type)
        object_ = OWL.ObjectProperty in kinds
        data = OWL.DatatypeProperty in kinds
        result: tuple[Choice["object", "data"] | None, tuple[Statement, ...]]
        if OWL.AnnotationProperty in kinds or object_ == data:
            result = None, ()
        else:
            role = OWL.ObjectProperty if object_ else OWL.DatatypeProperty
            result = (
                ("object" if object_ else "data"),
                (self.patterns.statement(predicate, RDF.type, role),),
            )
        self.operation.consume("states")
        self.properties[predicate] = cast(
            tuple[Choice["object", "data"] | None, tuple[Statement, ...]], result
        )
        return self.properties[predicate]

    def rows(self, predicate: URIRef) -> tuple[_Row, ...]:
        """Cache only demanded property assertions using the maintained graph index."""
        cached = self.assertions.get(predicate)
        if cached is not None:
            return cached
        kind, declaration = self.kind(predicate)
        rows = []
        seen: set[Statement] = set()
        for subject, _, value in self.graph.triples((None, predicate, None)):
            self.operation.consume("work")
            proof = self.patterns.statement(subject, predicate, value)
            if proof in seen:
                continue
            self.operation.consume("states")
            seen.add(proof)
            if not isinstance(subject, URIRef | BNode):
                self.unknown("A logical property assertion has an unsupported subject")
                continue
            if kind is None or (kind == "data") != isinstance(value, Literal):
                self.unknown(
                    "Assertion property/value roles lack one supported logical category"
                )
                continue
            self.mark_named(subject)
            if isinstance(value, URIRef | BNode):
                self.mark_named(value)
            self.operation.consume("states")
            self.operation.consume("bytes", 96)
            row = _Row(subject, cast(Value, value), proof)
            rows.append(row)
            if isinstance(value, Literal):
                self.validate(value, (proof, *declaration))
        result = tuple(rows)
        self.assertions[predicate] = result
        return result

    def indexed_rows(self, predicate: URIRef) -> PropertyRows:
        """Rebuild only after a new individual identity changes row membership."""
        generation = len(self.identity.consequences)
        existing = self.row_indexes.get(predicate)
        if existing is None or existing[0] != generation:
            rows = PropertyRows(self, predicate)
            self.operation.consume("states")
            self.operation.reserve_storage(128)
            self.row_indexes[predicate] = generation, rows
        return self.row_indexes[predicate][1]

    def validate(self, value: Literal, premises: tuple[Statement, ...]) -> None:
        """Qualify actual selected data samples without changing their lexical form."""
        key = self.key(value)
        if key in self.validated:
            return
        self.operation.consume("states")
        self.validated.add(key)
        if not self.faithful:
            self.unknown("Literal witnesses require exact source-term fidelity")
            return
        report = check_literal(value, budget=self.operation)
        retain_report(report, self.operation, self.issues)
        self.children.append(report)
        if any(issue.code == "literal.lexical_invalid" for issue in report.issues):
            self.witness("datatype_lexical", premises)

    def literal_equal(
        self, left: Literal, right: Literal
    ) -> Choice["equal", "different"] | None:
        """Use the actual OWL datatype map; intrinsic string identity is exact only."""
        key = self.key(left), self.key(right)
        if key in self.literal_relations:
            return self.literal_relations[key]
        answer = None
        if not self.faithful:
            self.unknown("Literal comparisons require exact source-term fidelity")
        elif (
            literal_datatype(left) in (XSD.string, RDF.langString)
            and literal_datatype(right) in (XSD.string, RDF.langString)
            and key[0] == key[1]
        ):
            report = check_literal(left, budget=self.operation)
            retain_report(report, self.operation, self.issues)
            self.children.append(report)
            answer = "equal" if report.verified else None
        else:
            comparison = self.comparisons.compare(
                left, right, regime="owl2-datatype-map"
            )
            retain_report(comparison.report, self.operation, self.issues)
            self.children.append(comparison.report)
            if comparison.report.verified and comparison.relation in (
                "equal",
                "different",
            ):
                answer = comparison.relation
        self.operation.consume("states")
        self.literal_relations[key] = cast(Choice["equal", "different"] | None, answer)
        return self.literal_relations[key]

    def initial(self) -> None:
        """Separate actual individual type positions from punned declarations."""
        for subject, _, kind in self.graph.triples((None, RDF.type, None)):
            self.operation.consume("work")
            proof = self.patterns.statement(subject, RDF.type, kind)
            if not isinstance(subject, URIRef | BNode):
                continue
            if kind == OWL.NamedIndividual:
                self.mark_named(subject)
            if declaration_role(kind) is not None:
                continue
            if not isinstance(kind, URIRef):
                self.unknown(
                    "Anonymous actual types exceed the finite named-type scope"
                )
                continue
            self.mark_named(subject)
            self.operation.consume("states")
            self.operation.consume("bytes", 64)
            self.types.append((subject, kind, proof))
        for subject, _, value in self.graph.triples((None, OWL.sameAs, None)):
            self.operation.consume("work")
            premise = self.patterns.statement(subject, OWL.sameAs, value)
            if not isinstance(subject, URIRef | BNode) or not isinstance(
                value, URIRef | BNode
            ):
                self.unknown(
                    "Same-individual assertions require two resource endpoints"
                )
                continue
            self.mark_named(subject)
            self.mark_named(value)
            self.identity.merge(
                self.key(subject), self.key(value), "same_individual", (premise,)
            )

    def characteristic(self, characteristic: URIRef) -> tuple[URIRef, ...]:
        """Qualify each selected characteristic before retaining its predicate."""
        result = []
        for node in self.graph.subjects(RDF.type, characteristic):
            self.operation.consume("work")
            bound_node(node, self.operation)
            if not isinstance(node, URIRef):
                self.unknown(
                    "Anonymous property expressions are outside characteristic scope"
                )
                continue
            self.operation.consume("states")
            result.append(node)
        return tuple(result)

    def member(self, actual: URIRef, required: URIRef) -> tuple[Statement, ...] | None:
        """Reuse selected finite named ancestry and its original authored edges."""
        bound_node(actual, self.operation)
        bound_node(required, self.operation)
        key = actual, required
        if key not in self.memberships:
            answer = self.proofs.answer(
                OntologyObligation(
                    "subsumption", (str(actual),), str(required), regime="owl-dl"
                )
            )
            self.operation.consume("states")
            self.memberships[key] = (
                tuple(self.patterns.statement(*edge) for edge in answer.witness)
                if answer.status == "established"
                else None
            )
        return self.memberships[key]
