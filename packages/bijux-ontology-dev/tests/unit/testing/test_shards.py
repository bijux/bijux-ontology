"""Test grouping covers the canonical inventory exactly once, including new files."""

from pathlib import Path

import pytest

from bijux_ontology_dev.testing.shards import partition, read_weights
from bijux_ontology_dev.testing.shards import test_files as discover_test_files


def test_groups_cover_real_make_owners_and_remain_balanced() -> None:
    root = Path(__file__).resolve().parents[5]
    files = discover_test_files(root)
    weights = read_weights(root / "configs/test-groups.json")
    groups = partition(files, weights)
    assert len(groups) == 5 and all(groups)
    assert sorted(path for group in groups for path in group) == list(files)
    assert any("/bijux-ontology/tests/" in path for path in files)
    assert any("/bijux-ontology-dev/tests/" in path for path in files)
    assert groups == partition(tuple(reversed(files)), weights)
    costs = [sum(weights.get(path, 1.0) for path in group) for group in groups]
    assert max(costs) - min(costs) <= max(weights.get(path, 1.0) for path in files)
    added = "packages/bijux-ontology/tests/functional/test_added.py"
    assert sum(added in group for group in partition((*files, added), weights)) == 1


def test_inventory_uses_only_selected_package_owners(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text(
        "list-test-packages:\n\t@printf '%s\\n' core tools\n", encoding="utf-8"
    )
    for package in ("core", "tools", "unselected"):
        folder = tmp_path / "packages" / package / "tests" / "unit"
        folder.mkdir(parents=True)
        (folder / "test_example.py").write_text("", encoding="utf-8")
    assert discover_test_files(tmp_path) == (
        "packages/core/tests/unit/test_example.py",
        "packages/tools/tests/unit/test_example.py",
    )
    with pytest.raises(ValueError, match="nonempty"):
        partition(("one.py",), {})
    with pytest.raises(ValueError, match="finite"):
        partition(tuple(f"{index}.py" for index in range(5)), {"0.py": float("nan")})
