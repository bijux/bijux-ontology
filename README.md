# bijux-ontology

Python tools for checking that application data, vocabulary declarations and ontology definitions agree. Use the typed library or CLI to inspect RDF terms, verify JSON-LD mappings, check model contracts and actual output, validate SKOS or supplied SHACL shapes, and assess ontology upgrades. The separate `bijux-ontology-adapters` distribution owns framework extraction and domain integrations.

The separate `bijux-ontology-reasoning` distribution provides Java-backed OWL profile, consistency, satisfiability and supported entailment checks. The core library's finite structural checks do not replace a reasoner. Both preserve findings, incomplete work and policy acceptance as separate outcomes; neither establishes scientific truth or correctness of unseen application output.

## Install and verify

Python 3.11+ and uv are required for development. This version is unpublished.

```sh
make install
make check
artifacts/root/check-venv/bin/python examples/check_declarations.py
```

Install the resulting wheel into a consumer environment:

```sh
uv pip install artifacts/bijux-ontology/build/bijux_ontology-0.1.0-py3-none-any.whl
# JSON-LD/SHACL capabilities and optional framework integrations:
uv pip install 'artifacts/bijux-ontology/build/bijux_ontology-0.1.0-py3-none-any.whl[jsonld,shacl]' \
  'artifacts/bijux-ontology-adapters/build/bijux_ontology_adapters-0.1.0-py3-none-any.whl[pydantic,linkml]'
```

Canonical targets use the pinned `bijux-std` implementations. `make bijux-std-checks` verifies accepted shared components and verifier inputs; `make check-python-baseline` checks Python configuration. This repository is not registered in the upstream standards manifest. Its CI profile verifies alignment locally; formal registration will replace that profile with canonical workflow projection. Owner-authored PRs require the standard `owner-self-signoff` label. Generated files stay under `artifacts/`.

Core RDF and vocabulary operations do not require PyLD. Install `bijux-ontology[jsonld]` for JSON-LD processing. Hosts pinned to another PyLD version can use the core without changing their dependency requirements; JSON-LD operations require a compatible runtime.

## Use Python

```python
from pathlib import Path
from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

vocabulary = load_vocabulary(Path("ontology.ttl"))
view = inspect_vocabulary(vocabulary)
term = view.lookup("https://example.org/Image")
report = check_vocabulary(vocabulary, [TermDeclaration(term.iri)])
report.raise_for_findings()
```

Runnable examples cover [inspection and declarations](examples/check_declarations.py), [multilingual SKOS](examples/multilingual_vocabulary.py), [catalogue upgrades](examples/catalogue_upgrade.py), [controlled resources](examples/controlled_resources.py), [custom annotations](examples/annotation_conventions.py) and [dataclasses](examples/dataclass_declarations.py). The [recipe guide](docs/integration.md) identifies their optional dependencies. Framework adapters use explicit RDF metadata; required Python fields do not imply guaranteed RDF emission.

| Operation | Supported scope | Incomplete behaviour |
| --- | --- | --- |
| `check_vocabulary`, `check_ontology` | Exact declarations, selected coverage, references and structural encoding | Missing resources fail input; unsupported expressions stay unverified |
| `inspect_vocabulary`, `check_skos` | Asserted facts, bounded navigation, finite SKOS integrity and explicit publication rules | Ambiguous labels, unresolved targets and unknown deprecation remain visible |
| `check_context`, `check_emission` | Actual selected JSON-LD occurrences, value traits and representative output | Unattributable or unsupported occurrences cannot verify |
| `check_models` | Finite named hierarchy, equivalent/inverse properties, explicit disjointness and literal contracts | Unknown serializers and relevant unsupported OWL remain unverified |
| `assess_upgrade` | Before/after facts, affected uses and rechecked consumer contracts | Verdict distinguishes demonstrated breaks from unassessed impact |
| `validate_shapes` | Supplied data and shapes; real optional backend, inference disabled | Empty required shape scopes, missing extras and backend failures cannot verify |
| `documents.export`, `rdf.transforms` | Copied syntactic candidates and reparsed RDF preservation checks | Lossy output is refused or explicitly excluded; no automatic domain correction |

RDF text preserves exact supported literal terms. RDFLib inputs retain the terms supplied by their caller; earlier normalisation cannot be recovered. Dataset operations require explicit selection or union. TriG retains declared empty named graphs where both maintained parsers support the input; otherwise primary quads retain explicit incomplete inventory and complete-inventory requests reject that scope. Synchronous APIs use work budgets; CLI deadlines interrupt an owned worker. POSIX worker groups include owned descendants. Windows Job ownership is implemented but awaits actual Windows qualification; unsupported process ownership fails before execution.

## Use the CLI

```sh
bijux-ontology vocabulary ontology.ttl inventory.json --coverage --namespace https://example.org/ --json
bijux-ontology context document.json expectations.json --sources resources.json
bijux-ontology inspect ontology.ttl --search colour --language en-GB
bijux-ontology skos concepts.ttl --rule label-integrity --rule language-coverage --language en
bijux-ontology upgrade before.ttl after.ttl --declarations models.json --json
bijux-ontology project checks.json --explain
bijux-ontology explain context.mapping_mismatch
```

`python -m bijux_ontology` uses the same commands. Subcommand help gives minimal input examples. JSON preserves complete outcomes; `--display-limit` only shortens human output. An inventory is an array of exact IRI strings or `{ "iri": "…", "kinds": ["…"] }` objects. Expectations select usages, for example `{ "path": ["title"], "predicate": "https://example.org/title" }`. Project and source-map filenames resolve relative to their containing file; logical URLs remain separate.

Exit **0** means policy accepts the result, **1** means findings or strict unknowns fail policy, and **2** means invalid or exhausted required work. `--strict`, `--minimum-severity` and exact `--permit CODE LOCATION` exceptions never erase evidence or turn it into verification. Use `--json` for full findings and `--timeout` for expensive CLI work. Input failures return evidence to stdout when output safety is unestablished.

Supply contexts and imports explicitly. Default loaders never fetch missing resources; custom loaders own their retrieval behaviour. Examples show a shared resource snapshot for checking and conversion. Keep existing submission validation and reasoning gates: these checks do not prove ontology consistency, satisfiability, scientific truth or compatibility for unseen data.

See [migration notes](docs/migration.md) and [consumer integration](docs/integration.md).

The uv workspace contains four independently built packages: `bijux-ontology` for generic checks, `bijux-ontology-adapters` for ecosystem integrations, `bijux-ontology-reasoning` for OWL reasoning, and `bijux-ontology-dev` for repository tooling. Each package owns its source, resources and tests.

## Supported Python entry points

These concrete entry modules are the supported consumer surface. Internal implementation stages may move without changing these imports. Types retain the same identity when returned by another operation; no root-level export registry is needed.

| Responsibility | Entry modules and symbols |
| --- | --- |
| Prepare RDF | `documents.preparation.prepare_source`; `rdf.sources.types.RdfSource`; `rdf.sources.snapshots.PreparedSource` |
| Declare intent | `declarations.types`: `TermDeclaration`, `ModelDeclaration`, `FieldDeclaration`, `ValueDeclaration` |
| Inspect and check vocabularies | `vocabulary.sources.loading.load_vocabulary`; `vocabulary.browsing.inspection.inspect_vocabulary`; `vocabulary.checking.check_vocabulary` |
| Check models and output | `models.contracts.checking.check_models`; `models.emission.checking.check_emission`; `models.emission.rdf.check_rdf_emission`; `models.emission.types`: `ModelUsage`, `RdfModelUsage` |
| Check JSON-LD intent | `jsonld.usages.checking.check_context`; `jsonld.contracts.expectations.ContextExpectation` |
| Validate supplied shapes | `shacl.checking.validate_shapes`; `shacl.evidence.results.ShaclResult` |
| Bound work and inspect results | `execution.limits`: `Limits`, `Budget`; `diagnostics.reports.Report`; `diagnostics.errors`: `InputError`, `ResourceError`, `LimitExceeded` |

Paths in this table are relative to `bijux_ontology`. JSON-LD and SHACL operations require their respective extras. Importing the package root starts no workers and loads no optional framework; importing an operation loads its implementation. Other advanced operations remain documented by their concrete owner without a blanket stability promise for every internal module.

## Package ownership

- Python APIs live in their owning domain modules; `interfaces/cli` owns commands, `interfaces/execution` owns isolated execution, while `bijux_ontology_adapters` owns domain integrations and framework extraction in its separate distribution.
- `execution` owns shared operation budgets, deadlines and finite input capture; `processes` owns operating-system worker lifetimes.
- `rdf` owns native graph primitives and transformations; `jsonld` owns context processing and conversion; `owl` owns finite construct interpretation. These engines do not depend on one another's higher-level checks.
- `documents` composes RDF and JSON-LD preparation and verified export. `vocabulary`, `models`, `skos`, `data` and `shacl` own their respective checks; `declarations` owns application contracts.
- `shacl.native` owns prepared native validation requests and result adoption; adapters own their external runtimes.
- `workflows` composes project jobs, ontology checks and consumer upgrade assessments. Semantic engines do not import workflows or interfaces.

Tests separate focused unit cases, functional capability contracts and installed-package E2E; generated environments and reports remain under `artifacts/`.
