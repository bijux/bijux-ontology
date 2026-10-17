"""Bounded LinkML extraction through maintained induced-slot inspection."""

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import TYPE_CHECKING

from bijux_ontology.declarations.types import (
    Emission,
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.interfaces.adapters.contracts import ModelExtraction, RdfField
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.reports import Issue, Report

if TYPE_CHECKING:
    from linkml_runtime.utils.schemaview import SchemaView  # type: ignore[import-untyped]


def model_declarations(
    view: "SchemaView",
    *,
    classes: Sequence[str] | None = None,
    identifiers: Mapping[str, str] | None = None,
    field_contracts: Mapping[str, Mapping[str, RdfField]] | None = None,
    limits: Limits | None = None,
) -> ModelExtraction:
    """Use explicit URI mappings and caller-known RDF output contracts.

    Supply a schema whose imports have deliberately been merged by the caller.
    Extraction performs no import retrieval and does not mutate the supplied
    view. LinkML owns inheritance, induced slots and submission validation;
    required fields never imply guaranteed RDF serialization.
    """
    try:
        from linkml_runtime.utils.schemaview import SchemaView
    except ImportError as error:
        raise ResourceError("Install bijux-ontology[linkml]") from error
    if not isinstance(view, SchemaView):
        raise InputError("Expected a LinkML SchemaView")
    if view.schema.imports:
        raise ResourceError(
            "Supply a deliberately merged LinkML schema; "
            "extraction does not retrieve imports"
        )
    if isinstance(classes, (str, bytes)):
        raise InputError("Selected classes must be a sequence of names")
    owned = SchemaView(deepcopy(view.schema))
    names = (
        tuple(owned.all_classes(imports=False)) if classes is None else tuple(classes)
    )
    declarations = []
    budget = Budget(limits if limits is not None else Limits())
    issues = IssueCollector(budget)
    checked = 0
    complete = True
    try:
        for name in names:
            budget.consume("work")
            cls = owned.get_class(name, imports=False)
            if cls is None:
                raise InputError("Selected LinkML class is absent", location=name)
            identifier = (identifiers or {}).get(name, name)
            checked += 1
            types: tuple[str, ...]
            if cls.class_uri:
                types = (absolute_iri(owned.expand_curie(str(cls.class_uri))),)
            else:
                types = ()
                issues.append(
                    Issue(
                        "adapter.class_mapping_unverified",
                        "LinkML class has no explicit class_uri",
                        identifier,
                        kind="unverified",
                    )
                )
            fields = []
            for slot in owned.class_induced_slots(name):
                budget.consume("work")
                checked += 1
                location = f"{identifier}.{slot.name}"
                if not slot.slot_uri:
                    issues.append(
                        Issue(
                            "adapter.mapping_unverified",
                            "Induced slot has no explicit slot_uri",
                            location,
                            kind="unverified",
                        )
                    )
                    continue
                predicate = absolute_iri(owned.expand_curie(str(slot.slot_uri)))
                contract = (field_contracts or {}).get(name, {}).get(str(slot.name))
                if contract is not None and not isinstance(contract, RdfField):
                    raise InputError(
                        "Expected explicit RDF field contracts", location=location
                    )
                values: tuple[ValueDeclaration, ...]
                emission: Emission
                if contract is None or not contract.output_contract:
                    values = (ValueDeclaration(),)
                    emission = "unknown"
                    reverse = False
                    issues.append(
                        Issue(
                            "adapter.output_unverified",
                            "LinkML schema does not establish the selected "
                            "serializer's RDF output",
                            location,
                            kind="unverified",
                        )
                    )
                else:
                    values = contract.values
                    emission = contract.emission
                    reverse = contract.reverse
                    if contract.predicate != predicate:
                        issues.append(
                            Issue(
                                "adapter.predicate_contract_conflict",
                                "Explicit output contract differs from slot_uri",
                                location,
                                expected=predicate,
                                actual=contract.predicate,
                            )
                        )
                fields.append(
                    FieldDeclaration(
                        str(slot.name),
                        predicate,
                        values,
                        reverse=reverse,
                        emission=emission,
                        location=location,
                    )
                )
                if slot.alias and slot.alias != slot.name:
                    issues.append(
                        Issue(
                            "adapter.alias_unverified",
                            "LinkML alias requires representative "
                            "serializer verification",
                            location,
                            kind="unverified",
                        )
                    )
            declarations.append(ModelDeclaration(identifier, types, tuple(fields)))
    except LimitExceeded as error:
        complete = False
        issues.exhausted(str(error))
    return ModelExtraction(
        tuple(declarations),
        Report(
            "linkml-extraction",
            checked,
            tuple(issues),
            complete=complete,
            requested=checked if complete else checked + 1,
            families=("explicit-serialization",),
        ),
    )
