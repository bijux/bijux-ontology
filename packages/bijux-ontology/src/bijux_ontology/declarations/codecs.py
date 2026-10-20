"""Public JSON-shaped declaration ingestion shared by API and console callers."""

from decimal import Decimal
from typing import Literal, cast

from rdflib import Literal as RDFLiteral
from rdflib import URIRef
from rdflib.namespace import XSD

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
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.budgets import operation
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import Budget, Limits, bound_text
from bijux_ontology.json.values import (
    array_value,
    at_input_location,
    boolean_value,
    input_location,
    known_keys,
    object_value,
    string_value,
)
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag
from bijux_ontology.rdf.literals.checking import check_literal


def terms_from_data(
    value: object,
    *,
    source: str = "",
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> tuple[TermDeclaration, ...]:
    """Decode term declarations under one optional cumulative ingestion allowance."""
    allowance = operation(limits, budget)
    bound_text(source, allowance)
    with at_input_location(source, ()):
        return _Decoder(allowance).terms(value, source=source)


def models_from_data(
    value: object,
    *,
    source: str = "",
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> tuple[ModelDeclaration, ...]:
    """Decode nested model contracts without resetting caller-selected limits."""
    allowance = operation(limits, budget)
    bound_text(source, allowance)
    with at_input_location(source, ()):
        return _Decoder(allowance).models(value, source=source)


class _Decoder:
    """Materialise one declaration inventory within its caller-owned allowance."""

    def __init__(self, budget: Budget) -> None:
        self.budget = budget

    def _array(self, value: object, *, source: str = "") -> tuple[object, ...]:
        """Capture only this container's references, reusing already owned JSON."""
        return capture(array_value(value, source=source), self.budget)

    def _object(
        self, value: object, *, location: str, message: str = "Expected an object"
    ) -> dict[str, object]:
        """Prepay shallow object ownership and keys before domain validation."""
        if not isinstance(value, dict):
            raise InputError(message, location=location)
        count = len(value)
        self.budget.consume("states", count + 1)
        self.budget.consume("bytes", 256 + 96 * count)
        self.budget.consume("work", count)
        original = object_value(value, location=location, message=message)
        result = {}
        iterator = iter(original)
        for _ in range(count):
            self.budget.consume("work")
            try:
                key = next(iterator)
                bound_text(key, self.budget)
                result[key] = original[key]
            except (KeyError, StopIteration, RuntimeError) as error:
                raise InputError(
                    "Declaration object changed during capture", location=location
                ) from error
        return result

    def _string(self, value: object) -> str:
        text = string_value(value)
        bound_text(text, self.budget)
        return text

    def _optional(self, value: object) -> str | None:
        return None if value is None else self._string(value)

    def _strings(self, value: object) -> tuple[str, ...]:
        return tuple(self._string(item) for item in self._array(value))

    def terms(self, value: object, *, source: str = "") -> tuple[TermDeclaration, ...]:
        """Ingest explicit terms through validated public constructors."""
        result = []
        for index, item in enumerate(self._array(value, source=source)):
            with at_input_location(source, (index,)):
                if isinstance(item, str):
                    result.append(
                        TermDeclaration(
                            self._string(item),
                            location=input_location(source, (index,)),
                        )
                    )
                    continue
                term = self._object(
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
                        self._string(term.get("iri")),
                        self._strings(term.get("kinds", [])),
                        self._string(
                            term.get("location", input_location(source, (index,)))
                        ),
                    )
                )
        return tuple(result)

    def _trait(self, value: object) -> str | Absent | None:
        """Keep an authored absence requirement distinct from omitted metadata."""
        if isinstance(value, dict):
            owned = self._object(value, location="")
            if set(owned) == {"absent"} and owned["absent"] is True:
                return ABSENT
        return self._optional(value)

    def _value(self, value: object) -> ValueDeclaration:
        data = self._object(
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
        kind = self._string(data.get("kind", "unknown"))
        if kind not in {"literal", "iri", "object", "unknown"}:
            raise InputError(f"Unknown value kind: {kind}")
        return ValueDeclaration(
            kind=cast(ValueKind, kind),
            datatype=self._optional(data.get("datatype")),
            language=self._trait(data.get("language")),
            direction=cast(
                Literal["ltr", "rtl"] | Absent | None,
                self._trait(data.get("direction")),
            ),
            types=self._strings(data.get("types", [])),
            model=self._optional(data.get("model")),
            container=self._optional(data.get("container")),
            constraints=self._constraints(data.get("constraints")),
            role=self._resource_role(data.get("role")),
            samples=tuple(
                self._sample(sample) for sample in self._array(data.get("samples", []))
            ),
        )

    def _resource_role(self, value: object) -> ResourceRole | None:
        """Read explicit finite facet policies without inferring RDF intent."""
        if value is None:
            return None
        data = self._object(value, location="role")
        known_keys(
            data, {"kind", "types", "type_mode", "punning", "bridge"}, location="role"
        )
        bridge = None
        if data.get("bridge") is not None:
            raw = self._object(data["bridge"], location="role.bridge")
            known_keys(
                raw, {"predicate", "source_role", "reverse"}, location="role.bridge"
            )
            bridge = RoleBridge(
                self._string(raw.get("predicate")),
                source_role=cast(
                    ResourceKind, self._string(raw.get("source_role", "concept"))
                ),
                reverse=boolean_value(raw.get("reverse", False)),
                budget=self.budget,
            )
        return ResourceRole(
            cast(ResourceKind, self._string(data.get("kind"))),
            types=self._strings(data.get("types", [])),
            type_mode=cast(
                Literal["asserted", "named"],
                self._string(data.get("type_mode", "asserted")),
            ),
            punning=cast(
                Literal["allow", "forbid"], self._string(data.get("punning", "forbid"))
            ),
            bridge=bridge,
            budget=self.budget,
        )

    def _constraints(self, value: object) -> ValueConstraints | None:
        """Decode explicit predicates with exact RDF enums and decimal text bounds."""
        if value is None:
            return None
        data = self._object(value, location="constraints")
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
            values = self._array(data["enumeration"])
            terms: list[URIRef | RDFLiteral] = []
            for index in range(len(values)):
                item = self._object(values[index], location="enumeration")
                if set(item) == {"iri"}:
                    terms.append(URIRef(absolute_iri(self._string(item["iri"]))))
                else:
                    terms.append(self._sample(item))
            enumeration = tuple(terms)
        bounds: dict[str, Decimal | None] = {}
        for name in ("minimum", "maximum"):
            raw = data.get(name)
            if raw is None:
                bounds[name] = None
            else:
                text = self._string(raw)
                sample = self._sample({"lexical": text, "datatype": str(XSD.decimal)})
                if not check_literal(sample, budget=self.budget).verified:
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
        namespaces = self._array(data.get("iri_namespaces", []))
        mode = self._string(data.get("pattern_mode", "fullmatch"))
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
            pattern=self._optional(data.get("pattern")),
            pattern_mode=cast(Literal["fullmatch", "search"], mode),
            iri_namespaces=tuple(
                self._string(namespaces[index]) for index in range(len(namespaces))
            ),
        )

    def _branch(
        self, value: object, source: str, path: tuple[str | int, ...]
    ) -> ValueDeclaration:
        with at_input_location(source, path):
            return self._value(value)

    def models(
        self, value: object, *, source: str = ""
    ) -> tuple[ModelDeclaration, ...]:
        """Ingest model contracts using the same invariants as direct Python callers."""
        result = []
        for index, item in enumerate(self._array(value, source=source)):
            with at_input_location(source, (index,)):
                model = self._object(
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
                with at_input_location(source, (index, "fields")):
                    raw_fields = self._array(model.get("fields", []))
                for field_index, raw_field in enumerate(raw_fields):
                    with at_input_location(source, (index, "fields", field_index)):
                        field = self._object(
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
                                name=self._string(field.get("name")),
                                predicate=self._string(field.get("predicate")),
                                values=tuple(
                                    self._branch(
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
                                        self._array(field.get("values"))
                                    )
                                ),
                                key=self._optional(field.get("key")),
                                optional=boolean_value(field.get("optional", False)),
                                output=self._output(field.get("output")),
                                location=self._string(
                                    field.get(
                                        "location",
                                        input_location(
                                            source, (index, "fields", field_index)
                                        ),
                                    )
                                ),
                                reverse=boolean_value(field.get("reverse", False)),
                                emission=cast(
                                    Emission,
                                    self._string(field.get("emission", "unknown")),
                                ),
                            )
                        )
                result.append(
                    ModelDeclaration(
                        self._string(model.get("identifier")),
                        self._strings(model.get("types")),
                        tuple(fields),
                        self._string(
                            model.get("location", input_location(source, (index,)))
                        ),
                        subject=self._subject(model.get("subject", {})),
                        emission_groups=tuple(
                            self._group(group)
                            for group in self._array(model.get("emission_groups", []))
                        ),
                        coverage=self._coverage(model.get("coverage")),
                    )
                )
        return tuple(result)

    def _coverage(self, value: object) -> EmissionCoverage | None:
        if value is None:
            return None
        data = self._object(value, location="coverage")
        known_keys(
            data,
            {"extensions", "reverse_extensions", "metadata", "aliases"},
            location="coverage",
            message="Unknown emission coverage keys",
        )
        aliases = []
        for pair in self._array(data.get("aliases", [])):
            values = self._strings(pair)
            if len(values) != 2:
                raise InputError("Coverage aliases require source-key/field-name pairs")
            aliases.append((values[0], values[1]))
        return EmissionCoverage(
            extensions=self._strings(data.get("extensions", [])),
            reverse_extensions=self._strings(data.get("reverse_extensions", [])),
            metadata=self._strings(data.get("metadata", ["@context"])),
            aliases=tuple(aliases),
        )

    def _sample(self, value: object) -> "RDFLiteral":
        data = self._object(
            value, location="", message="Expected a JSON object with string keys"
        )
        known_keys(
            data,
            {"lexical", "datatype", "language"},
            location="",
            message="Unknown declaration keys",
        )
        lexical = self._string(data.get("lexical"))
        datatype = self._optional(data.get("datatype"))
        language = self._optional(data.get("language"))
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

    def _subject(self, value: object) -> SubjectIdentity:
        data = self._object(
            value, location="", message="Expected a subject identity object"
        )
        known_keys(
            data,
            {"expected_iri", "allow_blank"},
            location="",
            message="Unknown subject identity keys",
        )
        return SubjectIdentity(
            expected_iri=self._optional(data.get("expected_iri")),
            allow_blank=boolean_value(data.get("allow_blank", True)),
        )

    def _output(self, value: object) -> OutputRequirement | None:
        if value is None:
            return None
        data = self._object(value, location="output")
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
                raise InputError(
                    "Output counts require integers or null", location=name
                )
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

    def _condition(self, value: object) -> EmissionCondition | None:
        if value is None:
            return None
        data = self._object(value, location="condition")
        known_keys(data, {"field", "equals"}, location="condition")
        expected = self._object(data.get("equals"), location="condition.equals")
        term: URIRef | RDFLiteral
        if "iri" in expected:
            known_keys(expected, {"iri"}, location="condition.equals")
            term = URIRef(absolute_iri(self._string(expected["iri"])))
        else:
            term = self._sample(expected)
        return EmissionCondition(self._string(data.get("field")), term)

    def _group(self, value: object) -> EmissionGroup:
        data = self._object(value, location="emission_groups")
        known_keys(
            data,
            {"identifier", "fields", "minimum", "maximum", "scenarios", "closed"},
            location="emission_groups",
        )
        minimum, maximum = data.get("minimum", 0), data.get("maximum")
        if type(minimum) is not int or (
            maximum is not None and type(maximum) is not int
        ):
            raise InputError("Emission bounds require integers or null maximum")
        scenarios = []
        for value in self._array(data.get("scenarios", [])):
            scenario = self._object(value, location="scenario")
            known_keys(
                scenario, {"identifier", "present", "condition"}, location="scenario"
            )
            scenarios.append(
                EmissionScenario(
                    self._string(scenario.get("identifier")),
                    self._strings(scenario.get("present")),
                    self._condition(scenario.get("condition")),
                )
            )
        return EmissionGroup(
            self._string(data.get("identifier")),
            self._strings(data.get("fields")),
            minimum=minimum,
            maximum=maximum,
            scenarios=tuple(scenarios),
            closed=boolean_value(data.get("closed", False)),
        )
