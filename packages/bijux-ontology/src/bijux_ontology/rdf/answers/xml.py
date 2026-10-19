"""Finite SPARQL XML vocabulary over non-resolving maintained Expat events."""

from typing import NoReturn
from xml.parsers import expat

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, bound_text

_NS = "http://www.w3.org/2005/sparql-results# "
_LANG = "http://www.w3.org/XML/1998/namespace lang"


def read_xml(payload: str, budget: Budget) -> dict[str, object]:
    """Preserve literal character data without native result-reader trimming."""
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    path: list[str] = []
    variables: list[str] = []
    rows: list[dict[str, object]] = []
    row: dict[str, object] = {}
    binding = ""
    parts: list[str] = []
    attrs: dict[str, str] = {}
    answer: bool | None = None
    seen: set[str] = set()

    def deny(*arguments: object) -> NoReturn:
        """No declarations, entity definitions or external resource resolution."""
        raise InputError("XML declarations and external entities are unsupported")

    def start(name: str, attributes: dict[str, str]) -> None:
        """Accept only actual result vocabulary at its exact structural position."""
        nonlocal row, binding, parts, attrs
        budget.consume("work")
        budget.consume("states", len(attributes) + 1)
        bound_text(name, budget)
        for key, value in attributes.items():
            bound_text(key, budget)
            bound_text(value, budget)
        if not name.startswith(_NS):
            raise InputError("Unsupported SPARQL XML namespace")
        local = name[len(_NS) :]
        parents = {
            "sparql": (),
            "head": ("sparql",),
            "results": ("sparql",),
            "boolean": ("sparql",),
            "variable": ("sparql", "head"),
            "link": ("sparql", "head"),
            "result": ("sparql", "results"),
            "binding": ("sparql", "results", "result"),
            "uri": ("sparql", "results", "result", "binding"),
            "bnode": ("sparql", "results", "result", "binding"),
            "literal": ("sparql", "results", "result", "binding"),
        }
        if local not in parents or tuple(path) != parents[local]:
            raise InputError("Invalid SPARQL XML element placement")
        allowed = {
            "variable": {"name"},
            "link": {"href"},
            "binding": {"name"},
            "literal": {"datatype", _LANG},
        }.get(local, set())
        if set(attributes) - allowed:
            raise InputError("Unsupported SPARQL XML attributes")
        if local in {"head", "results", "boolean"}:
            if (
                local in seen
                or local in {"results", "boolean"}
                and ("head" not in seen or {"results", "boolean"} & seen)
            ):
                raise InputError("Duplicate or unordered SPARQL XML sections")
            seen.add(local)
        if local == "variable":
            if set(attributes) != {"name"}:
                raise InputError("XML variable requires its name")
            if "head-link" in seen:
                raise InputError("XML variables must precede head links")
            variables.append(attributes["name"])
        elif local == "link":
            if set(attributes) != {"href"}:
                raise InputError("XML link requires its href")
            seen.add("head-link")
        elif local == "result":
            row = {}
        elif local == "binding":
            if set(attributes) != {"name"} or attributes["name"] in row:
                raise InputError("Missing or duplicate XML binding name")
            binding = attributes["name"]
        elif local in {"literal", "uri", "bnode", "boolean"}:
            parts, attrs = [], dict(attributes)
        if len(path) >= budget.limits.max_depth:
            raise LimitExceeded("SPARQL XML depth exceeds max_depth")
        path.append(local)

    def data(text: str) -> None:
        """Bound callback text before retaining literal or boolean content."""
        bound_text(text, budget)
        budget.consume("states")
        if path and path[-1] in {"literal", "uri", "bnode", "boolean"}:
            parts.append(text)
        elif text.strip():
            raise InputError("Unexpected SPARQL XML character content")

    def end(name: str) -> None:
        """Publish only a complete term, row or strict ASK boolean."""
        nonlocal answer
        budget.consume("work")
        local = path.pop()
        if name != _NS + local:
            raise InputError("Unbalanced SPARQL XML element")
        if local in {"literal", "uri", "bnode", "boolean"}:
            budget.consume("bytes", sum(len(part) * 4 for part in parts))
            text = "".join(parts)
            if local == "boolean":
                if text.strip() not in {"true", "false"}:
                    raise InputError("XML ASK requires true or false")
                answer = text.strip() == "true"
            else:
                if binding in row:
                    raise InputError("XML binding contains multiple RDF terms")
                term: dict[str, object] = {"type": local, "value": text}
                if local == "literal":
                    if "datatype" in attrs and _LANG in attrs:
                        raise InputError("Literal datatype and language conflict")
                    if "datatype" in attrs:
                        term["datatype"] = attrs["datatype"]
                    if _LANG in attrs:
                        term["xml:lang"] = attrs[_LANG]
                row[binding] = term
        elif local == "binding" and binding not in row:
            raise InputError("XML binding lacks an RDF term")
        elif local == "result":
            budget.consume("states")
            rows.append(row)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = data
    parser.StartDoctypeDeclHandler = deny
    parser.EntityDeclHandler = deny
    parser.ExternalEntityRefHandler = deny
    try:
        for offset in range(0, len(payload), 4096):
            budget.consume("work")
            parser.Parse(payload[offset : offset + 4096], False)
        parser.Parse("", True)
    except expat.ExpatError as error:
        raise InputError(str(error)) from error
    if "head" not in seen or not {"results", "boolean"} & seen:
        raise InputError("SPARQL XML lacks required result sections")
    if answer is not None:
        if variables:
            raise InputError("ASK cannot declare SELECT variables")
        return {"head": {}, "boolean": answer}
    return {"head": {"vars": variables}, "results": {"bindings": rows}}
