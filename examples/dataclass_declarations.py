"""Run explicit dataclass RDF declarations using only core dependencies."""

from dataclasses import dataclass, field, fields, replace

from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.models.contracts.checking import check_models

CATALOGUE = Namespace("https://example.org/catalogue#")


@dataclass
class Record:
    """Ordinary application data with explicit, framework-free RDF metadata."""

    name: str = field(
        metadata={
            "rdf": FieldDeclaration(
                "name",
                str(CATALOGUE.name),
                (ValueDeclaration("literal", datatype=str(XSD.string)),),
                # Construction requirements do not establish RDF emission guarantees.
                emission="unknown",
            )
        }
    )


def main() -> None:
    """Demonstrate supported agreement and an intentional datatype disagreement."""
    declaration = ModelDeclaration(
        "record",
        (str(CATALOGUE.Record),),
        tuple(item.metadata["rdf"] for item in fields(Record)),
    )
    ontology = Graph().add((CATALOGUE.Record, RDF.type, OWL.Class))
    ontology.add((CATALOGUE.name, RDF.type, OWL.DatatypeProperty))
    ontology.add((CATALOGUE.name, RDFS.range, XSD.string))
    assert check_models((declaration,), ontology).verified
    wrong = replace(
        declaration.fields[0],
        values=(ValueDeclaration("literal", datatype=str(XSD.boolean)),),
    )
    report = check_models((replace(declaration, fields=(wrong,)),), ontology)
    assert any(issue.code == "models.datatype_conflict" for issue in report.issues)
    print("Dataclass declaration agrees; intentional datatype mismatch detected.")


if __name__ == "__main__":
    main()
