"""Maintained finite RDFS/OWL-RL fixed points inside an isolated owned worker."""

import json
import sys
from typing import Any

from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import OWL
from rdflib.term import Node

from bijux_ontology._json import decode_json
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import ControlBudget
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.terms import (
    bound_node,
    literal_datatype,
    term_key,
    validate_triple,
)

from .protocol import decode_rows, decode_term, encode_rows, encode_term


def evaluate(request: dict[str, Any]) -> dict[str, object]:
    """Complete actual maintained rules, then retain all final consequences."""
    try:
        import owlrl  # type: ignore[import-untyped]
    except ImportError:
        return {
            "status": "unavailable",
            "message": "Install bijux-ontology[shacl] for owlrl entailment",
        }
    budget = ControlBudget(Limits(**request["limits"]), request["allowances"])
    backend = "owlrl " + owlrl.__version__
    reason = "backend_rejected"
    try:
        if request["regime"] not in ("rdfs", "owl-rl"):
            raise InputError("Unknown entailment regime")
        blanks: dict[str, BNode] = {}
        selection = decode_term(request["selection"], blanks, budget)
        if selection is not None and not isinstance(selection, (BNode, URIRef)):
            raise InputError("Entailment selection must be a graph resource")
        original = decode_rows(request["asserted"], blanks, budget, ordinary=True)
        dataset = Dataset()
        asserted = dataset.graph(URIRef("urn:bijux:entailment:asserted"))

        class BoundedDestination(Graph):
            """Bound public direct additions as well as stored rule proposals."""

            def add(self, triple: tuple[Node, Node, Node]) -> "BoundedDestination":
                """Reserve final triple storage before public Graph mutation."""
                budget.consume("work")
                for node in triple:
                    bound_node(node, budget)
                if triple not in self:
                    budget.consume("triples")
                    budget.consume("states")
                    budget.consume("bytes", 88)
                return super().add(triple)

        derived = BoundedDestination(
            store=dataset.store, identifier=URIRef("urn:bijux:entailment:derived")
        )
        for assertion in original:
            budget.consume("work")
            asserted.add(assertion)
        if (None, OWL.imports, None) in asserted:
            reason = "imports"
            raise InputError("Entailment preparation does not resolve required imports")
        if request["regime"] == "owl-rl":
            maintained_types = owlrl.OWLRL.OWL_RL_Datatypes
            budget.consume("states", len(maintained_types))
            supported = set()
            for datatype in maintained_types:
                bound_node(datatype, budget)
                supported.add(datatype)
            for assertion in original:
                for term in assertion:
                    budget.consume("work")
                    if (
                        isinstance(term, Literal)
                        and literal_datatype(term) not in supported
                    ):
                        reason = "unsupported_datatype"
                        raise InputError("Unsupported OWL-RL datatype")
        semantics = (
            owlrl.RDFS_Semantics
            if request["regime"] == "rdfs"
            else owlrl.OWLRL_Semantics
        )

        class BoundedSemantics(semantics):  # type: ignore[valid-type,misc]
            """Instrument public callbacks; all rule logic remains maintained."""

            def rules(self, triple: Any, cycle: int) -> None:
                """Charge each maintained rule sweep without replacing its logic."""
                budget.consume("work")
                super().rules(triple, cycle)

            def store_triple(self, triple: Any) -> None:
                """Reserve pending proposals before maintained deduplication/storage."""
                budget.consume("work")
                for node in triple:
                    bound_node(node, budget)
                if (
                    triple not in self.destination
                    and triple not in self.graph
                    and triple not in self.added_triples
                ):
                    budget.consume("triples")
                    budget.consume("states")
                    budget.consume("bytes", 88)
                super().store_triple(triple)

            def add_error(self, message: str) -> None:
                """Stop on maintained errors instead of exporting diagnostic RDF."""
                # Backend errors are not ordinary entailed data or a complete
                # global consistency proof. Stop rather than export error RDF.
                raise InputError(
                    "Maintained entailment reported unsupported or inconsistent data"
                )

        budget.consume("states", len(asserted))
        budget.consume("bytes", 64 + 32 * len(asserted))
        before = frozenset(asserted)
        asserted_keys = set()
        for asserted_triple in asserted:
            budget.consume("states")
            for asserted_term in asserted_triple:
                bound_node(asserted_term, budget)
            asserted_keys.add(tuple(term_key(term) for term in asserted_triple))
        owlrl.DeductiveClosure(
            BoundedSemantics,
            improved_datatypes=True,
            axiomatic_triples=False,
            datatype_axioms=False,
        ).expand(dataset, destination=derived)
        if len(asserted) != len(before):
            raise InputError("Maintained closure changed original asserted data")
        for retained in asserted:
            budget.consume("work")
            if retained not in before:
                raise InputError("Maintained closure changed original asserted data")
        ordinary = []
        generalized = []
        # Only the actual completed post_process result is projected. Inference
        # proposals removed by the maintained implementation are never exposed.
        for triple in derived:
            budget.consume("work")
            for final_term in triple:
                bound_node(final_term, budget)
            budget.consume("states")
            budget.consume("bytes", 32)
            if isinstance(triple[0], (URIRef, BNode)) and isinstance(triple[1], URIRef):
                validate_triple(*triple)
                if tuple(term_key(node) for node in triple) not in asserted_keys:
                    ordinary.append(triple)
            else:
                generalized.append(triple)
        budget.consume("states", len(ordinary) + len(generalized))
        budget.consume("bytes", 128 + 8 * (len(ordinary) + len(generalized)))
        result = {
            "status": "result",
            "backend": backend,
            "selection": encode_term(selection, budget),
            "asserted": encode_rows(asserted, budget),
            "derived": encode_rows(tuple(ordinary), budget),
            "generalized": encode_rows(tuple(generalized), budget),
        }
        budget.consume("bytes", json_bound(result, budget))
        budget.consume("bytes", json_bound({"consumed": request["allowances"]}, budget))
    except LimitExceeded:
        result = {
            "status": "exhausted",
            "backend": backend,
            "message": "Entailment exceeded its shared closure allowance",
        }
    except (InputError, ValueError, TypeError, RecursionError):
        result = {
            "status": "unverified",
            "backend": backend,
            "reason": reason,
            "message": "Maintained entailment could not qualify "
            "complete selected closure",
        }
    result["consumed"] = budget.consumed()
    return result


def main() -> int:
    """Read and return one fixed bounded JSON document without network loaders."""
    try:
        cap = int(sys.argv[1])
        if cap <= 0:
            raise InputError("Invalid entailment protocol bound")
        payload = sys.stdin.buffer.read(cap + 1)
        request = decode_json(payload, max_bytes=cap)
        if not isinstance(request, dict):
            raise InputError("Entailment request must be an object")
        result = evaluate(request)
        encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()
        if len(encoded) > cap:
            encoded = (
                b'{"status":"exhausted",'
                b'"message":"Entailment response exceeds protocol bound"}'
            )
        sys.stdout.buffer.write(encoded)
    except (InputError, LimitExceeded, MemoryError, RecursionError):
        sys.stdout.buffer.write(
            b'{"status":"exhausted","message":"Entailment protocol could not complete"}'
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
