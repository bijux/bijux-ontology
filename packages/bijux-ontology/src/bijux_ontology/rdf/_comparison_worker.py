"""Isolated byte entry point for exact bounded RDF comparison."""

import sys

from bijux_ontology.rdf.comparison import _compare_request


def main() -> int:
    """Read a finite parent-owned JSON request and emit only one boolean result."""
    try:
        maximum = int(sys.argv[1])
        if maximum <= 0:
            return 2
        payload = sys.stdin.buffer.read(maximum + 1)
        sys.stdout.buffer.write(_compare_request(payload, maximum))
        return 0
    except (ValueError, IndexError, TypeError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
