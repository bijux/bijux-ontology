# bijux-ontology

Typed Python checks for supplied RDF vocabularies, JSON-LD usages and application declarations. General OWL reasoning is outside its scope.

See the [repository guide](https://github.com/bijux/bijux-ontology#readme) for installation, supported checks and examples.

`bijux_ontology.interfaces.adapters.jena.shacl.validate_jena_shapes` returns the
same `ShaclResult` for an explicitly prepared local Apache Jena 6.2 backend.
Core and local trusted SELECT constraints support selected shapes, focus nodes,
physical graphs and required execution coverage. Missing functions, custom
components and meta preflight remain unverified. Raw conformance, targets and
constraint coverage are separate; inspect each report. Java is optional.

Build the shipped `java/` directory from an unpacked source distribution with
Maven and JDK 21 or newer; no repository checkout is needed:

```sh
python -m bijux_ontology.interfaces.adapters.jena.build \
  --source /absolute/path/to/unpacked/java \
  --maven /absolute/path/to/mvn --java /absolute/path/to/jdk/bin/java \
  --build-directory /absolute/path/to/artifacts/build \
  --dependency-directory /absolute/path/to/artifacts/maven \
  --output /absolute/path/to/artifacts/jena-bundle
```

The explicit build downloads dependencies only into the supplied Maven location,
checks real positive/negative operations, and prints the verified manifest pin.
Output must be new; failed output is removed. Logs remain in the build directory.

Use a fresh directory per build. Pass its printed pin, explicit Java executable,
bundle and artifact paths to `prepare_jena_backend` from
`bijux_ontology.interfaces.adapters.jena`. Runtime operations neither compile nor
retrieve dependencies. The current process profile requires POSIX ownership;
JVM heap, wall time and bounded transport constrain opaque native execution.

`interfaces.adapters.sparql.acquire_endpoint` explicitly acquires an
`EndpointQuery` with typed subject/predicate/object parameters and an optional
named graph. Supply an
`AcquisitionPolicy` with authorized origins, page size and maximum pages. The
returned `source` is an ordinary owned RDF slice for later offline checks.
Completeness additionally requires `EndpointCompleteness`: a server guarantee
that a supplied version parameter selects immutable data, with per-page version,
exact row-total and no-truncation headers. Missing guarantees, short/capped pages,
changed receipts and cross-page blank identities cannot certify a complete slice.
The initial profile uses parameterized distinct-triple SELECT over HTTP GET;
it does not accept executable query fragments. No validation fetches the endpoint.

The same local Jena bundle supports `jena.geosparql.check_geometries` with explicit
`GeometryValue`, `SpatialRelation` and `SpatialDistance` records. The finite
GeoSPARQL 1.0 profile covers two-dimensional WKT Point/LineString/Polygon/empty and
securely qualified GML 3.2 Point/pos. Only bundled CRS84 and EPSG4326 are supported,
with their actual axis order; distance requires explicit planar angular degrees.
Original RDF literals, geometric equality and topology stay distinct. Invalid
geometry is a completed negative; dependent operations, unavailable CRS transforms,
other serializations and unsupported units remain unverified.

`workflows.revisions.prepare_revision` seals immutable ontology/import descriptors,
model declarations, closed contexts and optional selected shapes. Required shape
preparation runs actual positive probes. Only verified preparation yields a
`PreparedRevision`; its semantic `identity` binds all inputs and engines, while
`revision` identifies one local activation. Supply `expected=identity` to require
the same complete preparation. Mutable Paths and live graphs are unsupported.
Use `RevisionManager.activate(candidate, expected_active=...)`, then `lease()` for
ordinary model or shape checks against that exact revision. Explicit rollback,
retirement, retained-count/byte caps and close-and-drain preserve in-flight runs.

`workflows.corpus.ResumeCorpus(path).run(lease, records, keys=...)` resumes explicit
`CorpusRecord` identities against an actual revision lease. The bounded local
SQLite store commits complete independent RDF-model outcomes at record boundaries;
changed inputs, bindings or semantic preparation invalidate reuse. Complete
negatives stay negative, and unknown or exhausted records stay pending. Selected
global key checks run again over every current record. Inspect `semantic_report`
and `checkpoint_report` separately: a fresh semantic recheck cannot hide corrupt
or incomplete checkpoint durability. This profile supports immutable native RDF
inputs and named subjects, with trusted local checkpoint state.

`workflows.finding_streams.stream_model_findings(plan, records, usages, sink=...)`
checks ordinary prepared-model records and delivers typed results and findings to
a `JsonlFindingSink` or synchronous `CallbackFindingSink`. `FindingStreamLimits`
separately caps records, emitted findings, retained samples and output bytes; each
record keeps its ordinary engine issue limit, with cumulative run work bounds.
Raw outcomes and coverage remain independent of sample filters. The returned
summary separates semantic and export completion; a wire footer confirms prior
frames finished, while final export confirmation follows its successful flush.
Sink failures, cancellation and truncation stay visible. Whole-report native
engines are outside this incremental profile; callbacks retain ordinary
cooperative lifetimes.
