# bijux-ontology-reasoning

Optional OWL 2 DL, EL, QL and RL membership checks through a pinned OWLAPI 5.5.1
worker and explicit Java 11 or newer. Imports start no JVM or download; core
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
