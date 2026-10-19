"""Result bounds retain complete earlier diagnostics instead of erasing them."""

import json
from pathlib import Path

from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.interfaces.execution.checkpoints import (
    read_completed,
    retain_completed,
)
from bijux_ontology.limits import Limits


def test_checkpoint_cap_retains_the_complete_prefix_and_marks_lost_evidence(
    tmp_path: Path,
) -> None:
    first = Report("first", 1, (Issue("example.conflict", "Meaning differs"),))
    line = (json.dumps(first.to_dict()) + "\n").encode()
    limits = Limits(max_total_bytes=len(line) + 10)
    retain_completed(first, tmp_path, max_bytes=limits.max_total_bytes)
    retain_completed(first, tmp_path, max_bytes=limits.max_total_bytes)
    retained = read_completed(tmp_path, limits)
    assert retained[0] == first
    assert retained[-1].outcome == "exhausted"
    assert (tmp_path / "completed.jsonl").stat().st_size <= limits.max_total_bytes
    # Interrupted older writers can leave an oversized or incomplete suffix.
    (tmp_path / "completed.jsonl").write_bytes(line + b"{" * 100)
    retained = read_completed(tmp_path, limits)
    assert retained[0] == first
    assert retained[-1].outcome == "exhausted"
