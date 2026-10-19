"""Finite association, endpoint and qualifier policies over actual native facts."""

from importlib import import_module
from typing import Any, cast

from bijux_ontology.execution.limits import Budget, IssueCollector

from .observations import class_ancestors, resolve_element, rule_slots, slot_facets
from .preparation import reserve
from .wire import association_diagnostics, encode_constraints


def check_association(
    toolkit: Any,
    data: dict[str, object],
    policy: dict[str, object],
    index: int,
    budget: Budget,
    issues: IssueCollector,
    output_cap: int,
    response_size: list[int],
) -> dict[str, object] | None:
    """Apply selected finite Biolink machine contracts and retain unknown coverage."""
    format_element = import_module("bmt.utils").format_element
    meta = import_module("linkml_runtime.linkml_model.meta")
    ClassDefinition, SlotDefinition = meta.ClassDefinition, meta.SlotDefinition

    location = cast(str, data["location"])

    emit = association_diagnostics(
        location, index, budget, issues, output_cap, response_size
    )

    def element(value: str, kind: Any, path: str) -> Any:
        """Use actual native name/alias resolution while preserving original input."""
        selected = resolve_element(toolkit, value, budget)
        if not isinstance(selected, kind):
            emit(
                "unknown_element",
                "No selected model element has the required role",
                (path,),
            )
            return None
        if selected.abstract and not policy["allow_abstract"]:
            emit(
                "abstract_element",
                "Submitted abstract model element is forbidden by the selected policy",
                (path,),
            )
        if selected.mixin and not policy["allow_mixins"]:
            emit(
                "mixin_element",
                "Submitted mixin model element is forbidden by the selected policy",
                (path,),
            )
        return selected

    association = element(
        cast(str, data["association"]), ClassDefinition, "association"
    )
    subject = element(
        cast(str, data["subject_category"]), ClassDefinition, "subject_category"
    )
    predicate = element(cast(str, data["predicate"]), SlotDefinition, "predicate")
    obj = element(
        cast(str, data["object_category"]), ClassDefinition, "object_category"
    )
    if any(v is None for v in (association, subject, predicate, obj)):
        return None
    association_ancestors = class_ancestors(toolkit, association.name, budget)
    if "association" not in association_ancestors:
        emit(
            "association_role",
            "The selected class is not a native association category",
            ("association",),
        )
        return None
    for selected, field in ((subject, "subject_category"), (obj, "object_category")):
        ancestry = class_ancestors(toolkit, selected.name, budget)
        if "named thing" not in ancestry and not selected.mixin:
            emit("category_role", "The selected class is not a node category", (field,))
    if not toolkit.is_predicate(predicate.name):
        emit(
            "predicate_role",
            "The submitted slot is not a native Biolink predicate",
            ("predicate",),
        )
    class_slots = toolkit.view.class_slots(association.name)
    reserve(budget, len(class_slots))
    constraints: dict[str, object] = {
        "mixins_inherited": True,
        "policy": policy,
        "association_ancestors": association_ancestors,
        "slots": {},
    }
    slots = cast(dict[str, object], constraints["slots"])

    def belongs(
        category: Any, target: str | None, path: str, *, native_mixin: bool = False
    ) -> None:
        """Use native named ancestry; generic mixin treatment is explicit BMT policy."""
        if target is None or toolkit.view.get_class(target) is None:
            emit(
                "range_unverified",
                "The selected endpoint has no supported named category range",
                (path,),
                unknown=True,
            )
            return
        ancestors = class_ancestors(toolkit, category.name, budget)
        if target in ancestors:
            return
        if category.mixin:
            if native_mixin and target == "named thing" and policy["allow_mixins"]:
                return
            emit(
                "mixin_range_unverified",
                "Native mixin ancestry does not establish this narrower endpoint",
                (path,),
                unknown=True,
            )
        else:
            emit(
                "endpoint_range",
                "Submitted category is outside the selected native endpoint range",
                (path,),
            )

    for name, category, field in (
        ("subject", subject, "subject_category"),
        ("object", obj, "object_category"),
    ):
        effective = toolkit.view.induced_slot(name, association.name)
        slots[name] = slot_facets(effective, budget)
        belongs(category, effective.range, field)
        if cast(dict[str, object], slots[name])["unsupported"]:
            emit(
                "slot_unverified",
                "Selected endpoint has unsupported native assertion facets",
                (field,),
                unknown=True,
            )
    effective_predicate = toolkit.view.induced_slot("predicate", association.name)
    slots["predicate"] = slot_facets(effective_predicate, budget)
    if cast(dict[str, object], slots["predicate"])["unsupported"]:
        emit(
            "slot_unverified",
            "Base predicate has unsupported native facets",
            ("predicate",),
            unknown=True,
        )
    if effective_predicate.subproperty_of:
        if not toolkit.is_subproperty_of(
            effective_predicate.subproperty_of, predicate.name
        ):
            emit(
                "predicate_context",
                "Base predicate is outside the contextual predicate hierarchy",
                ("predicate",),
            )
    else:
        usage = toolkit.get_slot_usage(association, "predicate")
        if usage is not None and usage.description:
            emit(
                "predicate_unverified",
                "Description-only predicate usage has no machine constraint",
                ("predicate",),
                unknown=True,
            )
    native_edge = toolkit.validate_edge(
        format_element(subject), format_element(predicate), format_element(obj)
    )
    constraints["native_edge_answer"] = bool(native_edge)
    if not native_edge:
        emit(
            "predicate_endpoints",
            "Native toolkit rejects the submitted predicate/category combination",
            ("predicate",),
        )
    native_predicate = toolkit.view.induced_slot(predicate.name)
    slots["predicate_endpoints"] = slot_facets(native_predicate, budget)
    if cast(dict[str, object], slots["predicate_endpoints"])["unsupported"]:
        emit(
            "slot_unverified",
            "Predicate endpoints have unsupported native facets",
            ("predicate",),
            unknown=True,
        )
    belongs(
        subject,
        native_predicate.domain,
        "subject_category",
        native_mixin=bool(native_edge),
    )
    belongs(
        obj, native_predicate.range, "object_category", native_mixin=bool(native_edge)
    )
    qualifiers = cast(list[dict[str, object]], data["qualifiers"])
    qualified = predicate
    normalized: list[list[str]] = []
    occurrences: dict[str, int] = {}
    present = {"subject", "predicate", "object"}
    for number, qualifier in enumerate(qualifiers):
        budget.consume("work")
        path = ("qualifiers", number)
        reserve(budget)
        normalized.append(
            [cast(str, qualifier["type_id"]), cast(str, qualifier["value"])]
        )
        q = toolkit.get_element(cast(str, qualifier["type_id"]))
        if not isinstance(q, SlotDefinition) or not toolkit.is_qualifier(q.name):
            emit(
                "qualifier_unverified",
                "The selected model does not expose this qualifier type",
                (*path, "type_id"),
                unknown=True,
            )
            continue
        present.add(str(q.name))
        occurrences[q.name] = occurrences.get(q.name, 0) + 1
        if (
            q.abstract
            and not policy["allow_abstract"]
            or q.mixin
            and not policy["allow_mixins"]
        ):
            emit(
                "qualifier_policy",
                "Submitted qualifier is abstract/mixin under a rejecting policy",
                (*path, "type_id"),
            )
        if q.name not in class_slots:
            emit(
                "qualifier_context",
                "Qualifier is not permitted in the selected association context",
                (*path, "type_id"),
            )
            continue
        effective = toolkit.view.induced_slot(q.name, association.name)
        slots[str(q.name)] = slot_facets(effective, budget)
        if cast(dict[str, object], slots[str(q.name)])["unsupported"]:
            emit(
                "qualifier_unverified",
                "Selected qualifier has unsupported native assertion facets",
                path,
                unknown=True,
            )
        value = cast(str, qualifier["value"])
        native_value = value
        if q.name == "qualified predicate":
            selected = element(value, SlotDefinition, "qualified_predicate")
            if selected is None:
                continue
            qualified = selected
            native_value = str(selected.name)
            if not toolkit.is_predicate(selected.name):
                emit(
                    "qualified_predicate",
                    "Qualified predicate is not in the native predicate hierarchy",
                    (*path, "value"),
                )
            if not effective.subproperty_of:
                emit(
                    "qualifier_unverified",
                    "Qualified predicate lacks a contextual subproperty contract",
                    path,
                    unknown=True,
                )
            else:
                # BMT expects native canonical names in this actual code path.
                # Original CURIE/value remains untouched in the submitted record.
                valid = toolkit.validate_qualifier(
                    str(q.name), native_value, [str(association.name)]
                )
                included = toolkit.is_subproperty_of(
                    effective.subproperty_of, native_value
                )
                if not included:
                    emit(
                        "qualifier_value",
                        "Qualified predicate is outside the selected association range",
                        (*path, "value"),
                    )
                elif not valid:
                    emit(
                        "qualifier_unverified",
                        "Native qualifier API did not establish contextual inheritance",
                        path,
                        unknown=True,
                    )
                endpoint = toolkit.view.induced_slot(selected.name)
                slots["qualified_predicate_endpoints"] = slot_facets(endpoint, budget)
                if cast(dict[str, object], slots["qualified_predicate_endpoints"])[
                    "unsupported"
                ]:
                    emit(
                        "slot_unverified",
                        "Qualified predicate has unsupported facets",
                        path,
                        unknown=True,
                    )
                belongs(
                    subject,
                    endpoint.domain,
                    "subject_category",
                    native_mixin=bool(
                        toolkit.validate_edge(
                            format_element(subject),
                            format_element(selected),
                            format_element(obj),
                        )
                    ),
                )
                belongs(
                    obj,
                    endpoint.range,
                    "object_category",
                    native_mixin=bool(
                        toolkit.validate_edge(
                            format_element(subject),
                            format_element(selected),
                            format_element(obj),
                        )
                    ),
                )
        elif effective.range and toolkit.is_enum(effective.range):
            valid = toolkit.is_permissible_value_of_enum(effective.range, value)
            native = toolkit.validate_qualifier(
                str(q.name), value, [str(association.name)]
            )
            if not valid:
                emit(
                    "qualifier_value",
                    "Qualifier value is outside its effective association enumeration",
                    (*path, "value"),
                )
            elif not native:
                emit(
                    "qualifier_unverified",
                    "Native qualifier API did not establish the effective value",
                    path,
                    unknown=True,
                )
        elif effective.range and toolkit.view.get_class(effective.range) is not None:
            native = toolkit.validate_qualifier(
                str(q.name), value, [str(association.name)]
            )
            categories = toolkit.get_element_by_prefix(value)
            if not native or effective.range not in categories:
                emit(
                    "qualifier_value",
                    "Native identifier-prefix categories do not match contextual range",
                    (*path, "value"),
                )
        else:
            emit(
                "qualifier_unverified",
                "Qualifier has no supported explicit enum/category context",
                path,
                unknown=True,
            )
        normalized[-1] = [
            str(format_element(q)),
            str(format_element(qualified))
            if q.name == "qualified predicate"
            else value,
        ]
        if not effective.multivalued and occurrences[q.name] > 1:
            emit(
                "qualifier_cardinality",
                "Multiple occurrences violate the native scalar qualifier contract",
                path,
            )
        if (
            effective.maximum_cardinality is not None
            and occurrences[q.name] > effective.maximum_cardinality
        ):
            emit(
                "qualifier_cardinality",
                "Qualifier occurrences exceed the native maximum",
                path,
            )
    for name in class_slots:
        budget.consume("work")
        if toolkit.is_qualifier(name):
            slot = toolkit.view.induced_slot(name, association.name)
            if (slot.required or slot.minimum_cardinality) and occurrences.get(
                name, 0
            ) < max(1 if slot.required else 0, slot.minimum_cardinality or 0):
                emit(
                    "required_qualifier",
                    "A required contextual qualifier is absent",
                    ("qualifiers",),
                )
    for ancestor in association_ancestors:
        cls = toolkit.view.get_class(ancestor)
        for rule in cls.rules or ():
            pre = rule_slots(rule.preconditions, budget)
            post = rule_slots(rule.postconditions, budget)
            if (not pre or pre.intersection(present)) and (pre | post).intersection(
                present
            ):
                emit(
                    "rule_unverified",
                    "A relevant native class rule needs unsupported checks",
                    unknown=True,
                )
        if cls.classification_rules:
            emit(
                "rule_unverified",
                "Selected association has unsupported native classification rules",
                unknown=True,
            )
    constraints_json = encode_constraints(constraints, budget)
    return {
        "association": str(format_element(association)),
        "subject_category": str(format_element(subject)),
        "predicate": str(format_element(predicate)),
        "object_category": str(format_element(obj)),
        "qualified_predicate": str(format_element(qualified)),
        "qualifiers": normalized,
        "constraints_json": constraints_json,
    }
