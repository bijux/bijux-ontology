"""Shared explicit SHACL parameter declarations and parsed variable names."""

from dataclasses import dataclass
from decimal import Decimal

from pyparsing import ParseBaseException
from rdflib.plugins.sparql.parser import parseQuery

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.rdf.identifiers import absolute_iri


def parameter_name(path: str) -> str:
    """Read one parameter name through the maintained SPARQL variable parser."""
    name = path.split("#", 1)[1] if "#" in path else path.rsplit("/", 1)[-1]
    try:
        parsed = parseQuery(f"SELECT ?{name} WHERE {{}}")
    except ParseBaseException as error:
        raise InputError(
            "SHACL parameter local name is not a SPARQL variable"
        ) from error
    if (
        len(parsed[1]["projection"]) != 1
        or str(parsed[1]["projection"][0]["var"]) != name
    ):
        raise InputError("SHACL parameter path requires a SPARQL variable local name")
    return name


@dataclass(frozen=True)
class ShaclParameter:
    """An RDF parameter name, optional position and descriptive datatype."""

    path: str
    order: int | Decimal | None = None
    optional: bool = False
    datatype: str | None = None

    def __post_init__(self) -> None:
        absolute_iri(self.path)
        try:
            parameter_name(self.path)
        except Exception as error:
            raise InputError(
                "SHACL parameter path has no supported variable name"
            ) from error
        if type(self.optional) is not bool:
            raise InputError("Parameter optional must be a boolean")
        if self.order is not None and (
            isinstance(self.order, bool)
            or not isinstance(self.order, (int, Decimal))
            or not Decimal(self.order).is_finite()
        ):
            raise InputError("Parameter order requires a finite integer or decimal")
        if self.datatype is not None:
            absolute_iri(self.datatype)
