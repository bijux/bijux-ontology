"""Isolated JSON entry point for bounded local RDF query execution."""

import sys

from bijux_ontology.rdf.queries.execution import process_request


def main() -> int:
    """Accept one bounded JSON request; emit one bounded structured result."""
    cap = int(sys.argv[1])
    sys.stdout.buffer.write(process_request(sys.stdin.buffer.read(cap + 1), cap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
