"""Refresh refuses conflicting user work before changing canonical projections."""

import subprocess
from pathlib import Path

import pytest

from bijux_ontology_dev.standards.projection import _preserve_local_changes


@pytest.mark.parametrize("state", ["unstaged", "staged", "untracked"])
def test_refresh_preserves_local_tooling_changes(tmp_path: Path, state: str) -> None:
    repo = tmp_path / "repository"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    target = repo / "tool.py"
    source = tmp_path / "accepted-source"
    source.mkdir()
    (source / "tool.py").write_text("accepted replacement\n")
    if state != "untracked":
        target.write_text("original\n")
        subprocess.run(["git", "-C", str(repo), "add", "tool.py"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "commit.gpgsign=false",
                "-c",
                f"core.hooksPath={source / 'hooks'}",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.org",
                "commit",
                "--quiet",
                "-m",
                "test(std): record tracked tooling",
            ],
            check=True,
        )
    target.write_text("user work\n")
    if state == "staged":
        subprocess.run(["git", "-C", str(repo), "add", "tool.py"], check=True)
        target.write_text("original\n")
    before = target.read_bytes()
    index = subprocess.check_output(["git", "-C", str(repo), "ls-files", "--stage"])
    with pytest.raises(SystemExit, match="Preserve"):
        _preserve_local_changes(repo, source, {"tool.py": "tool.py"})
    assert target.read_bytes() == before
    assert (
        subprocess.check_output(["git", "-C", str(repo), "ls-files", "--stage"])
        == index
    )
