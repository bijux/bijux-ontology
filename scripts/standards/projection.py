"""Project accepted standard tooling without registering a consumer repository."""

import argparse
import hashlib
import re
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    """Refresh or verify canonical verifier inputs and Python Make modules."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    pin = ROOT / ".github/standards/bijux-std.sha"
    sha = pin.read_text(encoding="utf-8").strip()
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise SystemExit("Standard source pin must be a full lowercase commit SHA")
    helper = ROOT / ".bijux/shared/bijux-makes-py/repository/governance_projection.py"
    accepted_source = runpy.run_path(str(helper))["accepted_source"]
    source = Path(accepted_source(ROOT, sha))
    mappings = {
        path: path
        for path in (
            ".github/scripts/render_repo_configs.py",
            ".github/scripts/check_pinned_actions.py",
            ".github/scripts/check_protected_github_changes.py",
            ".github/standards/repo-config.manifest.json",
        )
    }
    mappings["makes/bijux-std.mk"] = "makes/bijux-std.mk"
    for path in (source / ".github/scripts/workflow_execution").glob("*.py"):
        relative = path.relative_to(source).as_posix()
        mappings[relative] = relative
    for path in (source / "shared/bijux-makes-py").rglob("*"):
        if path.is_file():
            relative = path.relative_to(source / "shared/bijux-makes-py").as_posix()
            mappings[f"makes/bijux-py/{relative}"] = path.relative_to(source).as_posix()
    failures = []
    for destination, original in sorted(mappings.items()):
        target = ROOT / destination
        expected = (source / original).read_bytes()
        if target.is_symlink():
            raise SystemExit(
                f"Managed projection must be a regular file: {destination}"
            )
        if args.refresh:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(expected)
        elif not target.is_file() or target.read_bytes() != expected:
            failures.append(destination)
    if failures:
        raise SystemExit("Standard projection differs: " + ", ".join(failures))
    checksum = ROOT / ".github/bijux-std-shared.sha256"
    if args.refresh:
        paths = {
            line.split(maxsplit=1)[1] for line in checksum.read_text().splitlines()
        }
        paths.update(mappings)
        checksum.write_text(
            "".join(
                f"{hashlib.sha256((ROOT / path).read_bytes()).hexdigest()}  {path}\n"
                for path in sorted(paths)
            ),
            encoding="utf-8",
        )
    print(f"Verified {len(mappings)} canonical tooling projections at {sha}")
    print("Upstream repository registration is unchanged")


if __name__ == "__main__":
    main()
