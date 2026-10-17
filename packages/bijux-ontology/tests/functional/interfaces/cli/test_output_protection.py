"""Input discovery failures retain source files and emit their evidence to stdout."""

import json
from pathlib import Path

import pytest

from bijux_ontology.interfaces.cli import main


@pytest.mark.parametrize("timeout", [None, "5"])
@pytest.mark.parametrize("malformed_sibling", [False, True])
def test_invalid_source_metadata_never_overwrites_an_undiscovered_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    timeout: str | None,
    malformed_sibling: bool,
) -> None:
    ontology = tmp_path / "ontology.ttl"
    ontology.write_text(
        "<urn:Record> a <http://www.w3.org/2002/07/owl#Class> .", encoding="utf-8"
    )
    resource = tmp_path / "resource.ttl"
    original = b"original resource\n"
    resource.write_bytes(original)
    entries: dict[str, object] = {}
    if malformed_sibling:
        entries["urn:other"] = {"path": "other.ttl", "format": 123}
    entries["urn:resource"] = {
        "path": resource.name,
        "format": "turtle" if malformed_sibling else 123,
    }
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps(entries), encoding="utf-8")
    args = [
        "ontology",
        str(ontology),
        "--sources",
        str(sources),
        "--output",
        str(resource),
        "--json",
    ]
    if timeout is not None:
        args += ["--timeout", timeout]
    assert main(args) == 2
    assert resource.read_bytes() == original
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "input_error" and not report["verified"]
    assert report["issues"][0]["code"] == "input.invalid"
