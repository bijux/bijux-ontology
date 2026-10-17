"""Console data accompanies checking truth without replacing report policy."""

from dataclasses import dataclass, fields, is_dataclass

from rdflib import BNode, Literal, URIRef

from bijux_ontology.reports import Report


@dataclass(frozen=True)
class CommandResult:
    """A complete checking report and optional operation-specific display values."""

    report: Report
    data: object = None
    output_safe: bool = True


def display_data(value: object) -> object:
    """Encode typed RDF evidence without flattening nodes into ambiguous strings."""
    if isinstance(value, Report):
        return value.to_dict()
    if isinstance(value, URIRef):
        return {"kind": "iri", "value": str(value)}
    if isinstance(value, BNode):
        return {"kind": "blank", "value": str(value)}
    if isinstance(value, Literal):
        return {
            "kind": "literal",
            "value": str(value),
            "datatype": str(value.datatype) if value.datatype else None,
            "language": value.language,
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: display_data(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, (tuple, list)):
        return [display_data(item) for item in value]
    if isinstance(value, dict):
        return {str(key): display_data(item) for key, item in value.items()}
    return value
