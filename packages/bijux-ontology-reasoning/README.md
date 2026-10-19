# bijux-ontology-reasoning

Optional OWL 2 profile checks and DL diagnosis through pinned OWLAPI 5.5.1 and
HermiT 1.4.5.519 with explicit Java 11 or newer. Imports start no JVM or download; core
bijux-ontology remains usable without Java. Profile membership does not establish
consistency, satisfiability or entailment. Incomplete parsing remains unknown.

Build a bundle from the repository root with Maven and a Java development kit.
Use a fresh bundle directory for each revision; native runtime operations never
compile or fetch dependencies.

```sh
ontology_root="$(pwd)"
mkdir "$ontology_root/artifacts/owl-profile-bundle"
mvn -B -f packages/bijux-ontology-reasoning/java/pom.xml \
  -Dmaven.repo.local="$ontology_root/artifacts/maven" \
  -DoutputDirectory="$ontology_root/artifacts/owl-profile-bundle" \
  -DskipTests package dependency:copy-dependencies
cp artifacts/bijux-ontology-reasoning/java/owl-profile-worker-0.1.0.jar \
  artifacts/owl-profile-bundle/
python3 - <<'PYTHON'
import hashlib, json
from pathlib import Path
bundle = Path("artifacts/owl-profile-bundle")
manifest = {
    "protocol": 1,
    "owlapi": "5.5.1",
    "main_class": "org.bijux.ontology.profiles.ProfileWorker",
    "loader_profile": "turtle-permissive-no-punning-repair-offline",
    "jars": [{"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
             for path in sorted(bundle.glob("*.jar"))],
}
raw = (json.dumps(manifest, indent=2) + "\n").encode()
(bundle / "manifest.json").write_bytes(raw)
print(hashlib.sha256(raw).hexdigest())
PYTHON
```

Pass that printed SHA to `prepare_profile_backend(Path(java), Path(bundle),
manifest_sha256=sha, artifacts=Path("artifacts/owl-jobs"))` from
`bijux_ontology_reasoning.profiles`. Then call
`check_profile(backend, prepared_source, profile="owl2-dl", imports=offline_map)`.
Inspect `.in_profile`, `.violations`, `.loaders` and `.report`; a raw native answer
alone does not qualify the supplied RDF. The finite runtime profile supports
POSIX process ownership and one physical graph per supplied document. Missing
imports, unavailable runtimes and exhausted execution cannot pass. Original
anonymous identities shared across separately loaded documents are unsupported;
independent equal blank labels stay distinct.

The `documents` API captures original OWL/XML, Functional Syntax, Manchester
Syntax or OBO with `prepare_owl_document(content, format=...,
document_iri=..., effective_url=...)`. Call
`convert_ontology(backend, document, format="functional", imports=offline_map)`
for a maintained structural roundtrip. Inspect `.content`, `.changes`,
`.lossless` and `.report`. The default rejects lossy output; `loss="report"`
returns an explicit projection while retaining findings. Selected `profiles=`
record actual membership before and after conversion.

Native text uses UTF-8. OWL/XML supports absolute IRIs and qualified fragment
references; unsupported encodings, relative interpretation and anonymous
individual correspondence remain unknown. Only captured imports are available.
Preservation concerns annotated axioms and ontology headers, not original bytes
or exact RDF lexical identity.

Use `axioms.inspect_axioms(backend, source, axiom_types=None)` for bounded,
immutable typed observations of original native documents or selected prepared
RDF. Restrictions, object/data expressions and nested annotations retain their
roles. `.unsupported` records actual selected axioms outside the finite model;
`.exclusions` records families explicitly omitted by the caller. Missing input
structure or incomplete execution prevents a verified complete projection.
Inspection makes no consistency, satisfiability or entailment claim.

`diagnosis.check_consistency(backend, source, named_classes=None)` asks actual
ontology consistency and separate named-class satisfiability questions. A
consistent ontology may contain unsatisfiable classes. An inconsistent ontology
provides no ordinary class proof. `None` selects the actual class signature;
explicit classes must occur in the selected closure. Inspect `.consistent`,
`.consistency_report`, `.classes`, `.loaders` and `.report`. The operation requires
qualified original input, complete supplied imports and OWL 2 DL membership.
Unsupported datatypes, axioms and failed execution remain unknown. The reported
Maven artifact version and native reasoner version are retained separately.

`entailment.check_entailments(backend, source, obligations)` accepts existing typed
`SubClassOf` and `ClassAssertion` requests with supported class expressions.
Each answer is `entailed`, `not_entailed` or `unknown`. A negative assertion uses
an explicit `ClassComplement`; failure to entail a positive assertion never
proves its negation. Actual consistency and complete input qualification precede
all proofs. Unsupported queries retain their positions as unknown answers.

`patterns.prepare_dosdp_pattern(content, prefixes=...)` captures the JSON subset
of DOSDP's YAML input. `check_patterns(backend, source, patterns, expected,
scope=...)` retains actual generated queries, complete named filler assignments
and the single maintained RDFLib engine answer. Distinct assignments remain
distinct; `extras="allow"` explicitly relaxes only the expected inventory.
Syntactic absence does not prove logical nonconformance. Inferred matching,
nontrivial variable constraints and unsupported pattern fields remain unknown.
Use a separate local [DOSDP 0.20.0 distribution](https://github.com/INCATools/dosdp-tools/releases/tag/v0.20.0)
with `prepare_dosdp_backend`: its pinned manifest has `protocol=1`,
`dosdp="0.20.0"`, `main_class="org.monarchinitiative.dosdp.cli.Main"` and ordered
`jars` entries containing exact `name`/`sha256` pairs. Its 194-jar dependency set
requires an explicit preparation allowance such as
`Limits(max_total_bytes=200_000_000)`; subsequent checks use ordinary limits.
Pattern paths, prefixes and required ontology imports are local and explicit.
