"""Maintained BMT and LinkML interpretation inside one owned offline process."""

import json
import platform
import socket
import sys
from hashlib import sha256
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import Any, cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import ControlBudget
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.json.values import decode_json, object_value

from .evaluation import check_association
from .preparation import reserve
from .wire import admit_interpretation, encode_response


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
        budget = ControlBudget(
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
                interpretation = check_association(
                    toolkit,
                    object_value(value, location="association"),
                    policy,
                    index,
                    budget,
                    issues,
                    output_cap,
                    response_size,
                )
                admit_interpretation(interpretation, budget, output_cap, response_size)
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
    return encode_response(
        native_identity,
        records,
        report,
        budget,
        request if "request" in locals() else None,
        cap,
    )


def main() -> int:
    """Use the shared owned lifetime; child network access is always disabled."""
    socket.socket.connect = _offline  # type: ignore[method-assign]
    socket.socket.connect_ex = _offline  # type: ignore[method-assign]
    cap = int(sys.argv[1])
    sys.stdout.buffer.write(process(sys.stdin.buffer.read(cap + 1), cap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
