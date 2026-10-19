"""Actual RDF corpus restart, negative reuse and fresh cross-record obligations."""

import json
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from rdflib import URIRef

from bijux_ontology.declarations.keys import KeyComponent, KeyContract
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.workflows.corpus import CorpusRecord, ResumeCorpus
from bijux_ontology.workflows.revisions import RevisionManager, prepare_revision

E = "https://example.org/corpus/"
X = "http://www.w3.org/2001/XMLSchema#string"


def manager(*, changed: bool = False, limits: Limits | None = None) -> RevisionManager:
    """Prepare an actual named model contract, never a caller-labelled validator."""
    name = "other" if changed else "label"
    result = prepare_revision(
        (
            ModelDeclaration(
                "record",
                (E + "Record",),
                (
                    FieldDeclaration(
                        name,
                        E + name,
                        (ValueDeclaration("literal", datatype=X),),
                        emission="guaranteed",
                    ),
                ),
            ),
        ),
        RdfSource(
            "@prefix ex: <" + E + "> . "
            "@prefix owl: <http://www.w3.org/2002/07/owl#> . "
            "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> . "
            "ex:Record a owl:Class . ex:" + name + " a owl:DatatypeProperty ; "
            "rdfs:domain ex:Record ; rdfs:range <" + X + "> .",
            format="turtle",
        ),
        limits=limits,
    )
    assert result.report.verified and result.prepared, result.report
    owned = RevisionManager(limits=limits)
    owned.activate(result.prepared)
    return owned


def record(name: str, *, value: str | None = "valid") -> CorpusRecord:
    """Give each stable manifest identity its independently serialized RDF input."""
    subject = E + name
    text = "<" + subject + "> a <" + E + "Record> . "
    if value is not None:
        text += "<" + subject + "> <" + E + 'label> "' + value + '" .'
    return CorpusRecord(
        name,
        RdfSource(text, format="turtle"),
        (RdfModelUsage("record", URIRef(subject)),),
        key_subject=subject,
    )


def test_qualified_reuse_invalidates_changed_records_binding_and_revision(
    tmp_path: Path,
) -> None:
    """Only an identical closed record and actual semantic plan can reuse truth."""
    store = ResumeCorpus(tmp_path / "corpus.sqlite")
    first = manager()
    with first.lease() as lease:
        initial = store.run(lease, (record("one"), record("two")))
        assert initial.report.verified and not any(x.reused for x in initial.records)
        repeated = store.run(lease, (record("two"), record("one")))
        assert repeated.report.verified and all(x.reused for x in repeated.records)
        changed = store.run(lease, (record("one", value="changed"), record("two")))
        assert changed.report.verified
        assert [x.reused for x in changed.records] == [False, True]
        rebound = replace(
            record("two"), usages=(RdfModelUsage("record", URIRef(E + "absent")),)
        )
        binding = store.run(lease, (rebound,))
        assert not binding.records[0].reused and not binding.report.verified
        assert binding.pending == ("two",)
        retried = store.run(lease, (rebound,))
        assert not retried.records[0].reused and retried.pending == ("two",)
    with pytest.raises(InputError):
        store.run(lease, (record("one"),))
    first.close()
    fresh = manager()
    with fresh.lease() as lease:
        resumed = store.run(lease, (record("one", value="changed"),))
        assert resumed.report.verified and resumed.records[0].reused
    fresh.close()
    changed_plan = manager(changed=True)
    with changed_plan.lease() as lease:
        rerun = store.run(lease, (record("one", value="changed"),))
        assert (
            not rerun.records[0].reused
            and rerun.semantic_report.outcome == "disagreement"
        )
    changed_plan.close()


def test_complete_negative_remains_negative_unknown_stays_pending(
    tmp_path: Path,
) -> None:
    """Durable completion records a negative verdict without manufacturing green."""
    owned = manager()
    with owned.lease() as lease:
        store = ResumeCorpus(tmp_path / "negative.sqlite")
        bad = record("bad", value=None)
        initial = store.run(lease, (bad,))
        repeated = store.run(lease, (bad,))
        assert initial.semantic_report.complete and not initial.pending
        assert repeated.records[0].reused and repeated.records[0].durable
        assert repeated.semantic_report.outcome == "disagreement"
        assert (
            repeated.records[0].report.to_dict() == initial.records[0].report.to_dict()
        )
        unsupported = replace(
            record("pending"), source=RdfSource("broken", format="turtle")
        )
        uncertain = store.run(lease, (unsupported,))
        assert uncertain.pending == ("pending",) and not uncertain.report.verified
        again = store.run(lease, (unsupported,))
        assert not again.records[0].reused and again.pending == ("pending",)
    owned.close()


def test_global_keys_rebuild_even_with_every_record_cached(tmp_path: Path) -> None:
    """Independent cached positives cannot prove a corpus-wide uniqueness claim."""
    owned = manager()
    with owned.lease() as lease:
        store = ResumeCorpus(tmp_path / "keys.sqlite")
        records = (record("one"), record("two"))
        assert store.run(lease, records).report.verified
        result = store.run(
            lease,
            records,
            keys=(
                KeyContract(
                    "labels",
                    (KeyComponent(URIRef(E + "label")),),
                ),
            ),
        )
        assert all(item.reused for item in result.records)
        assert result.global_report and result.global_report.outcome == "disagreement"
        assert result.report.outcome == "disagreement"
        assert any(issue.kind == "finding" for issue in result.global_report.issues)
    owned.close()


def test_corrupt_checkpoint_is_not_truth_and_durability_is_separate(
    tmp_path: Path,
) -> None:
    """A fresh recheck cannot conceal required checkpoint uncertainty."""
    path = tmp_path / "corrupt.sqlite"
    owned = manager()
    with owned.lease() as lease:
        store = ResumeCorpus(path)
        assert store.run(lease, (record("one"),)).report.verified
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE completed SET report=?", (b'{"partial":',))
        rerun = store.run(lease, (record("one"),))
        assert rerun.semantic_report.verified and not rerun.records[0].reused
        assert not rerun.checkpoint_report.verified and not rerun.report.verified
        assert not rerun.records[0].durable
        assert any(
            x.code == "corpus.checkpoint_unverified"
            for x in rerun.checkpoint_report.issues
        )
        limited = ResumeCorpus(tmp_path / "limited.sqlite", max_records=1)
        full = limited.run(lease, (record("one"), record("two")))
        assert full.semantic_report.verified and not full.checkpoint_report.verified
        assert [x.durable for x in full.records] == [True, False]
        foreign_path = tmp_path / "foreign.sqlite"
        foreign = ResumeCorpus(foreign_path)
        assert foreign.run(lease, (record("one"),)).report.verified
        with sqlite3.connect(foreign_path) as connection:
            connection.execute(
                "CREATE TRIGGER foreign_insert AFTER INSERT ON completed "
                "BEGIN DELETE FROM completed WHERE identifier=NEW.identifier; END"
            )
        rejected = foreign.run(lease, (record("two"),))
        assert rejected.semantic_report.verified and not rejected.report.verified
        assert not rejected.records[0].durable
        for suffix in ("-wal", "-shm"):
            path = tmp_path / ("sidecar" + suffix + ".sqlite")
            store = ResumeCorpus(path)
            assert store.run(lease, (record("one"),)).report.verified
            path.with_name(path.name + suffix).write_bytes(b"foreign")
            rejected = store.run(lease, (record("one"),))
            assert rejected.semantic_report.verified and not rejected.report.verified
            assert not rejected.records[0].reused and not rejected.records[0].durable
    owned.close()


def test_bounded_manifest_and_real_shared_remaining_allowance(tmp_path: Path) -> None:
    """An exhausted record is pending; duplicate IDs never qualify an offset cache."""
    owned = manager()
    with owned.lease() as lease:
        store = ResumeCorpus(tmp_path / "bounded.sqlite")
        duplicated = store.run(lease, (record("same"), record("same")))
        assert not duplicated.report.verified and not duplicated.records
        operation = Budget()
        operation.consume("work", operation.limits.max_work - 1)
        result = store.run(lease, (record("one"),), budget=operation)
        assert not result.report.verified and not result.records
        assert result.report.outcome == "exhausted"
        assert operation._counts["work"] <= operation.limits.max_work
    owned.close()


def test_real_process_interruption_then_restart_matches_uninterrupted(
    tmp_path: Path,
) -> None:
    """Kill a real corpus process after committed record boundaries."""
    script = tmp_path / "validate.py"
    source = Path(__file__).read_text()
    helpers = source[source.index('E = "') : source.index("def test_qualified")]
    imports = source[: source.index('E = "')]
    script.write_text(
        imports
        + helpers
        + """
limits=Limits(max_resources=1024,max_work=10000000,max_states=5000000,max_total_bytes=1000000000)
owned=manager(limits=limits)
with owned.lease() as lease:
 records=tuple(record(str(i),value=str(i)) for i in range(120))
 result=ResumeCorpus(Path(sys.argv[1]),max_records=512).run(
  lease,records,limits=limits)
 print(json.dumps({"verified":result.report.verified,
  "semantic":result.semantic_report.to_dict(),
  "reused":sum(x.reused for x in result.records),
  "count":len(result.records)}),flush=True)
owned.close()
"""
    )
    command = (
        [sys.executable, "-I", "-B"] if sys.flags.isolated else [sys.executable, "-B"]
    )
    path = tmp_path / "interrupted.sqlite"
    log = tmp_path / "interrupted.log"
    with log.open("wb") as output:
        process = subprocess.Popen(
            [*command, str(script), str(path)],
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 30
        committed = 0
        while time.monotonic() < deadline and process.poll() is None:
            if path.exists():
                try:
                    with sqlite3.connect(path, timeout=0.01) as connection:
                        committed = connection.execute(
                            "SELECT count(*) FROM completed"
                        ).fetchone()[0]
                except sqlite3.Error:
                    pass
                if committed >= 3:
                    process.kill()
                    break
            time.sleep(0.005)
        process.wait(timeout=10)
    assert committed >= 3 and process.returncode != 0, log.read_text()
    resumed = subprocess.run(
        [*command, str(script), str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    plain = subprocess.run(
        [*command, str(script), str(tmp_path / "plain.sqlite")],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    a, b = json.loads(resumed.stdout), json.loads(plain.stdout)
    assert a["verified"] and b["verified"] and a["count"] == b["count"] == 120
    assert a["reused"] >= committed and b["reused"] == 0
    assert a["semantic"] == b["semantic"]
