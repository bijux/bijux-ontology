"""Maintained BMT and LinkML interpretation inside one owned offline process."""

import json
import platform
import socket
import sys
from hashlib import sha256
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import Any, cast

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.patches.correspondence import _ControlBudget, _json_bound
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, Location, Report

from .preparation import reserve

# These are selected-slot assertion facets, not descriptive LinkML metadata.
_UNSUPPORTED = (
    "any_of",
    "all_of",
    "exactly_one_of",
    "none_of",
    "equals_string",
    "equals_number",
    "equals_expression",
    "equals_string_in",
    "pattern",
    "structured_pattern",
    "minimum_value",
    "maximum_value",
    "range_expression",
    "has_member",
    "all_members",
    "array",
    "values_from",
)


def _offline(*args: object, **kwargs: object) -> Any:
    """Never acquire a model, import, map or terminology through native defaults."""
    raise ResourceError("Biolink interpretation permits only supplied local resources")


def _yaml(data: dict[str, object], budget: Budget) -> dict[str, Any]:
    """Use maintained YAML events/duplicate checks, bounding native construction."""
    yaml = import_module("yaml")
    yaml_loader = import_module("linkml_runtime.loaders").yaml_loader

    text = data.get("content")
    if type(text) is not str:
        raise InputError("Biolink worker requires captured UTF-8 YAML")
    bound_text(text, budget)
    digest = data.get("sha256")
    budget.consume("work", len(text) + 1)
    budget.consume("bytes", 36 * len(text) + 256)
    if sha256(text.encode("utf-8")).hexdigest() != digest:
        raise InputError("Biolink native bytes differ from their pinned identity")
    depth = 0
    try:
        for event in yaml.parse(text):
            budget.consume("work")
            budget.consume("states")
            budget.consume("bytes", 128)
            if isinstance(event, yaml.events.AliasEvent) or getattr(
                event, "anchor", None
            ):
                raise ResourceError(
                    "YAML aliases are outside this finite model profile"
                )
            if isinstance(event, yaml.events.CollectionStartEvent):
                depth += 1
                if depth > budget.limits.max_depth:
                    raise LimitExceeded("Biolink YAML exceeds max_depth")
            elif isinstance(event, yaml.events.CollectionEndEvent):
                depth -= 1
            elif isinstance(event, yaml.events.ScalarEvent):
                bound_text(event.value, budget)
        result = yaml_loader.load_as_dict(text)
    except yaml.YAMLError as error:
        raise InputError("Invalid or duplicate-key Biolink YAML") from error
    if not isinstance(result, dict):
        raise InputError("Biolink YAML must contain a schema/map object")
    return cast(dict[str, Any], result)


def _toolkit(
    request: dict[str, object], budget: Budget
) -> tuple[Any, dict[str, object]]:
    """Construct the actual toolkit with a complete maintained local importmap."""
    try:
        from bmt import Toolkit  # type: ignore[import-untyped]

        SchemaDefinition = import_module(
            "linkml_runtime.linkml_model.meta"
        ).SchemaDefinition
        yaml_loader = import_module("linkml_runtime.loaders").yaml_loader

        installed, linkml = version("bmt"), version("linkml-runtime")
    except (ImportError, PackageNotFoundError) as error:
        raise ResourceError(
            "Install bijux-ontology[biolink] with bmt==1.5.0"
        ) from error
    if installed != "1.5.0" or linkml != "1.12.1":
        raise ResourceError(
            "The qualified profile requires bmt1.5.0/linkml-runtime1.12.1"
        )
    source = object_value(request["model"], location="Biolink model")
    mapping = object_value(request["predicate_map"], location="Biolink map")
    root, pmap = _yaml(source, budget), _yaml(mapping, budget)
    imports = request["imports"]
    if type(imports) is not list:
        raise InputError("Biolink imports require a finite resource inventory")
    documents = {}
    digests = []
    for value in imports:
        item = object_value(value, location="Biolink import")
        name = item.get("name")
        if type(name) is not str or name in documents:
            raise InputError("Biolink imports need distinct exact binding names")
        bound_text(name, budget)
        reserve(budget)
        documents[name] = _yaml(item, budget)
        digests.append([name, item["sha256"]])
    for document in (root, *documents.values()):
        required = document.get("imports", [])
        if not isinstance(required, list) or any(
            not isinstance(n, str) for n in required
        ):
            raise InputError("Biolink schema imports require explicit named tokens")
        for name in required:
            budget.consume("work")
            if name not in documents:
                raise ResourceError(
                    "Missing explicitly supplied Biolink import: " + name
                )
    native = yaml_loader.load(root, target_class=SchemaDefinition)
    toolkit = Toolkit(schema=native, predicate_map=pmap)
    # This is the maintained public importmap, not a replacement schema parser.
    toolkit.view.importmap = documents
    toolkit.view.imports_closure()
    toolkit.view.all_classes()
    toolkit.view.all_slots()
    if (
        toolkit.view.get_class("association") is None
        or toolkit.view.get_class("named thing") is None
        or any(
            toolkit.view.get_slot(name) is None
            for name in ("subject", "predicate", "object", "related to", "qualifier")
        )
    ):
        raise ResourceError("Selected local schema lacks required Biolink model roots")
    actual = toolkit.get_model_version()
    if actual != source["version"]:
        raise InputError("Native model version disagrees with the selected resource")
    identity: dict[str, object] = {
        "model_version": actual,
        "model_sha256": source["sha256"],
        "predicate_sha256": mapping["sha256"],
        "imports": digests,
        "toolkit_version": installed,
        "linkml_version": linkml,
        "python_version": platform.python_version(),
    }
    return toolkit, identity


def _ancestors(toolkit: Any, name: str, budget: Budget) -> list[str]:
    """Retain maintained finite class ancestry; no OWL consequence is asserted."""
    budget.consume("work", len(toolkit.view.all_classes()) + 1)
    values = toolkit.view.class_ancestors(name, mixins=True, reflexive=True)
    reserve(budget, len(values))
    for value in values:
        bound_text(value, budget)
    return cast(list[str], values)


def _slot(slot: Any, budget: Budget) -> dict[str, object]:
    """Keep the effective native facets relevant to this selected application check."""
    reserve(budget)
    result = {
        "name": str(slot.name),
        "range": slot.range,
        "domain": slot.domain,
        "subproperty_of": slot.subproperty_of,
        "required": slot.required,
        "multivalued": slot.multivalued,
        "minimum_cardinality": slot.minimum_cardinality,
        "maximum_cardinality": slot.maximum_cardinality,
    }
    for value in result.values():
        if isinstance(value, str):
            bound_text(value, budget)
    unsupported = [name for name in _UNSUPPORTED if getattr(slot, name, None)]
    reserve(budget, len(unsupported))
    result["unsupported"] = unsupported
    return result


def _rule_slots(value: Any, budget: Budget) -> set[str]:
    """Inspect native rule field references without inventing rule evaluation."""
    budget.consume("work")
    result: set[str] = set()
    if value is None:
        return result
    conditions = getattr(value, "slot_conditions", {})
    for name in conditions:
        bound_text(str(name), budget)
        reserve(budget)
        result.add(str(name))
    for field in ("any_of", "all_of", "none_of", "exactly_one_of"):
        for child in getattr(value, field, ()) or ():
            result.update(_rule_slots(child, budget))
    return result


def _check(
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

    def emit(
        code: str,
        message: str,
        path: tuple[str | int, ...] = (),
        *,
        unknown: bool = False,
    ) -> None:
        """Locate submitted association/qualifier ordinals without rewriting them."""
        # Parent and child reports both retain these actual coordinates. Bound
        # temporary encoding first, then reserve exact escaped wire capacity
        # before the issue becomes paid.
        issue = Issue(
            "biolink." + code,
            message,
            location,
            kind="unverified" if unknown else "finding",
            details=Location(source=location, path=(index, *path)),
        )
        metadata = issue.to_dict()
        maximum = _json_bound(metadata, budget)
        budget.consume("bytes", 4 * maximum)
        budget.consume("work", maximum)
        amount = 512 + 2 * len(json.dumps(metadata, ensure_ascii=True))
        if response_size[0] + amount > output_cap:
            raise LimitExceeded("Biolink diagnostic output capacity exhausted")
        response_size[0] += amount
        issues.append(issue)

    def element(value: str, kind: Any, path: str) -> Any:
        """Use actual native name/alias resolution while preserving original input."""
        budget.consume(
            "work", len(toolkit.view.all_classes()) + len(toolkit.view.all_slots())
        )
        selected = toolkit.get_element(value)
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
    association_ancestors = _ancestors(toolkit, association.name, budget)
    if "association" not in association_ancestors:
        emit(
            "association_role",
            "The selected class is not a native association category",
            ("association",),
        )
        return None
    for selected, field in ((subject, "subject_category"), (obj, "object_category")):
        ancestry = _ancestors(toolkit, selected.name, budget)
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
        ancestors = _ancestors(toolkit, category.name, budget)
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
        slots[name] = _slot(effective, budget)
        belongs(category, effective.range, field)
        if cast(dict[str, object], slots[name])["unsupported"]:
            emit(
                "slot_unverified",
                "Selected endpoint has unsupported native assertion facets",
                (field,),
                unknown=True,
            )
    effective_predicate = toolkit.view.induced_slot("predicate", association.name)
    slots["predicate"] = _slot(effective_predicate, budget)
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
    slots["predicate_endpoints"] = _slot(native_predicate, budget)
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
        slots[str(q.name)] = _slot(effective, budget)
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
                slots["qualified_predicate_endpoints"] = _slot(endpoint, budget)
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
            pre = _rule_slots(rule.preconditions, budget)
            post = _rule_slots(rule.postconditions, budget)
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
    amount = _json_bound(constraints, budget)
    budget.consume("work", amount)
    budget.consume("bytes", amount * 4)
    constraints_json = json.dumps(constraints, ensure_ascii=True, separators=(",", ":"))
    reserve(budget)
    return {
        "association": str(format_element(association)),
        "subject_category": str(format_element(subject)),
        "predicate": str(format_element(predicate)),
        "object_category": str(format_element(obj)),
        "qualified_predicate": str(format_element(qualified)),
        "qualifiers": normalized,
        "constraints_json": constraints_json,
    }


def process(payload: bytes, cap: int) -> bytes:
    """Return paid raw report prefixes even when later native control work stops."""
    budget = None
    issues = None
    records: list[dict[str, object]] = []
    native_identity = None
    checked, requested = 0, 1
    children: list[Report] = []
    try:
        request = object_value(
            decode_json(payload, max_bytes=cap), location="Biolink request"
        )
        budget = _ControlBudget(
            Limits(**cast(dict[str, int], request["limits"])), request["allowances"]
        )
        issues = IssueCollector(budget)
        output_cap = request["output_cap"]
        if type(output_cap) is not int or not 0 < output_cap <= cap:
            raise InputError("Malformed Biolink output capacity")
        # Pay response/report serialization and fixed terminal handoff before work.
        budget.consume("bytes", 8 * output_cap + 1024)
        budget.consume("states", output_cap // 8 + 16)
        budget.consume("work", output_cap + 1)
        toolkit, native_identity = _toolkit(request, budget)
        response_size = [4096 + len(json.dumps(native_identity, ensure_ascii=True))]
        if response_size[0] > output_cap:
            native_identity = None
            raise LimitExceeded("Biolink identity exceeds response capacity")
        if request["mode"] == "prepare":
            checked = 1
        elif request["mode"] == "check":
            associations = request["associations"]
            policy = object_value(request["policy"], location="Biolink policy")
            if type(associations) is not list or not associations:
                raise InputError(
                    "Biolink checks require nonempty selected associations"
                )
            requested = len(associations)
            for index, value in enumerate(associations):
                # Publication space is paid before the native selected contract.
                reserve(budget)
                start = len(issues)
                interpretation = _check(
                    toolkit,
                    object_value(value, location="association"),
                    policy,
                    index,
                    budget,
                    issues,
                    output_cap,
                    response_size,
                )
                encoded_size = _json_bound(interpretation, budget)
                budget.consume("bytes", encoded_size * 2)
                observed = len(json.dumps(interpretation, ensure_ascii=True)) + 1024
                if response_size[0] + observed > output_cap:
                    raise LimitExceeded(
                        "Biolink interpretation output capacity exhausted"
                    )
                response_size[0] += observed
                leaf = Report(
                    "biolink.association",
                    1,
                    tuple(issues[start:]),
                    requested=1,
                    regime="biolink-selected-association",
                )
                children.append(leaf)
                records.append({"effective": interpretation})
                checked += 1
        else:
            raise InputError("Unknown Biolink worker request mode")
    except Exception as error:
        if issues is not None:
            if isinstance(error, LimitExceeded | MemoryError | RecursionError):
                issues.exhausted(str(error) or type(error).__name__)
            else:
                try:
                    issues.append(
                        Issue(
                            "biolink.unverified"
                            if isinstance(error, ResourceError | ImportError)
                            or not isinstance(error, InputError)
                            else "biolink.input_error",
                            str(error)[:128] or type(error).__name__,
                            kind="unverified"
                            if isinstance(error, ResourceError | ImportError)
                            or not isinstance(error, InputError)
                            else "input_error",
                        )
                    )
                except LimitExceeded as limit:
                    issues.exhausted(str(limit))
    report = Report(
        "biolink.native",
        checked,
        tuple(issues or ()),
        requested=max(requested, checked),
        children=tuple(children),
        regime="biolink-selected-association",
    )
    response = {
        "identity": native_identity,
        "interpretations": records,
        "report": report.to_dict(),
        "consumed": budget.consumed() if budget else None,
    }
    raw = json.dumps(response, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    maximum = request.get("output_cap", cap) if "request" in locals() else cap
    return raw if len(raw) <= cast(int, maximum) else b"{}"


def main() -> int:
    """Use the shared owned lifetime; child network access is always disabled."""
    socket.socket.connect = _offline  # type: ignore[method-assign]
    socket.socket.connect_ex = _offline  # type: ignore[method-assign]
    cap = int(sys.argv[1])
    sys.stdout.buffer.write(process(sys.stdin.buffer.read(cap + 1), cap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
