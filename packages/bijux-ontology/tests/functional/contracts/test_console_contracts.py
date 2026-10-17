"""Console failures use the same validated inputs and corrective evidence as Python."""

import json
from pathlib import Path

import pytest

from bijux_ontology.interfaces.cli import main


def test_console_reports_duplicate_input_and_human_mapping_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = tmp_path / "document.json"
    expectations = tmp_path / "expectations.json"
    document.write_text(
        '{"@context":{"title":"https://example.org/wrong"},"title":"Example"}',
        encoding="utf-8",
    )
    expectations.write_text(
        '[{"path":["title"],"predicate":"https://example.org/right"}]', encoding="utf-8"
    )
    assert main(["context", str(document), str(expectations)]) == 1
    human = capsys.readouterr().out
    assert "https://example.org/right" in human
    assert "https://example.org/wrong" in human
    expectations.write_text(
        '[{"path":["title"],"predicate":"https://example.org/right","predicate":"https://example.org/wrong"}]',
        encoding="utf-8",
    )
    assert main(["context", str(document), str(expectations), "--json"]) == 2
    structured = json.loads(capsys.readouterr().out)
    assert structured["outcome"] == "input_error"
    assert not structured["verified"]
    assert "/0/predicate" in structured["issues"][0]["message"]
