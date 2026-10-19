"""Public JSON-shaped declaration ingestion shared by API and console callers."""

from decimal import Decimal
from typing import Literal, cast

from rdflib import Literal as RDFLiteral
from rdflib import URIRef
from rdflib.namespace import XSD

from bijux_ontology._json import (
    array_value,
    at_input_location,
    boolean_value,
    input_location,
    known_keys,
    object_value,
    optional_string_value,
    string_value,
    string_values,
)
from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.declarations.resource_roles import (
    ResourceKind,
    ResourceRole,
    RoleBridge,
)
from bijux_ontology.declarations.types import (
    Emission,
    EmissionCondition,
    EmissionCoverage,
    EmissionGroup,
    EmissionScenario,
    FieldDeclaration,
    ModelDeclaration,
    OutputRequirement,
    SubjectIdentity,
    TermDeclaration,
    ValueDeclaration,
    ValueKind,
)
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag
from bijux_ontology.rdf.literals import check_literal


def terms_from_data(value: object, *, source: str = "") -> tuple[TermDeclaration, ...]:
    """Ingest explicit term inventories through the validated public constructors."""
    result = []
    for index, item in enumerate(array_value(value, source=source)):
        with at_input_location(source, (index,)):
            if isinstance(item, str):
                result.append(
                    TermDeclaration(item, location=input_location(source, (index,)))
                )
                continue
            term = object_value(
                item, location="", message="Expected a JSON object with string keys"
            )
            known_keys(
                term,
                {"iri", "kinds", "location"},
                location="",
                message="Unknown declaration keys",
            )
            result.append(
                TermDeclaration(
                    string_value(term.get("iri")),
                    string_values(term.get("kinds", [])),
                    string_value(
                        term.get("location", input_location(source, (index,)))
                    ),
                )
            )
    return tuple(result)


def _trait(value: object) -> str | Absent | None:
    """Keep an authored absence requirement distinct from omitted metadata."""
    if isinstance(value, dict) and set(value) == {"absent"} and value["absent"] is True:
        return ABSENT
    return optional_string_value(value)


def _value(value: object) -> ValueDeclaration:
    data = object_value(
        value, location="", message="Expected a JSON object with string keys"
    )
    known_keys(
        data,
        {
            "kind",
            "datatype",
            "language",
            "direction",
            "types",
            "model",
            "container",
            "samples",
            "constraints",
            "role",
        },
        location="",
        message="Unknown declaration keys",
    )
    kind = string_value(data.get("kind", "unknown"))
    if kind not in {"literal", "iri", "object", "unknown"}:
        raise InputError(f"Unknown value kind: {kind}")
    return ValueDeclaration(
        kind=cast(ValueKind, kind),
        datatype=optional_string_value(data.get("datatype")),
        language=_trait(data.get("language")),
        direction=cast(
            Literal["ltr", "rtl"] | Absent | None, _trait(data.get("direction"))
        ),
        types=string_values(data.get("types", [])),
        model=optional_string_value(data.get("model")),
        container=optional_string_value(data.get("container")),
        constraints=_constraints(data.get("constraints")),
        role=_resource_role(data.get("role")),
        samples=tuple(
            _sample(sample) for sample in array_value(data.get("samples", []))
        ),
    )


def _resource_role(value: object) -> ResourceRole | None:
    """Read explicit finite facet policies without inferring RDF intent."""
    if value is None:
        return None
    data = object_value(value, location="role")
    known_keys(
        data, {"kind", "types", "type_mode", "punning", "bridge"}, location="role"
    )
    bridge = None
    if data.get("bridge") is not None:
        raw = object_value(data["bridge"], location="role.bridge")
        known_keys(raw, {"predicate", "source_role", "reverse"}, location="role.bridge")
        bridge = RoleBridge(
            string_value(raw.get("predicate")),
            source_role=cast(
                ResourceKind, string_value(raw.get("source_role", "concept"))
            ),
            reverse=boolean_value(raw.get("reverse", False)),
        )
    return ResourceRole(
        cast(ResourceKind, string_value(data.get("kind"))),
        types=string_values(data.get("types", [])),
        type_mode=cast(
            Literal["asserted", "named"],
            string_value(data.get("type_mode", "asserted")),
        ),
        punning=cast(
            Literal["allow", "forbid"], string_value(data.get("punning", "forbid"))
        ),
        bridge=bridge,
    )


def _constraints(value: object) -> ValueConstraints | None:
    """Decode explicit predicates with exact RDF enums and decimal text bounds."""
    if value is None:
        return None
    data = object_value(value, location="constraints")
    known_keys(
        data,
        {
            "enumeration",
            "minimum",
            "maximum",
            "minimum_inclusive",
            "maximum_inclusive",
            "min_length",
            "max_length",
            "pattern",
            "pattern_mode",
            "iri_namespaces",
        },
        location="constraints",
    )
    enumeration: tuple[URIRef | RDFLiteral, ...] | None = None
    if data.get("enumeration") is not None:
        values = array_value(data["enumeration"])
        if len(values) > Limits().max_states:
            raise InputError("Value enumeration exceeds supported declaration size")
        terms: list[URIRef | RDFLiteral] = []
        for index in range(len(values)):
            item = object_value(values[index], location="enumeration")
            if set(item) == {"iri"}:
                terms.append(URIRef(absolute_iri(string_value(item["iri"]))))
            else:
                terms.append(_sample(item))
        enumeration = tuple(terms)
    bounds: dict[str, Decimal | None] = {}
    for name in ("minimum", "maximum"):
        raw = data.get(name)
        if raw is None:
            bounds[name] = None
        else:
            text = string_value(raw)
            sample = _sample({"lexical": text, "datatype": str(XSD.decimal)})
            if not check_literal(sample).verified:
                raise InputError(
                    "Numeric bounds require finite XSD decimal text", location=name
                )
            bounds[name] = Decimal(text)
    lengths: dict[str, int | None] = {}
    for name in ("min_length", "max_length"):
        raw = data.get(name)
        if raw is not None and type(raw) is not int:
            raise InputError(
                "String length bounds require integers or null", location=name
            )
        lengths[name] = raw
    namespaces = array_value(data.get("iri_namespaces", []))
    if len(namespaces) > Limits().max_states:
        raise InputError("IRI namespaces exceed supported declaration size")
    mode = string_value(data.get("pattern_mode", "fullmatch"))
    if mode not in ("fullmatch", "search"):
        raise InputError("Pattern mode must be fullmatch or search")
    return ValueConstraints(
        enumeration=enumeration,
        minimum=bounds["minimum"],
        maximum=bounds["maximum"],
        minimum_inclusive=boolean_value(data.get("minimum_inclusive", True)),
        maximum_inclusive=boolean_value(data.get("maximum_inclusive", True)),
        min_length=lengths["min_length"],
        max_length=lengths["max_length"],
        pattern=optional_string_value(data.get("pattern")),
        pattern_mode=cast(Literal["fullmatch", "search"], mode),
        iri_namespaces=tuple(
            string_value(namespaces[index]) for index in range(len(namespaces))
        ),
    )


def _branch(
    value: object, source: str, path: tuple[str | int, ...]
) -> ValueDeclaration:
    with at_input_location(source, path):
        return _value(value)


def models_from_data(
    value: object, *, source: str = ""
) -> tuple[ModelDeclaration, ...]:
    """Ingest model contracts using the same invariants as direct Python callers."""
    result = []
    for index, item in enumerate(array_value(value, source=source)):
        with at_input_location(source, (index,)):
            model = object_value(
                item, location="", message="Expected a JSON object with string keys"
            )
            known_keys(
                model,
                {
                    "identifier",
                    "types",
                    "fields",
                    "location",
                    "subject",
                    "emission_groups",
                    "coverage",
                },
                location="",
                message="Unknown declaration keys",
            )
            fields = []
            for field_index, raw_field in enumerate(
                array_value(model.get("fields", []))
            ):
                with at_input_location(source, (index, "fields", field_index)):
                    field = object_value(
                        raw_field,
                        location="",
                        message="Expected a JSON object with string keys",
                    )
                    known_keys(
                        field,
                        {
                            "name",
                            "predicate",
                            "values",
                            "key",
                            "optional",
                            "location",
                            "reverse",
                            "emission",
                            "output",
                        },
                        location="",
                        message="Unknown declaration keys",
                    )
                    fields.append(
                        FieldDeclaration(
                            name=string_value(field.get("name")),
                            predicate=string_value(field.get("predicate")),
                            values=tuple(
                                _branch(
                                    branch,
                                    source,
                                    (
                                        index,
                                        "fields",
                                        field_index,
                                        "values",
                                        branch_index,
                                    ),
                                )
                                for branch_index, branch in enumerate(
                                    array_value(field.get("values"))
                                )
                            ),
                            key=optional_string_value(field.get("key")),
                            optional=boolean_value(field.get("optional", False)),
                            output=_output(field.get("output")),
                            location=string_value(
                                field.get(
                                    "location",
                                    input_location(
                                        source, (index, "fields", field_index)
                                    ),
                                )
                            ),
                            reverse=boolean_value(field.get("reverse", False)),
                            emission=cast(
                                Emission, string_value(field.get("emission", "unknown"))
                            ),
                        )
                    )
            result.append(
                ModelDeclaration(
                    string_value(model.get("identifier")),
                    string_values(model.get("types")),
                    tuple(fields),
                    string_value(
                        model.get("location", input_location(source, (index,)))
                    ),
                    subject=_subject(model.get("subject", {})),
                    emission_groups=tuple(
                        _group(group)
                        for group in array_value(model.get("emission_groups", []))
                    ),
                    coverage=_coverage(model.get("coverage")),
                )
            )
    return tuple(result)


def _coverage(value: object) -> EmissionCoverage | None:
    if value is None:
        return None
    data = object_value(value, location="coverage")
    known_keys(
        data,
        {"extensions", "reverse_extensions", "metadata", "aliases"},
        location="coverage",
        message="Unknown emission coverage keys",
    )
    aliases = []
    for pair in array_value(data.get("aliases", [])):
        values = string_values(pair)
        if len(values) != 2:
            raise InputError("Coverage aliases require source-key/field-name pairs")
        aliases.append((values[0], values[1]))
    return EmissionCoverage(
        extensions=string_values(data.get("extensions", [])),
        reverse_extensions=string_values(data.get("reverse_extensions", [])),
        metadata=string_values(data.get("metadata", ["@context"])),
        aliases=tuple(aliases),
    )


def _sample(value: object) -> "RDFLiteral":
    data = object_value(
        value, location="", message="Expected a JSON object with string keys"
    )
    known_keys(
        data,
        {"lexical", "datatype", "language"},
        location="",
        message="Unknown declaration keys",
    )
    lexical = string_value(data.get("lexical"))
    datatype = optional_string_value(data.get("datatype"))
    language = optional_string_value(data.get("language"))
    if datatype is not None and language is not None:
        raise InputError("Literal samples use a datatype or language, not both")
    if datatype is not None:
        absolute_iri(datatype)
    if language is not None:
        language_tag(language)
    sample = RDFLiteral(lexical, datatype=datatype, lang=language, normalize=False)
    if str(sample) != lexical:
        raise InputError("RDFLib cannot preserve the supplied sample lexical form")
    return sample


def _subject(value: object) -> SubjectIdentity:
    data = object_value(
        value, location="", message="Expected a subject identity object"
    )
    known_keys(
        data,
        {"expected_iri", "allow_blank"},
        location="",
        message="Unknown subject identity keys",
    )
    return SubjectIdentity(
        expected_iri=optional_string_value(data.get("expected_iri")),
        allow_blank=boolean_value(data.get("allow_blank", True)),
    )


def _output(value: object) -> OutputRequirement | None:
    if value is None:
        return None
    data = object_value(value, location="output")
    known_keys(
        data,
        {
            "require_property",
            "allow_null",
            "allow_empty",
            "minimum_values",
            "maximum_values",
            "minimum_list_members",
            "maximum_list_members",
        },
        location="output",
    )
    counts: dict[str, int | None] = {}
    for name in (
        "minimum_values",
        "maximum_values",
        "minimum_list_members",
        "maximum_list_members",
    ):
        count = data.get(name)
        if count is not None and type(count) is not int:
            raise InputError("Output counts require integers or null", location=name)
        counts[name] = count
    return OutputRequirement(
        require_property=boolean_value(data.get("require_property", False)),
        allow_null=boolean_value(data.get("allow_null", False)),
        allow_empty=boolean_value(data.get("allow_empty", False)),
        minimum_values=counts["minimum_values"],
        maximum_values=counts["maximum_values"],
        minimum_list_members=counts["minimum_list_members"],
        maximum_list_members=counts["maximum_list_members"],
    )


def _condition(value: object) -> EmissionCondition | None:
    if value is None:
        return None
    data = object_value(value, location="condition")
    known_keys(data, {"field", "equals"}, location="condition")
    expected = object_value(data.get("equals"), location="condition.equals")
    term: URIRef | RDFLiteral
    if "iri" in expected:
        known_keys(expected, {"iri"}, location="condition.equals")
        term = URIRef(absolute_iri(string_value(expected["iri"])))
    else:
        term = _sample(expected)
    return EmissionCondition(string_value(data.get("field")), term)


def _group(value: object) -> EmissionGroup:
    data = object_value(value, location="emission_groups")
    known_keys(
        data,
        {"identifier", "fields", "minimum", "maximum", "scenarios", "closed"},
        location="emission_groups",
    )
    minimum, maximum = data.get("minimum", 0), data.get("maximum")
    if type(minimum) is not int or (maximum is not None and type(maximum) is not int):
        raise InputError("Emission bounds require integers or null maximum")
    scenarios = []
    for value in array_value(data.get("scenarios", [])):
        scenario = object_value(value, location="scenario")
        known_keys(
            scenario, {"identifier", "present", "condition"}, location="scenario"
        )
        scenarios.append(
            EmissionScenario(
                string_value(scenario.get("identifier")),
                string_values(scenario.get("present")),
                _condition(scenario.get("condition")),
            )
        )
    return EmissionGroup(
        string_value(data.get("identifier")),
        string_values(data.get("fields")),
        minimum=minimum,
        maximum=maximum,
        scenarios=tuple(scenarios),
        closed=boolean_value(data.get("closed", False)),
    )
