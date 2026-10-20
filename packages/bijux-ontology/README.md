# bijux-ontology

Typed Python checks for supplied RDF vocabularies, JSON-LD usages and application declarations. General OWL reasoning is outside its scope.

See the [repository guide](https://github.com/bijux/bijux-ontology#readme) for installation, supported checks and examples.

Domain formats, framework extraction and optional Java/Node integrations belong to
the separate `bijux-ontology-adapters` distribution. The core has no adapter dependency.

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
