"""Unconfigured shared Make targets must not masquerade as successful checks."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("target", ["api", "security", "sbom", "docs"])
def test_unconfigured_gates_fail_explicitly(target: str) -> None:
    root = Path(__file__).resolve().parents[5]
    result = subprocess.run(
        ["make", target],
        cwd=root,
        env={
            key: value
            for key, value in os.environ.items()
            if key not in {"MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES"}
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "gate is not configured" in result.stderr
