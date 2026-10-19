"""Actual maintained native parsing, writing and structural preservation checks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import Literal, cast

from bijux_ontology._json import object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits

from ..backend.owlapi.bundles import PROFILES, qualify_backend
from ..backend.owlapi.execution import invoke_worker, qualify_capability
from ..backend.owlapi.protocol import result_counter, result_text
from ..backend.types import OwlProfile, OwlProfileBackend
from .receipts import native_profiles, native_records
from .sources.native import native_syntax, stage_native_documents
from .types import (
    ConversionChange,
    NativeExport,
    NativeOntology,
    NativeProfile,
    OwlDocument,
    OwlSyntax,
)


def convert_ontology(
    backend: OwlProfileBackend,
    source: OwlDocument,
    *,
    format: OwlSyntax,
    imports: Mapping[str, OwlDocument] | None = None,
    loss: Literal["reject", "report"] = "reject",
    profiles: Sequence[OwlProfile] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> NativeExport:
    """Convert original native syntax with an actual checked structural roundtrip.

    OWLAPI owns parsing, import discovery, profiles and writing. Only explicit
    captured imports are available; document bases stay their effective URLs.
    Annotated axiom, ontology/version, import and ontology-annotation changes
    are retained. Relative parsed IRIs are unsupported. XML fragment references
    require a hierarchical effective base without a fragment or xml:base; full
    IRIs and absolute prefix namespaces remain supported. Anonymous-individual
    correspondence is unavailable. JVM heap
    and owned deadline bound opaque native work, without instruction/RSS claims.
    """
    syntax = native_syntax(format)
    if loss not in ("reject", "report"):
        raise InputError("Native conversion loss must be reject or report")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    documents: list[NativeOntology] = []
    changes: list[ConversionChange] = []
    capability = None
    loader_profile = None
    before: tuple[NativeProfile, ...] = ()
    after: tuple[NativeProfile, ...] = ()
    content = None
    lossless = None
    complete = False
    children: list[Report] = []
    try:
        if isinstance(profiles, (str, bytes)) or not isinstance(profiles, Sequence):
            raise InputError("Profiles require a finite sequence")
        count = len(profiles)
        if count > len(PROFILES):
            raise InputError("Choose at most four distinct OWL profiles")
        operation.consume("states", count)
        selected: list[OwlProfile] = []
        for index in range(count):
            chosen = cast(object, profiles[index])
            operation.consume("work")
            if type(chosen) is not str or chosen not in PROFILES or chosen in selected:
                raise InputError("Choose distinct supported OWL profiles")
            selected.append(cast(OwlProfile, chosen))
        qualify_backend(backend, operation)
        backend.artifacts.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="conversion-", dir=backend.artifacts) as job:
            directory = Path(job)
            inputs, request = stage_native_documents(
                source, imports, directory, operation
            )
            maximum = min(operation.remaining("issues"), operation.limits.max_states)
            if not maximum:
                raise LimitExceeded("Native conversion has no diagnostic allowance")
            request.update(
                operation="convert",
                format=syntax,
                profiles=selected,
                max_bytes=operation.limits.max_bytes,
                max_issues=maximum,
                max_resources=operation.limits.max_resources,
            )
            data = invoke_worker(backend, request, directory, operation, heap_mb)
            operation.consume("states")
            capability = qualify_capability(data, backend, operation)
            loader_profile = result_text(data.get("native_loader_profile"), operation)
            if loader_profile != "original-native-strict-offline-schema-qualified":
                raise ResourceError("Unsupported native document loader profile")
            children.append(capability.report)
            if data.get("format") != syntax:
                raise ResourceError(
                    "Native writer did not complete its selected format"
                )
            # Adopt completed differences before later metadata/output exhaustion.
            for raw in native_records(
                data.get("changes"), operation, operation.limits.max_issues
            ):
                item = object_value(raw, location="native conversion change")
                kind = cast(str, result_text(item.get("kind"), operation))
                value = cast(str, result_text(item.get("value"), operation))
                operation.consume("states")
                issues.append(
                    Issue(
                        "owl.conversion_loss",
                        "Native conversion changes the maintained ontology structure",
                        expected=kind,
                        actual=value,
                    )
                )
                changes.append(ConversionChange(kind, value))
            if data.get("status") == "exhausted":
                raise LimitExceeded(
                    "Native conversion did not finish its bounded inventory"
                )
            if data.get("qualified") is not True:
                raise ResourceError(
                    "Native input or output interpretation is incomplete"
                )
            seen = set()
            for raw in native_records(data.get("documents"), operation, len(inputs)):
                item = object_value(raw, location="loaded native ontology")
                index = result_counter(item.get("document"), operation, len(inputs) - 1)
                if index in seen:
                    raise ResourceError("Duplicate native loaded document")
                operation.consume("states")
                seen.add(index)
                ontology = result_text(
                    item.get("ontology_iri"), operation, optional=True
                )
                version = result_text(item.get("version_iri"), operation, optional=True)
                declared_imports = tuple(
                    cast(str, result_text(value, operation))
                    for value in native_records(
                        item.get("imports"), operation, operation.limits.max_resources
                    )
                )
                axioms = result_counter(
                    item.get("axioms"), operation, operation.limits.max_states
                )
                documents.append(
                    NativeOntology(
                        inputs[index], ontology, version, declared_imports, axioms
                    )
                )
            if 0 not in seen:
                raise ResourceError("Native worker omitted the selected root")
            before = native_profiles(
                data.get("before_profiles"), tuple(selected), operation
            )
            after = native_profiles(
                data.get("after_profiles"), tuple(selected), operation
            )
            size = result_counter(
                data.get("bytes"), operation, operation.limits.max_bytes
            )
            path = directory / "converted.owl"
            if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
                raise ResourceError("Native writer output is unavailable or changed")
            operation.consume("bytes", size + 1)
            operation.consume("work", size + 1)
            with path.open("rb") as reader:
                raw_content = reader.read(size + 1)
            if len(raw_content) != size:
                raise ResourceError("Native writer output changed during adoption")
            lossless = not changes
            if lossless or loss == "report":
                content = raw_content
            complete = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.conversion")
    except (InputError, OSError):
        try:
            issues.append(
                Issue(
                    "owl.conversion_unverified",
                    "Native parsing, offline closure or structural conversion "
                    "is unavailable",
                    kind="unverified",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location="owl.conversion")
    return NativeExport(
        syntax,
        content,
        lossless,
        tuple(documents),
        tuple(changes),
        before,
        after,
        loader_profile,
        capability,
        Report(
            "owl.conversion",
            int(complete),
            tuple(issues),
            requested=1,
            complete=complete,
            children=tuple(children),
            regime="owlapi-native-structural-roundtrip",
        ),
    )
