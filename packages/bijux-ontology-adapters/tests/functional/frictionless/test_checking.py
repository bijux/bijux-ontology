"""Normally installed native package rows, keys and explicit RDF contracts."""

import json
import subprocess
import sys
import threading
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import FrameType
from typing import Literal as EventKind
from typing import cast

import pytest
from rdflib import XSD, URIRef

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    OutputRequirement,
    ValueDeclaration,
)
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import BUDGET_KINDS, Budget, IssueCollector, Limits
from bijux_ontology.execution.processes.transport import exchange_worker
from bijux_ontology.json.receipts import TerminalResponse
from bijux_ontology_adapters.frictionless import (
    ColumnBinding,
    ResourceBinding,
    extract_package,
)
from bijux_ontology_adapters.frictionless.profile import array, object_value
from bijux_ontology_adapters.vocabulary_bindings.controlled.algebra import (
    compile_allowed,
)


def package() -> tuple[
    dict[str, object], dict[str, bytes], tuple[ResourceBinding, ...]
]:
    """Keep real table identities and an independently compiled allowed set."""
    descriptor: dict[str, object] = {
        "profile": "tabular-data-package",
        "name": "measurements",
        "resources": [
            {
                "name": "instruments",
                "profile": "tabular-data-resource",
                "path": "lookup/instruments.csv",
                "format": "csv",
                "schema": {
                    "fields": [
                        {
                            "name": "code",
                            "type": "string",
                            "constraints": {"required": True},
                        },
                        {"name": "label", "type": "string"},
                    ],
                    "primaryKey": "code",
                    "missingValues": ["", "NA"],
                },
            },
            {
                "name": "observations",
                "profile": "tabular-data-resource",
                "path": "data/observations.csv",
                "format": "csv",
                "dialect": {"delimiter": ";"},
                "schema": {
                    "fields": [
                        {"name": "id", "type": "string"},
                        {"name": "instrument", "type": "string"},
                        {"name": "term", "type": "string"},
                        {"name": "value", "type": "number"},
                        {"name": "active", "type": "boolean"},
                        {"name": "count", "type": "integer"},
                    ],
                    "primaryKey": "id",
                    "foreignKeys": [
                        {
                            "fields": "instrument",
                            "reference": {"resource": "instruments", "fields": "code"},
                        }
                    ],
                    "missingValues": ["", "NA"],
                },
            },
        ],
    }
    contents = {
        "lookup/instruments.csv": (
            b'code,label\ninst1,"Scope\nwith text"\ninst2,Other\n'
        ),
        "data/observations.csv": (
            b"id;instrument;term;value;active;count\n"
            b"urn:row:1;inst1;urn:term:allowed;01.250;true;01\n"
            b"urn:row:2;inst2;urn:term:allowed;NA;false;2\n"
        ),
    }
    allowed = compile_allowed(ValueEnumeration((URIRef("urn:term:allowed"),)))
    bindings = (
        ResourceBinding(
            "observations",
            "id",
            (
                ColumnBinding(
                    FieldDeclaration(
                        "term",
                        "urn:property:term",
                        (
                            ValueDeclaration(
                                kind="iri",
                                constraints=ValueConstraints(enumeration=allowed),
                            ),
                        ),
                    ),
                    "iri",
                ),
                ColumnBinding(
                    FieldDeclaration(
                        "value",
                        "urn:property:value",
                        (ValueDeclaration(kind="literal", datatype=str(XSD.decimal)),),
                    )
                ),
                ColumnBinding(
                    FieldDeclaration(
                        "active",
                        "urn:property:active",
                        (ValueDeclaration(kind="literal", datatype=str(XSD.boolean)),),
                    )
                ),
                ColumnBinding(
                    FieldDeclaration(
                        "count",
                        "urn:property:count",
                        (ValueDeclaration(kind="literal", datatype=str(XSD.integer)),),
                    )
                ),
            ),
        ),
    )
    return descriptor, contents, bindings


def test_native_related_package_and_exact_cell_values(tmp_path: Path) -> None:
    """Actual native PK/FK, multiline raw text, typed scalars and null output agree."""
    pytest.importorskip("frictionless")
    descriptor, contents, bindings = package()
    original = deepcopy(descriptor)
    sentinel = tmp_path / "consumer.txt"
    sentinel.write_text("preserve")
    result = extract_package(descriptor, contents, bindings, work_directory=tmp_path)
    assert result.report.verified, result.report.to_dict()
    assert descriptor == original and result.source is not None
    assert result.producer is not None and result.producer.framework == "5.21.0"
    value = next(
        cell
        for cell in result.cells
        if cell.resource == "observations" and cell.row == 2 and cell.name == "value"
    )
    assert value.text == "01.250" and value.value == Decimal("1.250")
    assert str(value.statements[0][2]) == "1.250"
    missing = next(
        cell
        for cell in result.cells
        if cell.resource == "observations" and cell.row == 3 and cell.name == "value"
    )
    assert missing.text == "NA" and missing.value is None and missing.statements == ()
    active = next(
        cell for cell in result.cells if cell.row == 2 and cell.name == "active"
    )
    assert type(active.value) is bool
    assert (
        next(
            cell for cell in result.cells if cell.row == 2 and cell.name == "count"
        ).value
        == 1
    )
    assert (
        next(cell for cell in result.cells if cell.name == "label").text
        == "Scope\nwith text"
    )
    assert sorted(path.name for path in tmp_path.iterdir()) == ["consumer.txt"]
    assert sentinel.read_text() == "preserve"


def test_semantic_failure_is_at_actual_native_cell(tmp_path: Path) -> None:
    """A native-valid undeclared IRI fails its actual resource/record/column binding."""
    pytest.importorskip("frictionless")
    descriptor, contents, bindings = package()
    contents["data/observations.csv"] = contents["data/observations.csv"].replace(
        b"urn:term:allowed", b"urn:term:undeclared", 1
    )
    result = extract_package(descriptor, contents, bindings, work_directory=tmp_path)
    assert (
        result.native_report.verified
        and not result.report.verified
        and result.source is not None
    )
    finding = next(
        issue
        for issue in result.report.issues
        if issue.code == "emission.value_mismatch"
    )
    assert (
        finding.details is not None
        and finding.details.source == "data/observations.csv"
        and finding.details.path == (2, "term")
    )


def test_actual_native_key_and_type_failures_never_certify(tmp_path: Path) -> None:
    """Reject real target, primary-key and native scalar failures."""
    pytest.importorskip("frictionless")
    descriptor, contents, bindings = package()
    variants = [
        (
            {
                **contents,
                "data/observations.csv": contents["data/observations.csv"].replace(
                    b"inst1", b"absent"
                ),
            },
            "foreign_key",
            (2, "instrument"),
        ),
        (
            {**contents, "lookup/instruments.csv": b"code,label\n"},
            "foreign_key",
            (2, "instrument"),
        ),
        (
            {
                **contents,
                "lookup/instruments.csv": b"code,label\ninst1,A\ninst1,B\ninst2,C\n",
            },
            "primary_key",
            (3,),
        ),
        (
            {
                **contents,
                "data/observations.csv": contents["data/observations.csv"].replace(
                    b"01.250", b"not-a-number"
                ),
            },
            "type_error",
            (2, "value"),
        ),
    ]
    for resources, code, location in variants:
        result = extract_package(
            descriptor, resources, bindings, work_directory=tmp_path
        )
        assert not result.report.verified and result.source is None
        matching = [
            issue
            for issue in result.report.issues
            if issue.code == "frictionless.native_" + code
        ]
        assert matching and matching[0].details is not None
        assert matching[0].details.path == location
    limited = extract_package(
        descriptor,
        variants[0][0],
        bindings,
        work_directory=tmp_path,
        limits=Limits(max_issues=1),
    )
    assert not limited.native_report.complete and not limited.report.verified
    assert any(
        issue.code == "frictionless.native_foreign_key"
        for issue in limited.report.issues
    )
    assert any(issue.kind == "exhausted" for issue in limited.report.issues)
    assert not list(tmp_path.iterdir())


def test_unsupported_and_ambiguous_required_package_scope(tmp_path: Path) -> None:
    """Reject ignored policies, unsupported keys and unsafe resource scope."""
    descriptor, contents, bindings = package()
    variants = []
    unknown = deepcopy(descriptor)
    unknown["unknownSemanticPolicy"] = {"required": True}
    variants.append(unknown)
    composite = deepcopy(descriptor)
    first = object_value(array(composite["resources"])[0])
    object_value(first["schema"])["primaryKey"] = ["code", "label"]
    variants.append(composite)
    remote = deepcopy(descriptor)
    object_value(array(remote["resources"])[0])["path"] = (
        "https://example.invalid/table.csv"
    )
    variants.append(remote)
    unsupported = deepcopy(descriptor)
    object_value(
        array(
            object_value(object_value(array(unsupported["resources"])[0])["schema"])[
                "fields"
            ]
        )[0]
    )["type"] = "date"
    variants.append(unsupported)
    for candidate in variants:
        result = extract_package(candidate, contents, bindings, work_directory=tmp_path)
        assert result.source is None and result.report.outcome == "unverified"
    duplicate = deepcopy(descriptor)
    object_value(array(duplicate["resources"])[1])["name"] = "instruments"
    result = extract_package(duplicate, contents, bindings, work_directory=tmp_path)
    assert result.report.outcome == "input_error" and result.source is None
    missing = extract_package(
        descriptor,
        {"data/observations.csv": contents["data/observations.csv"]},
        bindings,
        work_directory=tmp_path,
    )
    assert missing.report.outcome == "unverified" and missing.source is None
    assert not list(tmp_path.iterdir())


def test_row_local_check_does_not_hide_missing_output(tmp_path: Path) -> None:
    """The same RDF subject in another table cannot satisfy this row's missing cell."""
    pytest.importorskip("frictionless")
    descriptor, contents, _ = package()
    binding = (
        ResourceBinding(
            "observations",
            "id",
            (
                ColumnBinding(
                    FieldDeclaration(
                        "value",
                        "urn:property:value",
                        (ValueDeclaration(kind="literal", datatype=str(XSD.decimal)),),
                        output=OutputRequirement(allow_null=False),
                    )
                ),
            ),
        ),
    )
    result = extract_package(descriptor, contents, binding, work_directory=tmp_path)
    assert not result.report.verified and any(
        issue.code == "emission.null_forbidden"
        and issue.details is not None
        and issue.details.path == (3, "value")
        for issue in result.report.issues
    )


def test_limits_and_predispatch_cancellation(tmp_path: Path) -> None:
    """No staged child survives a failed operation or pre-dispatch cancellation."""
    import threading

    descriptor, contents, bindings = package()
    current = Budget(Limits(max_total_bytes=1000))
    result = extract_package(
        descriptor, contents, bindings, work_directory=tmp_path, budget=current
    )
    assert result.report.outcome == "exhausted" and result.source is None
    cancel = threading.Event()
    cancel.set()
    result = extract_package(
        descriptor, contents, bindings, work_directory=tmp_path, cancel=cancel
    )
    assert not result.report.verified and result.source is None
    assert not list(tmp_path.iterdir())


def test_bad_public_arguments_are_input_errors(tmp_path: Path) -> None:
    """Wrong caller types do not become framework absence or successful empty data."""
    from bijux_ontology.diagnostics.errors import InputError

    descriptor, contents, bindings = package()
    with pytest.raises(InputError):
        extract_package(
            descriptor, contents, bindings, work_directory=cast(Path, "relative")
        )
    result = extract_package(
        descriptor,
        {"lookup/instruments.csv": cast(bytes, "not bytes")},
        bindings,
        work_directory=tmp_path,
    )
    assert result.report.outcome == "input_error" and result.source is None


@pytest.mark.parametrize("mode", ("completed", "missing", "ordinary", "deadline"))
def test_owned_parent_controls_native_lifetime_and_terminal_receipts(
    mode: str,
    tmp_path: Path,
) -> None:
    """Observe real native receipts, inherited events and reaped process lifetimes."""
    pytest.importorskip("frictionless")
    descriptor, contents, selected = package()
    cancellation = threading.Event()
    ledger = Budget()
    parent = (
        ledger
        if mode == "ordinary"
        else Operation(None, ledger, 0.1 if mode == "deadline" else 30, cancellation)
    )
    local = cancellation if mode == "ordinary" else None
    processes: list[subprocess.Popen[bytes]] = []
    native: dict[str, int] = {}
    grants: dict[str, int] = {}
    host: dict[str, int] = {}
    before: dict[str, int] = {}
    timeouts: list[float] = []
    previous = sys.getprofile()

    def native_command(command: object) -> bool:
        """Recognize only this adapter's actual native command."""
        return isinstance(command, (list, tuple)) and any(
            str(part) == "bijux_ontology_adapters.frictionless.worker"
            for part in command
        )

    def observe(
        frame: FrameType,
        event: EventKind["call", "return", "c_call", "c_return", "c_exception"],
        value: object,
    ) -> None:
        """Interrupt actual lifecycle boundaries without replacing producer output."""
        if previous is not None:
            previous(frame, event, value)
        actual = frame.f_code is exchange_worker.__code__ and native_command(
            frame.f_locals.get("command")
        )
        if actual and event == "call":
            grants.update(json.loads(frame.f_locals["payload"])["allowances"])
            before.update((kind, ledger.remaining(kind)) for kind in BUDGET_KINDS)
            timeouts.append(frame.f_locals["timeout"])
            assert frame.f_locals["cancel"] is cancellation
        if event != "return":
            return
        process = frame.f_locals.get("self")
        if (
            frame.f_code is subprocess.Popen.__init__.__code__
            and isinstance(process, subprocess.Popen)
            and native_command(process.args)
        ):
            processes.append(process)
            if mode == "missing":
                cancellation.set()
        if actual and isinstance(value, bytes):
            native.update(json.loads(value)["controls"])
            if mode in ("completed", "ordinary"):
                cancellation.set()
        if frame.f_code is TerminalResponse.close.__code__:
            host.update(frame.f_locals["self"]._host.consumed())

    sys.setprofile(observe)
    try:
        result = extract_package(
            descriptor,
            contents,
            selected,
            work_directory=tmp_path,
            budget=parent,
            cancel=local,
        )
    finally:
        sys.setprofile(previous)
    assert all(process.returncode is not None for process in processes)
    if mode == "deadline":
        # A fast native completion is valid; a child never gets a fresh ten seconds.
        assert result.report.outcome in ("agreement", "exhausted")
        assert all(0 < timeout <= 0.1 for timeout in timeouts)
        if not timeouts:
            assert result.report.outcome == "exhausted"
    else:
        assert result.report.outcome == "exhausted" and result.source is None
        assert processes and bool(native) == (mode != "missing")
        paid = native if native else grants
        assert {
            kind: before[kind] - ledger.remaining(kind) for kind in BUDGET_KINDS
        } == {kind: paid[kind] + host[kind] for kind in BUDGET_KINDS}
    assert extract_package(
        descriptor, contents, selected, work_directory=tmp_path
    ).report.verified


def test_paid_native_failure_keeps_original_cell_after_inherited_cancellation(
    tmp_path: Path,
) -> None:
    """Retain a real paid native finding when its next host adoption is cancelled."""
    pytest.importorskip("frictionless")
    descriptor, contents, selected = package()
    contents["data/observations.csv"] = contents["data/observations.csv"].replace(
        b"01.250", b"not-a-number", 1
    )
    cancellation = threading.Event()
    operation = Operation(None, Budget(), 30, cancellation)
    paid: list[Issue] = []
    previous = sys.getprofile()

    def observe(
        frame: FrameType,
        event: EventKind["call", "return", "c_call", "c_return", "c_exception"],
        value: object,
    ) -> None:
        """Cancel after the actual native error has paid its host retention cost."""
        if previous is not None:
            previous(frame, event, value)
        if event == "return" and frame.f_code is IssueCollector.append.__code__:
            issue = frame.f_locals.get("issue")
            if (
                isinstance(issue, Issue)
                and issue.code == "frictionless.native_type_error"
            ):
                paid.append(issue)
                cancellation.set()

    sys.setprofile(observe)
    try:
        result = extract_package(
            descriptor, contents, selected, work_directory=tmp_path, budget=operation
        )
    finally:
        sys.setprofile(previous)
    assert result.report.outcome == "exhausted" and result.source is None
    assert paid and any(issue is paid[0] for issue in result.report.issues)
    assert paid[0].details is not None
    assert paid[0].details.source == "data/observations.csv"
    assert paid[0].details.path == (2, "value")
