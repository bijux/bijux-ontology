"""Finite compatibility of actual scoped RDF with named ontology contracts."""

import json
from typing import Literal as Choice

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.owl.obligations import OntologyObligation, _NativeProofs
from bijux_ontology.owl.properties import PropertyContract, property_contract
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry
from bijux_ontology.rdf.literals import Regime, check_literal, datatype_relation
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary

from .literals import bounded_text, integer_membership

Triple = tuple[Node, Node, Node]


def _term(value: Node, budget: Budget) -> list[str | None]:
    """Retain exact RDF identities, including original literal lexical forms."""
    for field in (
        value,
        value.datatype if isinstance(value, Literal) else None,
        value.language if isinstance(value, Literal) else None,
    ):
        if isinstance(field, str) and len(field) > budget.limits.max_bytes:
            raise LimitExceeded("Data compatibility term exceeds max_bytes")
    key = term_key(value)
    budget.consume("states", len(key))
    result: list[str | None] = []
    for part in key:
        result.append(None if part is None else bounded_text(part, budget))
    return result


class _Compatibility:
    """One operation's named contracts, evidence and cumulative allowances."""

    def __init__(
        self,
        schema: Graph,
        budget: Budget,
        regime: Regime,
        registry: DatatypeRegistry | None,
        graph: str | None,
    ) -> None:
        self.sources: tuple[tuple[str, Graph], ...] = ()
        self.budget = budget
        self.regime = regime
        self.registry = registry
        self.graph = graph
        self.selection: dict[str, object] = {"kind": "graph"}
        self.proofs = _NativeProofs(schema, budget)
        self.contracts: dict[str, PropertyContract] = {}
        self.asserted: dict[Node, set[str]] = {}
        self.types: dict[Node, set[str]] = {}
        self.witnesses: dict[Node, dict[Triple, None]] = {}
        self.seen: set[tuple[Node, str, str]] = set()
        self.issues = IssueCollector(budget)
        self.policy = IssueCollector(budget)
        self.checked = 0
        self.policy_checked = 0

    def issue(
        self,
        code: str,
        message: str,
        subject: Node,
        *,
        unknown: bool = False,
        expected: str | None = None,
        witness: tuple[Triple, ...] = (),
        policy: bool = False,
    ) -> None:
        """Capture bounded original data and ontology assertions once per finding."""
        key = subject, code, expected or ""
        self.budget.consume("work")
        if key in self.seen:
            return
        self.budget.consume("states")
        self.seen.add(key)
        origins: list[list[str]] = []
        for triple in witness:
            scopes = []
            for role, source in self.sources:
                self.budget.consume("work")
                if triple in source:
                    self.budget.consume("states")
                    scopes.append(bounded_text(role, self.budget))
            origins.append(scopes)
        evidence = {
            "subject": _term(subject, self.budget),
            "graph_selection": self.selection,
            "asserted_types": [
                bounded_text(kind, self.budget)
                for kind in sorted(self.asserted.get(subject, ()))
            ],
            "supported_types": [
                bounded_text(kind, self.budget)
                for kind in sorted(self.types.get(subject, ()))
            ],
            "ontology_scopes": origins,
            "data": [
                [_term(term, self.budget) for term in triple]
                for triple in self.witnesses.get(subject, ())
            ],
            "ontology": [
                [_term(term, self.budget) for term in triple] for triple in witness
            ],
        }
        chunks: list[str] = []
        size = 0
        for chunk in json.JSONEncoder(ensure_ascii=False).iterencode(evidence):
            bounded_text(chunk, self.budget)
            size += len(chunk.encode("utf-8"))
            if size > self.budget.limits.max_bytes:
                raise LimitExceeded("Data compatibility evidence exceeds max_bytes")
            self.budget.consume("states")
            chunks.append(chunk)
        self.budget.consume("bytes", size)
        encoded = "".join(chunks)
        anchor = bounded_text(str(subject), self.budget)
        if expected is not None:
            bounded_text(expected, self.budget)
        (self.policy if policy else self.issues).append(
            Issue(
                code,
                message,
                anchor,
                kind="unverified" if unknown else "finding",
                expected=expected,
                details=Location(term=anchor, graph=self.graph),
                evidence_json=encoded,
            )
        )

    def retain_assertion(self, node: Node, triple: Triple) -> None:
        """Retain original endpoint evidence even when its obligation is unknown."""
        self.budget.consume("work")
        if triple not in self.witnesses.get(node, ()):
            self.budget.consume("states")
            self.witnesses.setdefault(node, {})[triple] = None

    def add_type(self, node: Node, kind: str, witness: Triple) -> None:
        """Add one actual or supported endpoint consequence, never an RDF edge."""
        self.budget.consume("work")
        if kind in self.types.get(node, ()):
            return
        self.budget.consume("states", 3)
        self.types.setdefault(node, set()).add(kind)
        self.retain_assertion(node, witness)
        self.budget.consume("work", len(self.types[node]))
        self.budget.consume("states", len(self.types[node]))
        types = tuple(sorted(self.types[node]))
        _, unsupported = self.proofs.classes.ancestors(types)
        answer = self.proofs.answer(
            OntologyObligation("required_coinstance", types, regime=self.regime)
        )
        self.checked += 1
        if answer.status == "contradicted":
            self.issue(
                "data.disjoint_conflict",
                "Actual endpoint types imply explicitly disjoint classes",
                node,
                witness=answer.witness,
            )
        if unsupported:
            self.issue(
                "data.expression_unverified",
                "Relevant class expressions exceed the supported named contracts",
                node,
                unknown=True,
            )

    def endpoints(
        self,
        node: Node,
        requirements: tuple[Node, ...],
        triple: Triple,
        *,
        infer: bool,
        require_asserted: bool,
    ) -> None:
        """Keep inferred type truth separate from exact assertion coverage policy."""
        for required in requirements:
            self.retain_assertion(node, triple)
            self.budget.consume("work")
            self.checked += 1
            if not isinstance(required, URIRef):
                self.issue(
                    "data.expression_unverified",
                    "An actual property endpoint has an anonymous obligation",
                    node,
                    unknown=True,
                )
                continue
            kind = bounded_text(str(required), self.budget)
            if self.proofs.classes.ancestors((kind,))[1]:
                self.issue(
                    "data.expression_unverified",
                    "A named endpoint obligation has unsupported expressions",
                    node,
                    unknown=True,
                )
            datatype = (
                required == RDFS.Literal
                or (required, RDF.type, RDFS.Datatype) in self.proofs.graph
                or datatype_relation(kind, kind) in ("equal", "subset")
                or self.registry is not None
                and self.registry.lookup(kind, budget=self.budget) is not None
            )
            if datatype:
                self.issue(
                    "data.datatype_unverified",
                    "Resource membership in a datatype is not a named-class proof",
                    node,
                    unknown=True,
                    expected=kind,
                )
                continue
            if infer:
                self.add_type(node, kind, triple)
            if require_asserted:
                self.policy_checked += 1
                if kind not in self.asserted.get(node, ()):
                    self.issue(
                        "data.asserted_type_missing",
                        "Application policy requires an explicit type assertion",
                        node,
                        expected=kind,
                        policy=True,
                    )

    def literal(self, subject: Node, value: Literal, ranges: tuple[Node, ...]) -> None:
        """Check original samples before finite value-space range membership."""
        self.checked += 1
        report = check_literal(
            value,
            location=bounded_text(str(subject), self.budget),
            datatype_registry=self.registry,
            budget=self.budget,
        )
        self.issues.extend(report.issues)
        actual = str(
            value.datatype or (RDF.langString if value.language else XSD.string)
        )
        for required in ranges:
            self.budget.consume("work")
            self.checked += 1
            if required in (RDFS.Literal, RDFS.Resource):
                continue
            expression = (
                not isinstance(required, URIRef)
                or self.proofs.classes.ancestors((str(required),))[1]
            )
            if expression:
                self.issue(
                    "data.expression_unverified",
                    "A literal range has unsupported expressions",
                    subject,
                    unknown=True,
                )
            if not report.verified:
                if not expression:
                    self.issue(
                        "data.datatype_unverified",
                        "Range membership needs a qualified source literal",
                        subject,
                        unknown=True,
                        expected=str(required),
                    )
                continue
            relation = (
                datatype_relation(
                    actual,
                    str(required),
                    regime=self.regime,
                    datatype_registry=self.registry,
                    budget=self.budget,
                )
                if isinstance(required, URIRef)
                else "unknown"
            )
            if relation in ("equal", "subset"):
                continue
            member = (
                integer_membership(value, required, self.budget)
                if relation in ("overlap", "unknown")
                and isinstance(required, URIRef)
                and self.regime == "owl-dl"
                else None
            )
            if (
                member is True
                or expression
                and relation == "unknown"
                and member is None
            ):
                continue
            conflict = relation == "disjoint" or member is False
            self.issue(
                "data.datatype_conflict" if conflict else "data.datatype_unverified",
                "Actual literal is outside the required datatype value space"
                if conflict
                else "Actual literal range membership is outside supported operations",
                subject,
                unknown=not conflict,
                expected=str(required) if isinstance(required, URIRef) else None,
            )


def check_data_compatibility(
    data: Graph | PreparedSource,
    vocabulary: Vocabulary | Graph,
    *,
    graph_name: GraphSelection | None = None,
    union: bool = False,
    scopes: tuple[str, ...] = ("local", "imports"),
    mode: Choice["asserted", "inferred"] = "inferred",
    asserted_types: Choice["open", "required"] = "open",
    regime: Regime = "owl-dl",
    datatype_registry: DatatypeRegistry | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Report:
    """Check selected actual RDF against finite named ontology obligations.

    Inferred mode adds supported named domain/range endpoint consequences to the
    type checks; asserted mode examines supplied types only. Literal range and
    explicit property-kind checks apply in both modes. Required asserted types
    are a separate opt-in application policy, never an open-world missing field.
    Anonymous obligations stay unknown. Success is not global OWL consistency.
    Select ontology resource graphs while loading the supplied vocabulary.
    """
    if not isinstance(data, Graph | PreparedSource) or not isinstance(
        vocabulary, Vocabulary | Graph
    ):
        raise InputError("Expected owned RDF data and explicit ontology sources")
    if mode not in ("asserted", "inferred") or asserted_types not in (
        "open",
        "required",
    ):
        raise InputError("Select explicit data compatibility and assertion policies")
    if (
        type(scopes) is not tuple
        or not scopes
        or len(scopes) > 3
        or any(
            type(scope) is not str or scope not in ("local", "imports", "lookup")
            for scope in scopes
        )
    ):
        raise InputError("Select finite explicit ontology source roles")
    if regime not in ("application", "rdf", "owl-dl"):
        raise InputError("Select a supported semantic regime")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Expected explicit Limits")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("limits conflict with the supplied Budget")
    if datatype_registry is not None and not isinstance(
        datatype_registry, DatatypeRegistry
    ):
        raise InputError("Expected a DatatypeRegistry")
    operation = budget or Budget(limits)
    state = _Compatibility(Graph(), operation, regime, datatype_registry, None)
    complete = True
    try:
        if graph_name is not None:
            bounded_text(str(graph_name), operation)
        prepared = (
            data
            if isinstance(data, PreparedSource)
            else prepare_rdf(data, budget=operation)
        )
        source = prepared._read(graph_name, union=union, budget=operation)
        selected = (
            vocabulary
            if isinstance(vocabulary, Vocabulary)
            else load_vocabulary(vocabulary, budget=operation)
        )
        reader = selected._read(scopes, budget=operation)
        schema = reader.graph
        operation.consume("triples", len(source) + len(schema))
        state.sources = reader.scopes
        for triple in schema:
            operation.consume("work")
            for term in triple:
                _term(term, operation)
        state.proofs = _NativeProofs(schema, operation)
        chosen = graph_name if graph_name is not None else prepared.graph_name
        state.graph = (
            None
            if union or prepared.union or chosen is None
            else bounded_text(str(chosen), operation)
        )
        if union or prepared.union:
            state.selection = {"kind": "union"}
        elif chosen == "@default":
            state.selection = {"kind": "default"}
        elif chosen is not None:
            name = chosen if isinstance(chosen, BNode) else URIRef(str(chosen))
            state.selection = {"kind": "named", "term": _term(name, operation)}
        for subject, predicate, value in source:
            operation.consume("work")
            for term in (subject, predicate, value):
                _term(term, operation)
            if predicate != RDF.type:
                continue
            state.retain_assertion(subject, (subject, predicate, value))
            if not isinstance(value, URIRef):
                state.issue(
                    "data.type_unverified",
                    "An asserted type is not a named class",
                    subject,
                    unknown=True,
                )
                continue
            operation.consume("states")
            state.asserted.setdefault(subject, set()).add(str(value))
            state.add_type(subject, str(value), (subject, predicate, value))
        for subject, predicate, value in source:
            operation.consume("work")
            triple = (subject, predicate, value)
            state.retain_assertion(subject, triple)
            contract = state.contracts.get(str(predicate))
            if contract is None:
                operation.consume("states")
                contract = property_contract(
                    schema,
                    str(predicate),
                    annotation_contracts=regime != "owl-dl",
                    budget=operation,
                )
                operation.consume(
                    "states", len(contract.parents) + len(contract.values)
                )
                state.contracts[str(predicate)] = contract
            state.checked += 1
            if contract.unsupported or contract.literal_parent:
                state.issue(
                    "data.property_unverified",
                    "Relevant property relations exceed supported named contracts",
                    subject,
                    unknown=True,
                )
            if contract.bottom:
                state.issue(
                    "data.property_conflict",
                    "An actual assertion uses a supported bottom property",
                    subject,
                )
            state.endpoints(
                subject,
                contract.parents,
                triple,
                infer=mode == "inferred",
                require_asserted=asserted_types == "required",
            )
            if isinstance(value, Literal):
                if contract.object_property:
                    state.issue(
                        "data.property_conflict",
                        "An object-property assertion has a literal endpoint",
                        subject,
                    )
                state.literal(subject, value, contract.values)
            else:
                if contract.literal_value:
                    state.issue(
                        "data.property_conflict",
                        "A datatype-property assertion has a resource endpoint",
                        subject,
                    )
                state.endpoints(
                    value,
                    contract.values,
                    triple,
                    infer=mode == "inferred",
                    require_asserted=asserted_types == "required",
                )
        relevant: set[str] = set()
        for types in state.types.values():
            ancestors, _ = state.proofs.classes.ancestors(tuple(types))
            operation.consume("states", len(ancestors))
            relevant.update(ancestors)
        for group in state.proofs.classes.prepare_disjoint(relevant):
            operation.consume("work")
            state.issues.append(
                Issue(
                    "data.expression_unverified",
                    "A relevant disjoint-class group is malformed or unsupported",
                    kind="unverified",
                    actual=bounded_text(str(group), operation),
                )
            )
    except LimitExceeded as error:
        complete = False
        state.issues.exhausted(str(error))
    semantic = Report(
        "data.ontology-compatibility",
        state.checked,
        tuple(state.issues),
        complete=complete,
        regime=regime,
        families=(
            f"{mode}-named-types",
            "actual-literal-ranges",
            "named-property-contracts",
        ),
    )
    children = [semantic]
    if asserted_types == "required":
        children.append(
            Report(
                "data.asserted-type-policy",
                state.policy_checked,
                tuple(state.policy),
                allow_empty=True,
                complete=complete,
                regime="application-validation",
                families=("required-exact-type-assertions",),
            )
        )
    return Report(
        "data.compatibility",
        sum(child.checked for child in children),
        children=tuple(children),
        complete=complete,
        regime=f"finite-{regime}-{mode}",
    )
