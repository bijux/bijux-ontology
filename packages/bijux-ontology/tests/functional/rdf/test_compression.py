"""Explicit gzip documents preserve terms, finite bounds and caller ownership."""

import gzip
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import XSD

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.workflows.projects.checking import PreparedChecks
from bijux_ontology.workflows.projects.configuration import ProjectConfig

BASE = "https://compression.example/"
PAYLOAD = (
    b'<item> <https://compression.example/value> "01"^^'
    b"<http://www.w3.org/2001/XMLSchema#integer> ."
)


class ShortReads(BytesIO):
    """Exercise ordinary binary streams that return fewer bytes than requested."""

    def read(self, size: int | None = -1) -> bytes:
        return super().read(min(3, size) if size is not None and size >= 0 else 3)


def test_paths_bytes_short_streams_and_vocabulary_preserve_exact_rdf(
    tmp_path: Path,
) -> None:
    compressed = gzip.compress(PAYLOAD, mtime=0)
    path = tmp_path / "ontology.bin"
    path.write_bytes(compressed)
    stream = ShortReads(compressed)
    expected = set(prepare_source(PAYLOAD, format="turtle", base=BASE).graph())
    for content in (path, compressed, stream):
        prepared = prepare_source(
            content, format="turtle", base=BASE, compression="gzip"
        )
        assert set(prepared.graph()) == expected
        assert prepared.literal_fidelity == "source_terms"
        assert next(prepared.graph().objects()) == Literal(
            "01", datatype=XSD.integer, normalize=False
        )
        assert prepared.document_iri == prepared.effective_url == BASE
    assert not stream.closed
    assert (
        set(
            prepare_rdf(
                compressed, format="turtle", base=BASE, compression="gzip"
            ).graph()
        )
        == expected
    )
    source = RdfSource(compressed, "turtle", BASE, compression="gzip")
    assert set(load_vocabulary(source).graph()) == expected
    assert (
        set(prepare_source(ShortReads(PAYLOAD), format="turtle", base=BASE).graph())
        == expected
    )


def test_separate_input_expansion_and_shared_cumulative_limits() -> None:
    compressed = gzip.compress(PAYLOAD, mtime=0)
    for value in (compressed, ShortReads(compressed)):
        with pytest.raises(LimitExceeded, match="max_compressed_bytes"):
            prepare_source(
                value,
                format="turtle",
                compression="gzip",
                max_compressed_bytes=len(compressed) - 1,
            )
    expanded = b"#" + b"a" * 10_000 + b"\n" + PAYLOAD
    bomb = gzip.compress(expanded, mtime=0)
    stream = BytesIO(bomb)
    with pytest.raises(LimitExceeded, match="Expanded gzip RDF"):
        prepare_source(stream, format="turtle", compression="gzip", max_bytes=1_000)
    assert not stream.closed
    # Logical document identity and effective base are separately retained fields.
    identity_bytes = 2 * len(BASE.encode("utf-8"))
    total = len(compressed) + len(PAYLOAD) + identity_bytes
    budget = Budget(Limits(max_total_bytes=total, max_resources=1))
    assert (
        len(
            prepare_source(
                compressed,
                format="turtle",
                base=BASE,
                compression="gzip",
                budget=budget,
            ).graph()
        )
        == 1
    )
    with pytest.raises(LimitExceeded, match="max_total_bytes"):
        budget.consume("bytes")
    with pytest.raises(LimitExceeded, match="max_total_bytes"):
        prepare_source(
            compressed,
            format="turtle",
            base=BASE,
            compression="gzip",
            budget=Budget(Limits(max_total_bytes=total - 1)),
        )


def test_multimember_integrity_failures_never_publish_partial_rdf() -> None:
    first = b"<urn:a> <urn:p> <urn:b> .\n"
    second = b"<urn:c> <urn:p> <urn:d> .\n"
    member = gzip.compress(first, mtime=0)
    concatenated = member + gzip.compress(second, mtime=0)
    assert (
        len(prepare_source(concatenated, format="nt", compression="gzip").graph()) == 2
    )
    assert (
        len(
            prepare_source(
                member + gzip.compress(b"", mtime=0), format="nt", compression="gzip"
            ).graph()
        )
        == 1
    )
    crc = bytearray(member)
    crc[-8] ^= 1
    for malformed in (
        member[:-1],
        concatenated[:-1],
        bytes(crc),
        member + b"invalid",
        b"",
        b"not gzip",
    ):
        stream = BytesIO(malformed)
        with pytest.raises(InputError, match="gzip"):
            prepare_source(stream, format="nt", compression="gzip")
        assert not stream.closed
    over = BytesIO(concatenated)
    with pytest.raises(LimitExceeded, match="max_bytes"):
        prepare_source(
            over,
            format="nt",
            compression="gzip",
            max_bytes=len(first),
            max_compressed_bytes=len(concatenated),
        )
    assert not over.closed
    assert len(prepare_source(member, format="nt", compression="gzip").graph()) == 1


def test_explicit_metadata_and_read_failures_preserve_caller_handles(
    tmp_path: Path,
) -> None:
    compressed = gzip.compress(PAYLOAD, mtime=0)
    path = tmp_path / "ontology.ttl.gz"
    path.write_bytes(compressed)
    with pytest.raises(InputError, match="explicit format"):
        prepare_source(path, compression="gzip")
    stream = BytesIO(compressed)
    with pytest.raises(InputError, match="Unsupported RDF format"):
        prepare_source(stream, compression="gzip", format="archive")
    assert stream.tell() == 0 and not stream.closed
    with pytest.raises(InputError, match="Cannot parse nt"):
        prepare_source(compressed, compression="gzip", format="nt", base=BASE)
    for content in ("text", Graph()):
        with pytest.raises(InputError, match="requires bytes"):
            RdfSource(content, compression="gzip")
    text_stream = StringIO("not binary")
    with pytest.raises(InputError, match="must return bytes"):
        prepare_source(text_stream, format="nt", compression="gzip")
    assert not text_stream.closed
    invalid: Any = "zip"
    with pytest.raises(InputError, match="Unsupported RDF compression"):
        prepare_source(compressed, format="nt", compression=invalid)
    with pytest.raises(InputError, match="positive integer"):
        prepare_source(
            compressed, format="nt", compression="gzip", max_compressed_bytes=0
        )
    closed = BytesIO(compressed)
    closed.close()
    with pytest.raises(ResourceError, match="stream"):
        prepare_source(closed, format="nt", compression="gzip")
    assert set(
        prepare_source(b"<urn:a> <urn:p> <urn:b> .", format="nt").graph().subjects()
    ) == {URIRef("urn:a")}


def test_compressed_replacement_is_decoded_once_and_keeps_previous_revision(
    tmp_path: Path,
) -> None:
    """Owned decoded bytes lose the compression marker before cached preparation."""
    path = tmp_path / "ontology.ttl"
    path.write_bytes(b"<urn:original> <urn:p> <urn:o> .")
    original = PreparedChecks(
        ProjectConfig(
            {
                "version": 1,
                "rdf": {"ontology": {"path": str(path), "format": "turtle"}},
                "checks": [
                    {"name": "selected", "kind": "vocabulary", "source": "ontology"}
                ],
            },
            directory=tmp_path,
        )
    )
    initial = original.source("ontology")
    revised = original.replace_resources(
        rdf={
            "ontology": RdfSource(
                gzip.compress(PAYLOAD, mtime=0),
                "turtle",
                BASE,
                compression="gzip",
            )
        }
    )
    source = revised.source("ontology")
    assert source is revised.source("ontology")
    assert {str(value) for value in source.graph().objects()} == {"01"}
    assert {str(subject) for subject in source.graph().subjects()} == {BASE + "item"}
    assert original.source("ontology") is initial
    assert {str(subject) for subject in initial.graph().subjects()} == {"urn:original"}
