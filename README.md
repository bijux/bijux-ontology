# bijux-ontology

Typed Python contracts, bounded RDF preparation and explicit syntactic transformations. This package reports finite supported checks and comparisons it cannot verify; it does not perform general OWL reasoning. The package has not been released to a package index.

## Development

Use Python 3.11+ and uv:

```sh
make install
make lint quality test-ci build
```

These targets use pinned `bijux-std` implementations. Generated output stays under `artifacts/`. Full `make bijux-std-checks` requires canonical repository registration, which remains unresolved; no replacement gate is provided.

## Supported capabilities

- Package configuration, canonical shared checks, bounded JSON/report contracts, native RDF primitives and explicit application declarations.
