"""Hash exact supplied inputs and installed code without RDF syntax preparation."""

import hashlib
import importlib.metadata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import cast

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget, Limits, bound_text
from bijux_ontology.json.values import own_json
from bijux_ontology.jsonld.conversion import DirectionPolicy
from bijux_ontology.rdf.literals import Regime
from bijux_ontology.rdf.sources.types import RdfSource

from .codecs import declaration_data, json_bytes
from .types import PreparationIdentity


@dataclass(frozen=True)
class _Inputs:
    """One owned explicit input capture shared by identity and actual compilation."""

    declarations: list[object]
    source: RdfSource
    imports: dict[str, RdfSource]
    lookup: tuple[RdfSource, ...]
    options: dict[str, object]
    identity: PreparationIdentity


def operation(limits: Limits | None, budget: Budget | None) -> Budget:
    """Validate one shared allowance before inspecting caller inputs."""
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Preparation requires Limits")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Preparation requires Budget")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Preparation limits conflict with supplied budget")
    return budget if budget is not None else Budget(limits)


def engine_identity(budget: Budget) -> str:
    """Fingerprint local installed implementation bytes, never payload imports."""
    root = Path(__file__).resolve().parents[2]
    paths: list[Path] = []
    for path in root.rglob("*"):
        budget.consume("work")
        if path.is_file() and path.suffix in (".py", ".json", ".ttl", ".rq"):
            budget.consume("states")
            budget.consume("bytes", 128)
            paths.append(path)
    budget.consume("work", len(paths) * max(1, len(paths).bit_length()))
    digest = hashlib.sha256(b"bijux-ontology.model-preparation:1\n")
    for path in sorted(paths):
        name = path.relative_to(root).as_posix()
        bound_text(name, budget)
        size = path.stat().st_size
        if size > budget.limits.max_bytes:
            raise LimitExceeded("Semantic implementation exceeds max_bytes")
        budget.consume("bytes", size)
        budget.consume("work", 1 + size // 4096)
        digest.update(name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    for package in ("rdflib", "pyoxigraph", "PyLD", "google-re2"):
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            version = "absent"
        bound_text(version, budget)
        digest.update(package.encode("ascii") + b"\x00" + version.encode("utf-8"))
    return digest.hexdigest()


def _source(source: RdfSource, budget: Budget) -> tuple[RdfSource, dict[str, object]]:
    """Bind content integrity to exactly the immutable descriptor actually loaded."""
    if type(source) is not RdfSource:
        raise InputError("Preparation requires an ordinary immutable RdfSource")
    descriptor = {
        field.name: getattr(source, field.name) for field in fields(RdfSource)
    }
    if type(descriptor["content"]) not in (str, bytes):
        raise InputError("Preparation artifacts require str/bytes source content")
    data: dict[str, object] = {}
    for name, value in descriptor.items():
        if name == "content":
            if isinstance(value, str):
                bound_text(value, budget)
                payload = value.encode("utf-8")
            else:
                if len(value) > budget.limits.max_bytes:
                    raise LimitExceeded("RDF source exceeds max_bytes")
                budget.consume("bytes", len(value))
                budget.consume("work", 1 + len(value) // 4096)
                payload = value
            data["content_kind"] = "text" if isinstance(value, str) else "bytes"
            data["content_sha256"] = hashlib.sha256(payload).hexdigest()
        else:
            if value is not None and type(value) not in (str, bool):
                raise InputError(
                    "Preparation source metadata must be plain text/boolean"
                )
            if isinstance(value, str):
                bound_text(value, budget)
            data[name] = value
    return RdfSource(**descriptor), data


def capture_inputs(
    models: Sequence[ModelDeclaration],
    source: RdfSource,
    *,
    imports: Mapping[str, RdfSource] | None,
    lookup: Sequence[RdfSource],
    context: object | Absent,
    resources: Mapping[str, object] | None,
    base: str | None,
    regime: Regime,
    rdf_direction: DirectionPolicy | None,
    budget: Budget,
) -> _Inputs:
    """Own all mutable containers once; compile uses this exact identity capture."""
    if isinstance(cast(object, models), (str, bytes)) or not isinstance(
        models, Sequence
    ):
        raise InputError("Preparation requires a finite declaration sequence")
    budget.consume("states", len(models))
    if any(not isinstance(model, ModelDeclaration) for model in models):
        raise InputError("Preparation requires ModelDeclaration values")
    declarations = [declaration_data(model, budget) for model in models]
    local, local_data = _source(source, budget)
    if imports is not None and not isinstance(imports, Mapping):
        raise InputError("Preparation imports require an explicit mapping")
    supplied_imports = imports if imports is not None else {}
    budget.consume("resources", len(supplied_imports))
    imported: dict[str, RdfSource] = {}
    imported_data: list[object] = []
    for name, item in supplied_imports.items():
        if type(name) is not str:
            raise InputError("Preparation import keys require IRI text")
        bound_text(name, budget)
        owned, descriptor = _source(item, budget)
        imported[name] = owned
        imported_data.append([name, descriptor])
    if isinstance(cast(object, lookup), (str, bytes)) or not isinstance(
        lookup, Sequence
    ):
        raise InputError("Preparation lookup requires a finite source sequence")
    budget.consume("resources", len(lookup) + 1)
    lookups: list[RdfSource] = []
    lookup_data: list[object] = []
    for item in lookup:
        owned, descriptor = _source(item, budget)
        lookups.append(owned)
        lookup_data.append(descriptor)
    if base is not None and type(base) is not str:
        raise InputError("Preparation base requires IRI text")
    if regime not in ("application", "rdf", "owl-dl") or not isinstance(regime, str):
        raise InputError("Preparation requires supported literal regime")
    if rdf_direction not in (None, "i18n-datatype", "compound-literal"):
        raise InputError("Unsupported preparation RDF direction")
    if resources is not None and not isinstance(resources, Mapping):
        raise InputError("Preparation contexts require a closed JSON mapping")
    context_data = {"absent": True} if context is ABSENT else {"value": context}
    budget.consume("states", len(resources) if resources is not None else 0)
    budget.consume("bytes", 64 * (len(resources) if resources is not None else 0))
    options: dict[str, object] = {
        "context": context_data,
        "resources": dict(resources) if resources is not None else {},
        "base": base,
        "regime": regime,
        "rdf_direction": rdf_direction,
    }
    json_bytes(options, budget)
    owned_options = own_json(
        options, max_bytes=budget.limits.max_bytes, max_depth=budget.limits.max_depth
    )
    assert isinstance(owned_options, dict)
    options = owned_options
    identity_data = {
        "declarations": declarations,
        "source": local_data,
        "imports": imported_data,
        "lookup": lookup_data,
        "options": options,
        "literal_preservation": True,
    }
    inputs_digest = hashlib.sha256(json_bytes(identity_data, budget)).hexdigest()
    return _Inputs(
        declarations,
        local,
        imported,
        tuple(lookups),
        options,
        PreparationIdentity(inputs_digest, engine_identity(budget)),
    )


def model_preparation_identity(
    models: Sequence[ModelDeclaration],
    source: RdfSource,
    *,
    imports: Mapping[str, RdfSource] | None = None,
    lookup: Sequence[RdfSource] = (),
    context: object | Absent = ABSENT,
    resources: Mapping[str, object] | None = None,
    base: str | None = None,
    regime: Regime = "application",
    rdf_direction: DirectionPolicy | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> PreparationIdentity:
    """Fingerprint the exact current compilation inputs without parsing RDF.

    Every supplied import entry invalidates conservatively, including unused ones.
    Only closed ordinary str/bytes resources and data-only declarations are supported.
    """
    return capture_inputs(
        models,
        source,
        imports=imports,
        lookup=lookup,
        context=context,
        resources=resources,
        base=base,
        regime=regime,
        rdf_direction=rdf_direction,
        budget=operation(limits, budget),
    ).identity
