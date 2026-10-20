"""Real owned workers preserve semantics and never turn execution loss into truth."""

import os
import sys
import time
from pathlib import Path
from threading import Event, Thread
from typing import cast

import pytest

from bijux_ontology.execution.limits import Limits
from bijux_ontology.interfaces.execution.executor import WorkerExecutor
from bijux_ontology.jsonld.checking import check_context
from bijux_ontology.jsonld.codecs import expectations_from_data


def test_supported_operations_agree_and_resources_stay_explicit(tmp_path: Path) -> None:
    document = {"@context": "urn:context", "@id": "urn:record", "name": "Text"}
    resources = {"urn:context": {"@context": {"name": "urn:name"}}}
    expectations = [{"path": ["name"], "predicate": "urn:name"}]
    executor = WorkerExecutor(artifacts=tmp_path)
    result = executor.run(
        "context",
        {
            "document": document,
            "resources": resources,
            "expectations": expectations,
        },
    )
    from bijux_ontology.jsonld.processing.resources import OfflineDocumentLoader

    direct = check_context(
        document,
        expectations_from_data(expectations),
        document_loader=OfflineDocumentLoader(resources),
    )
    assert result.report == direct and direct.verified
    missing = executor.run(
        "context", {"document": document, "expectations": expectations}
    )
    assert missing.report.outcome == "input_error"
    flat = executor.run("flatten", {"document": document, "resources": resources})
    assert flat.report.verified and isinstance(flat.data, dict)
    assert flat.data["document"] == [
        {"@id": "urn:record", "urn:name": [{"@value": "Text"}]}
    ]
    assert executor.run(
        "serialization", {"before": {"urn:p": "x"}, "after": {"urn:p": "x"}}
    ).report.verified
    assert executor.run(
        "vocabulary",
        {
            "source": "<urn:A> a <http://www.w3.org/2002/07/owl#Class>.",
            "format": "turtle",
            "terms": ["urn:A"],
        },
    ).report.verified
    models = [
        {
            "identifier": "record",
            "types": [],
            "fields": [
                {
                    "name": "name",
                    "predicate": "urn:name",
                    "values": [{"kind": "literal"}],
                    "emission": "guaranteed",
                }
            ],
        }
    ]
    assert executor.run(
        "emission",
        {
            "document": document,
            "resources": resources,
            "models": models,
            "usages": [{"model": "record"}],
        },
    ).report.verified
    unsupported = executor.run("arbitrary-function", {})
    assert unsupported.report.outcome == "input_error"
    assert not list(tmp_path.iterdir())


def test_deadline_input_cap_and_live_cancellation_retain_completed_findings(
    tmp_path: Path,
) -> None:
    (tmp_path / "small.ttl").write_text("<urn:A> <urn:p> <urn:o>.")
    (tmp_path / "inventory.json").write_text('["urn:Missing"]')
    large = "\n".join(f"<urn:n{i}> <urn:p> <urn:o>." for i in range(50000))
    (tmp_path / "large.ttl").write_text(large)
    configuration = {
        "version": 1,
        "rdf": {"small": {"path": "small.ttl"}, "large": {"path": "large.ttl"}},
        "checks": [
            {
                "name": "membership",
                "kind": "vocabulary",
                "source": "small",
                "inventory": "inventory.json",
            },
            {
                "name": "expensive",
                "kind": "vocabulary",
                "source": "large",
                "inventory": "inventory.json",
            },
        ],
    }
    root = tmp_path / "runs"
    cancellation = Event()

    def cancel_after_evidence() -> None:
        """Cancel actual ongoing work after the first independent scope completes."""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not cancellation.is_set():
            if any(path.stat().st_size for path in root.glob("*/completed.jsonl")):
                cancellation.wait(0.05)
                cancellation.set()
                return
            cancellation.wait(0.01)

    watcher = Thread(target=cancel_after_evidence)
    watcher.start()
    try:
        result = WorkerExecutor(artifacts=root, timeout=15).run(
            "project",
            {
                "configuration": configuration,
                "directory": str(tmp_path),
            },
            cancellation=cancellation,
        )
    finally:
        cancellation.set()
        watcher.join()
    assert result.report.outcome == "exhausted"
    assert {issue.code for issue in result.report.issues} >= {
        "execution.cancelled",
        "vocabulary.term_missing",
    }
    bounded = WorkerExecutor(artifacts=root, timeout=0.05).run(
        "vocabulary", {"source": large, "format": "turtle", "terms": ["urn:A"]}
    )
    assert "execution.deadline" in {issue.code for issue in bounded.report.issues}
    capped = WorkerExecutor(artifacts=root, limits=Limits(max_total_bytes=1000)).run(
        "vocabulary", {"source": large}
    )
    assert capped.report.outcome == "exhausted"
    assert not list(root.iterdir())


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable fault runtimes")
@pytest.mark.parametrize("fault", ["crash", "malformed", "protocol", "oversized"])
def test_faulty_explicit_runtimes_cannot_return_agreement(
    tmp_path: Path, fault: str
) -> None:
    runtime = tmp_path / "runtime"
    actions = {
        "crash": "raise SystemExit(7)",
        "malformed": "(work / 'result.json').write_text('{')",
        "protocol": "(work / 'result.json').write_text(json.dumps({'protocol': 99}))",
        "oversized": "(work / 'result.json').write_text('x' * 5000)",
    }
    runtime.write_text(
        f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
        f"work=Path(sys.argv[-2]).parent\n{actions[fault]}\n"
    )
    runtime.chmod(0o700)
    result = WorkerExecutor(
        python=runtime, artifacts=tmp_path / "runs", limits=Limits(max_total_bytes=2048)
    ).run("context", {})
    assert not result.report.verified
    assert result.report.outcome == (
        "exhausted" if fault == "oversized" else "input_error"
    )
    assert result.report.issues


def test_missing_runtime_and_explicit_memory_request_are_non_successful(
    tmp_path: Path,
) -> None:
    missing = WorkerExecutor(
        python=tmp_path / "absent", artifacts=tmp_path / "runs"
    ).run("context", {})
    assert missing.report.outcome == "input_error"
    memory = WorkerExecutor(memory_bytes=1, artifacts=tmp_path / "runs").run(
        "context", {}
    )
    assert not memory.report.verified


def test_isolated_entity_bindings_survive_reordering_and_reject_identity_mutations(
    tmp_path: Path,
) -> None:
    models = [
        {
            "identifier": "record",
            "types": ["urn:Record"],
            "fields": [
                {
                    "name": "name",
                    "predicate": "urn:name",
                    "values": [{"kind": "literal"}],
                    "emission": "guaranteed",
                }
            ],
        }
    ]
    usages = [
        {
            "model": "record",
            "reference": "urn:record",
            "graph": None,
            "subject": {"expected_iri": "urn:record", "allow_blank": False},
        }
    ]
    records = [
        {"@id": "urn:other", "@type": "urn:Other"},
        {"@id": "urn:record", "@type": "urn:Record", "urn:name": "Text"},
    ]
    executor = WorkerExecutor(artifacts=tmp_path)
    for _ in range(2):
        result = executor.run(
            "emission",
            {"document": {"@graph": records}, "models": models, "usages": usages},
        )
        assert result.report.verified
        records.reverse()
    selected = executor.run(
        "selection",
        {
            "document": {"@graph": records},
            "selector": {"kind": "entity", "types": ["urn:Record"], "graph": None},
        },
    )
    assert selected.report.verified and isinstance(selected.data, dict)
    assert selected.data["entities"] == [
        {
            "identifier": "urn:record",
            "anonymous": None,
            "graph": None,
            "anonymous_graph": False,
            "graph_identity": None,
        }
    ]
    missing = executor.run(
        "selection",
        {
            "document": {"@graph": records},
            "selector": {
                "kind": "source",
                "steps": [{"kind": "property", "key": "absent", "minimum": 1}],
            },
        },
    )
    assert missing.report.outcome == "disagreement"
    records[1]["@id"] = "urn:sibling"
    absent = executor.run(
        "emission",
        {"document": {"@graph": records}, "models": models, "usages": usages},
    )
    assert absent.report.outcome == "unverified"
    assert absent.report.issues[0].code == "emission.reference_unverified"
    malformed = executor.run(
        "emission",
        {
            "document": {},
            "models": models,
            "usages": [{"model": "record", "reference": "urn:record"}],
        },
    )
    assert malformed.report.outcome == "input_error"


@pytest.mark.parametrize("field", ("index", "list_index"))
def test_isolated_context_preserves_index_requirements(
    tmp_path: Path, field: str
) -> None:
    value: dict[str, object] = {"@index": "chapter"}
    if field == "index":
        value["@value"] = "text"
    else:
        value["@list"] = [{"@id": "urn:member"}]
    executor = WorkerExecutor(artifacts=tmp_path)
    for wanted, agrees in (
        ("chapter", True),
        ("different", False),
        (None, True),
        ({"absent": True}, False),
    ):
        expectations = [
            {"path": ["urn:items"], "predicate": "urn:items", field: wanted}
        ]
        result = executor.run(
            "context",
            {
                "document": {"@id": "urn:Root", "urn:items": value},
                "expectations": expectations,
            },
        )
        assert result.report.verified is agrees
        assert result.report.outcome == ("agreement" if agrees else "disagreement")
        if not agrees:
            assert {issue.code for issue in result.report.issues} == {
                f"context.{field}_mismatch"
            }
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(os.name != "posix", reason="Actual POSIX process ownership")
@pytest.mark.parametrize("interruption", ["cancel", "deadline"])
@pytest.mark.parametrize("native_kind", ["shacl", "query-rules"])
def test_isolated_project_native_jobs_stay_owned_and_honor_timeout(
    tmp_path: Path, interruption: str, native_kind: str
) -> None:
    """Stop actual native search without leaving a nested independent process."""
    import json
    import subprocess

    (tmp_path / "small.ttl").write_text(
        "<urn:A> a <http://www.w3.org/2002/07/owl#Class>."
    )
    (tmp_path / "inventory.json").write_text('["urn:Missing"]')
    (tmp_path / "data.ttl").write_text(
        "\n".join(f"<urn:n{i}> <urn:number> {i} ." for i in range(100))
    )
    (tmp_path / "shapes.ttl").write_text(
        "@prefix sh:<http://www.w3.org/ns/shacl#>. <urn:Shape> a sh:NodeShape; "
        'sh:targetNode <urn:record>; sh:sparql [sh:select """SELECT $this WHERE { '
        "?a <urn:number> ?b . ?c <urn:number> ?d . ?e <urn:number> ?f "
        'FILTER ((?b + ?d + ?f) < 0) }"""].'
    )
    (tmp_path / "rules.json").write_text(
        json.dumps(
            [
                {
                    "name": "costly",
                    "query": "SELECT ?a WHERE { ?a <urn:number> ?b . "
                    "?c <urn:number> ?d . ?e <urn:number> ?f "
                    "FILTER ((?b + ?d + ?f) < 0) }",
                    "scope": {"kind": "default"},
                }
            ]
        )
    )
    configuration = {
        "version": 1,
        "rdf": {name: {"path": name + ".ttl"} for name in ("small", "data", "shapes")},
        "checks": [
            {
                "name": "membership",
                "kind": "vocabulary",
                "source": "small",
                "inventory": "inventory.json",
            },
            {
                "name": "native",
                "kind": native_kind,
                **(
                    {"document": "data", "shapes": "shapes", "trusted_sparql": True}
                    if native_kind == "shacl"
                    else {"source": "data", "rules": "rules.json"}
                ),
                "timeout": 0.3 if interruption == "deadline" else 5.0,
            },
        ],
    }
    runs = tmp_path / "runs"
    stop, cancellation = Event(), Event()
    owned: set[int] = set()
    nested: set[int] = set()

    def observe() -> None:
        """Observe real descendants; cancellation begins after native admission."""
        while not stop.wait(0.01):
            rows = [
                line.strip().split(None, 2)
                for line in subprocess.check_output(
                    ["ps", "-ww", "-axo", "pid=,ppid=,command="], text=True
                ).splitlines()
            ]
            owned.update(
                int(pid)
                for pid, parent, command in rows
                if int(parent) == os.getpid()
                and "bijux_ontology.interfaces.execution.worker" in command
            )
            nested.update(
                int(pid)
                for pid, parent, command in rows
                if int(parent) in owned
                and any(
                    name in command
                    for name in (
                        "bijux_ontology.shacl.execution.worker",
                        "bijux_ontology.rdf.queries.worker",
                    )
                )
            )
            if interruption == "cancel" and any(runs.glob("*/job-deadline")):
                cancellation.set()

    watcher = Thread(target=observe, daemon=True)
    watcher.start()
    started = time.monotonic()
    try:
        result = WorkerExecutor(artifacts=runs, timeout=6).run(
            "project",
            {"configuration": configuration, "directory": str(tmp_path)},
            cancellation=cancellation,
        )
    finally:
        stop.set()
        watcher.join(timeout=2)
    assert time.monotonic() - started < 5
    assert result.report.outcome == "exhausted"
    assert {issue.code for issue in result.report.issues} >= {
        "vocabulary.term_missing",
        "execution.cancelled" if interruption == "cancel" else "execution.deadline",
    }
    assert owned and not nested
    for pid in owned:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    (tmp_path / "inventory.json").write_text(json.dumps(["urn:A"]))
    (tmp_path / "data.ttl").write_text('<urn:record> <urn:p> "value".')
    (tmp_path / "shapes.ttl").write_text(
        "@prefix sh:<http://www.w3.org/ns/shacl#>. <urn:Shape> a sh:NodeShape; "
        "sh:targetNode <urn:record>; sh:property [sh:path <urn:p>; sh:minCount 1]."
    )
    (tmp_path / "rules.json").write_text(
        json.dumps(
            [
                {
                    "name": "exists",
                    "query": "ASK {}",
                    "scope": {"kind": "default"},
                    "expected": True,
                }
            ]
        )
    )
    cast(list[dict[str, object]], configuration["checks"])[1]["timeout"] = 5.0
    recovered = WorkerExecutor(artifacts=runs, timeout=6).run(
        "project", {"configuration": configuration, "directory": str(tmp_path)}
    )
    assert recovered.report.verified
    assert not list(runs.iterdir())
