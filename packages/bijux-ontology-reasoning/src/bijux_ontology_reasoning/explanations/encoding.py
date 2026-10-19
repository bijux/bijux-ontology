"""Reuse the existing finite axiom contract for an owned explanation question."""

from dataclasses import replace

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget

from ..axioms.codec.decoding import ProjectionDecoder
from ..axioms.codec.encoding import ConstructionEncoder
from .types import ExplanationQuestion


def capture_question(
    value: ExplanationQuestion, budget: Budget
) -> tuple[ExplanationQuestion, dict[str, object]]:
    """Own exact plain records before native execution or import callbacks."""
    if type(value) is not ExplanationQuestion:
        raise InputError("Supply an ordinary typed explanation question")
    encoder = ConstructionEncoder(budget)
    location = encoder.text(value.location)
    kind = encoder.text(value.kind)
    if kind not in ("entailment", "unsatisfiability", "inconsistency"):
        raise InputError("Choose a supported logical explanation", location=location)
    if kind == "entailment":
        if value.axiom is None or value.class_iri is not None:
            raise InputError(
                "Entailment explanations require only an axiom", location=location
            )
        wire = encoder.node(value.axiom)
        owned = ProjectionDecoder(budget).axiom(wire)
        return replace(value, axiom=owned), {"kind": kind, "axiom": wire}
    if value.axiom is not None:
        raise InputError("This explanation does not select an axiom", location=location)
    if kind == "unsatisfiability":
        if value.class_iri is None:
            raise InputError("Select the named unsatisfiable class", location=location)
        iri = encoder.text(value.class_iri, iri=True)
        return replace(value, class_iri=iri), {"kind": kind, "class_iri": iri}
    if value.class_iri is not None:
        raise InputError("Inconsistency selects the whole closure", location=location)
    return replace(value), {"kind": kind}
