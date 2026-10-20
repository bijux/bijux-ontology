"""Pinned external metadata stays distinct from application rules and RDF terms."""

import hashlib
import json
import socket
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from zipfile import ZipFile

import pytest
from rdflib import BNode, Graph, Literal, URIRef

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Location
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.interfaces.adapters.bioregistry import (
    BioregistryOverride,
    BioregistrySnapshot,
    read_bioregistry,
    read_bioregistry_bundle,
)
from bijux_ontology.vocabulary.accessions import (
    AccessionRule,
    AccessionValue,
    check_accessions,
)
from bijux_ontology.vocabulary.checking import check_vocabulary

# Complete original record from biopragmatics/bioregistry commit
# b245faf8c85be0ccd4a3ea482be394f3c0d4cfaf, src/bioregistry/data/bioregistry.json.
ACTUAL_RECORD: dict[str, object] = {
    "absd": {
        "contact": {
            "email": "nicolas.maillet@pasteur.fr",
            "name": "Nicolas Maillet",
            "orcid": "0000-0003-1611-5243",
        },
        "contributor": {
            "email": "palayoor.n@northeastern.edu",
            "github": "nalikapalayoor",
            "name": "Nalika Palayoor",
            "orcid": "0009-0008-8406-631X",
        },
        "description": "The AntiBody Sequence Database is a public dataset for "
        "antibody sequence data. It provides unique identifiers "
        "for antibody sequences, including both immunoglobulin "
        "and single-chain variable fragment sequences. These are "
        "are critical for immunological studies, and  allows "
        "users to search and retrieve antibody sequences based on "
        "sequence similarity and specificity, and other "
        "biological properties.",
        "example": "81d0ea69a9ab134f6ba68314e982171a367d3ab739c85c5ba49701dd02170ed3",
        "github_request_issue": 1362,
        "homepage": "https://absd.pasteur.cloud",
        "name": "AntiBody Sequence Database",
        "pattern": "^[a-f0-9]{64}$",
        "publications": [
            {
                "doi": "10.1093/nargab/lqae171",
                "pmc": "PMC11655285",
                "pubmed": "39703430",
                "title": "AntiBody Sequence Database",
                "year": 2024,
            }
        ],
        "reviewer": {
            "email": "b.gyori@northeastern.edu",
            "github": "bgyori",
            "name": "Benjamin M. Gyori",
            "orcid": "0000-0001-9439-5346",
        },
        "uri_format": "https://absd.pasteur.cloud/antibody/$1",
    }
}
REVISION = "b245faf8c85be0ccd4a3ea482be394f3c0d4cfaf:absd-complete-record"
NS = "https://absd.pasteur.cloud/antibody/"
RULE = AccessionRule(NS, "[a-f0-9]{64}")


def payload(records: dict[str, object]) -> bytes:
    """Serialize the explicitly supplied subset, with its own distinct byte pin."""
    return json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode()


def read(records: dict[str, object], **options: Any) -> BioregistrySnapshot:
    """Supply the exact subset digest rather than claiming the whole-source hash."""
    raw = payload(records)
    return read_bioregistry(
        raw,
        revision=REVISION,
        sha256=hashlib.sha256(raw).hexdigest(),
        prefixes=tuple(records),
        **options,
    )


def local() -> BioregistryOverride:
    """State an explicit application rule with additional permitted spelling."""
    return BioregistryOverride(
        "absd", canonical_prefix="ABSD", aliases=("local-absd",), rule=RULE
    )


def test_actual_pinned_record_rules_aliases_and_no_ontology_declaration() -> None:
    snapshot = read(ACTUAL_RECORD, overrides=[local()])
    assert snapshot.report.verified and snapshot.registry is not None
    assert snapshot.registry.expand_curie("absd:abc") == NS + "abc"
    assert snapshot.registry.expand_curie("local-absd:abc") == NS + "abc"
    assert snapshot.registry.compact_iri(NS + "abc") == "ABSD:abc"
    assert snapshot.records == ACTUAL_RECORD
    exported = snapshot.records
    exported.clear()
    assert snapshot.records == ACTUAL_RECORD
    assert snapshot.payload == payload(ACTUAL_RECORD)
    assert ("absd", "pattern") in snapshot.uninterpreted
    with pytest.raises(InputError):
        replace(snapshot, payload=b"{}")
    pytest.importorskip("re2")
    example = "f" * 64
    values = [
        AccessionValue(URIRef(NS + example), "iri", Location("out", ("id",))),
        AccessionValue("ABSD:not-hex", "curie"),
    ]
    checked = check_accessions(values, snapshot.registry, rules=snapshot.rules)
    assert [item.status for item in checked.observations] == ["valid", "invalid"]
    assert checked.observations[0].original is values[0]
    absent = check_vocabulary(Graph(), [TermDeclaration(NS + example)])
    assert absent.outcome == "disagreement"
    assert any(issue.code == "vocabulary.term_missing" for issue in absent.issues)


def test_unsupported_source_rules_import_precedence_and_exact_fields() -> None:
    source = read(ACTUAL_RECORD)
    assert source.registry is not None and not source.report.verified
    assert source.rules == () and ("absd", "pattern") in source.unsupported
    for pattern in (False, 0, None, "", ".*"):
        missing_rule = read(
            {"p": {"uri_format": "https://example.org/$1", "pattern": pattern}}
        )
        assert not missing_rule.report.verified and missing_rule.rules == ()
        assert ("p", "pattern") in missing_rule.unsupported
    records: dict[str, object] = {
        "orcid": {
            "name": "ORCID",
            "synonyms": ["ORCID", "ORCiD"],
            "miriam": {
                "uri_format": "https://orcid.org/$1",
                "pattern": r"^\d{4}-\d{4}-\d{4}-\d{3}(\d|X)$",
            },
            "providers": [
                {
                    "code": "scholia",
                    "uri_format": "https://scholia.toolforge.org/orcid/$1",
                }
            ],
        }
    }
    untouched = read(records)
    assert untouched.registry is None
    assert ("orcid", "uri_format") in untouched.unsupported
    rule = AccessionRule(
        "https://orcid.org/",
        r"[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9X]",
        check_digit="iso7064-mod11-2",
        checksum_separators="-",
    )
    fixed = read(
        records,
        overrides=[BioregistryOverride("orcid", namespace=rule.namespace, rule=rule)],
    )
    assert fixed.report.verified and fixed.registry is not None
    assert fixed.records == records
    assert (
        fixed.registry.expand_curie("ORCiD:0000-0002-1825-0097")
        == rule.namespace + "0000-0002-1825-0097"
    )
    pytest.importorskip("re2")
    pytest.importorskip("stdnum")
    result = check_accessions(
        [
            AccessionValue("ORCID:0000-0002-1825-0097", "curie"),
            AccessionValue("ORCID:0000-0002-1825-0098", "curie"),
        ],
        fixed.registry,
        rules=fixed.rules,
    )
    assert [item.status for item in result.observations] == ["valid", "invalid"]


def test_conflicts_unsupported_fields_and_no_silent_alias_replacement() -> None:
    records: dict[str, object] = {
        "x": {
            "preferred_prefix": "X",
            "synonyms": ["old-x"],
            "uri_format": "urn:x:$1",
            "pattern": "[0-9]+",
        }
    }
    rule = AccessionRule("urn:x:", "[0-9]+")
    changed = read(
        records,
        overrides=[BioregistryOverride("x", canonical_prefix="chosen", rule=rule)],
    )
    assert changed.report.verified and changed.registry is not None
    assert dict(changed.registry.bindings) == {
        "x": "urn:x:",
        "X": "urn:x:",
        "old-x": "urn:x:",
        "chosen": "urn:x:",
    }
    duplicate = read(
        records,
        overrides=[
            BioregistryOverride("x", rule=rule),
            BioregistryOverride("x", rule=rule),
        ],
    )
    assert duplicate.report.outcome == "input_error" and duplicate.registry is None
    collision = read(
        {
            "x": {"uri_format": "urn:x:$1", "synonyms": ["common"]},
            "y": {"uri_format": "urn:y:$1", "synonyms": ["common"]},
        },
        overrides=[
            BioregistryOverride("x", rule=rule),
            BioregistryOverride("y", rule=AccessionRule("urn:y:", "[0-9]+")),
        ],
    )
    assert collision.report.outcome == "input_error" and collision.registry is None
    for field, value in (
        ("banana", "x"),
        ("provides_for", "other"),
        ("has_canonical", "other"),
    ):
        unsupported = read({"x": {"uri_format": "urn:x:$1", field: value}})
        assert (
            "x",
            field,
        ) in unsupported.unsupported and not unsupported.report.verified
    for value in ("urn:x:$1/tail", "urn:x:$1/$1"):
        unsupported = read({"x": {"uri_format": value}})
        assert (
            unsupported.registry is None
            and ("x", "uri_format") in unsupported.unsupported
        )
    alias = read(
        {"x": {"uri_format": "urn:x:$1", "synonyms": ["space alias"]}},
        overrides=[BioregistryOverride("x", rule=rule)],
    )
    assert not alias.report.verified and ("x", "synonyms") in alias.unsupported
    for bad in (BNode("urn:prefix"), Literal("urn:prefix")):
        with pytest.raises(InputError):
            BioregistryOverride(cast(str, bad))


def test_byte_pin_duplicate_keys_malformed_and_atomic_failures() -> None:
    raw = payload(ACTUAL_RECORD)
    for body, pin in (
        (raw + b" ", hashlib.sha256(raw).hexdigest()),
        (b'{"x":{},"x":{}}', hashlib.sha256(b'{"x":{},"x":{}}').hexdigest()),
        (b"[]", hashlib.sha256(b"[]").hexdigest()),
    ):
        result = read_bioregistry(body, revision=REVISION, sha256=pin, prefixes=["x"])
        assert result.report.outcome == "input_error" and result.registry is None
    missing = read_bioregistry(
        raw,
        revision=REVISION,
        sha256=hashlib.sha256(raw).hexdigest(),
        prefixes=["not-present"],
    )
    assert missing.report.outcome == "input_error" and missing.registry is None
    empty = read_bioregistry(
        raw, revision=REVISION, sha256=hashlib.sha256(raw).hexdigest(), prefixes=[]
    )
    assert not empty.report.verified and empty.registry is None
    bad_rule = read(
        ACTUAL_RECORD,
        overrides=[
            BioregistryOverride("absd", rule=AccessionRule("urn:wrong:", "[0-9]+"))
        ],
    )
    assert bad_rule.report.outcome == "input_error" and bad_rule.registry is None


def test_moved_bundle_offline_and_original_exact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = payload(ACTUAL_RECORD)
    archive = tmp_path / "input.zip"
    with ZipFile(archive, "w") as output:
        output.writestr("registry/subset.json", raw)
    moved = tmp_path / "moved.zip"
    archive.rename(moved)

    def unavailable(*args: object, **kwargs: object) -> None:
        raise AssertionError("Registry interpretation must remain offline")

    monkeypatch.setattr(socket, "socket", unavailable)
    result = read_bioregistry_bundle(
        moved,
        "registry/subset.json",
        revision=REVISION,
        sha256=hashlib.sha256(raw).hexdigest(),
        prefixes=["absd"],
        overrides=[local()],
    )
    assert result.report.verified and result.payload == raw
    assert result.records == ACTUAL_RECORD
    with pytest.raises(InputError):
        read_bioregistry_bundle(
            moved,
            "../registry/subset.json",
            revision=REVISION,
            sha256="0" * 64,
            prefixes=["absd"],
        )


def test_metadata_bounds_prefix_retention_and_fresh_recovery() -> None:
    raw = payload(ACTUAL_RECORD)
    pin = hashlib.sha256(raw).hexdigest()
    huge = read_bioregistry(
        raw,
        revision="é" * 100000,
        sha256=pin,
        prefixes=["absd"],
        limits=Limits(max_bytes=1000),
    )
    assert (
        huge.report.outcome == "exhausted"
        and huge.revision == ""
        and huge.payload == b""
    )
    fields = read(
        {"x": {"uri_format": "urn:x:$1", "banana": "x", "pattern": r"\d+"}},
        limits=Limits(max_issues=1),
    )
    assert fields.report.outcome == "exhausted"
    assert any(
        issue.code == "adapter.bioregistry_unverified" for issue in fields.report.issues
    )
    budget = Budget(Limits(max_work=1))
    stopped = read_bioregistry(
        raw, revision=REVISION, sha256=pin, prefixes=["absd"], budget=budget
    )
    assert stopped.report.outcome == "exhausted" and stopped.registry is None
    assert read(ACTUAL_RECORD, overrides=[local()]).report.verified
