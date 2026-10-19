"""Explicit serializer bindings and retained original SHACL obligations."""

from dataclasses import dataclass, field

from rdflib import BNode, URIRef
from rdflib.term import Node

from bijux_ontology.errors import InputError
from bijux_ontology.interfaces.adapters.contracts import ModelExtraction
from bijux_ontology.reports import Report
from bijux_ontology.shacl.inspection.types import ShapeInspection


@dataclass(frozen=True)
class ShapeFieldBinding:
    """Bind an actual selected property shape to one ordinary serializer key.

    The containing source object is selected independently by ModelUsage.path.
    Blank shape identifiers must be actual terms owned by the supplied source.
    """

    shape: URIRef | BNode
    name: str
    key: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Require typed metadata without inferring names from RDF identifiers."""
        if not isinstance(self.shape, URIRef | BNode):
            raise InputError("Shape field bindings require an IRI or owned blank")
        for text in (self.name, self.key):
            if text is not None and (
                not isinstance(text, str) or isinstance(text, Node) or not text
            ):
                raise InputError("Serializer member names and keys require text")


@dataclass(frozen=True)
class UntranslatedShapeConstraint:
    """One exact original assertion not represented by the imported declarations."""

    shape: URIRef | BNode
    predicate: URIRef
    value: Node
    reason: str


@dataclass(frozen=True, init=False)
class ShapeContractImport:
    """Partial declarations with indispensable source and qualification evidence.

    An early capture cutoff can prevent inspection entirely. A declaration's
    presence alone never discharges the extraction report's required obligations.
    """

    inspection: ShapeInspection | None
    extraction: ModelExtraction
    untranslated: tuple[UntranslatedShapeConstraint, ...]
    rdf_contract_report: Report
    _translation_report: Report = field(repr=False)

    def __init__(self) -> None:
        """Keep source qualification associated with the actual import operation."""
        raise InputError("ShapeContractImport is created by import_shape_contracts")

    @classmethod
    def _from_import(
        cls,
        inspection: ShapeInspection | None,
        extraction: ModelExtraction,
        untranslated: tuple[UntranslatedShapeConstraint, ...],
        rdf_contract_report: Report,
        translation_report: Report,
    ) -> "ShapeContractImport":
        """Freeze the operation's declarations and required original obligations."""
        result = object.__new__(cls)
        object.__setattr__(result, "inspection", inspection)
        object.__setattr__(result, "extraction", extraction)
        object.__setattr__(result, "untranslated", untranslated)
        object.__setattr__(result, "rdf_contract_report", rdf_contract_report)
        object.__setattr__(result, "_translation_report", translation_report)
        return result
