# bijux-ontology

Typed Python checks for supplied RDF vocabularies, JSON-LD usages and application declarations. General OWL reasoning is outside its scope.

See the [repository guide](https://github.com/bijux/bijux-ontology#readme) for installation, supported checks and examples.

`bijux_ontology.interfaces.adapters.jena.shacl.validate_jena_shapes` returns the
same `ShaclResult` for an explicitly prepared local Apache Jena 6.2 backend.
Core and local trusted SELECT constraints support selected shapes, focus nodes,
physical graphs and required execution coverage. Missing functions, custom
components and meta preflight remain unverified. Raw conformance, targets and
constraint coverage are separate; inspect each report. Java is optional.

Build the native worker with Maven and JDK 21 or newer from the repository root:

```sh
ontology_root="$(pwd)"
mvn -B -f packages/bijux-ontology/java/pom.xml \
  -Dmaven.repo.local="$ontology_root/artifacts/maven" \
  -DoutputDirectory="$ontology_root/artifacts/jena-bundle" \
  -DskipTests package dependency:copy-dependencies
cp artifacts/bijux-ontology/java/jena-worker-0.1.0.jar artifacts/jena-bundle/
python3 - <<'PYTHON'
import hashlib, json
from pathlib import Path
bundle = Path("artifacts/jena-bundle")
manifest = {"protocol": 1, "jena": "6.2.0",
            "main_class": "org.bijux.ontology.jena.JenaWorker",
            "jars": [{"name": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                     for p in sorted(bundle.glob("*.jar"))]}
raw = (json.dumps(manifest, indent=2) + "\n").encode()
(bundle / "manifest.json").write_bytes(raw)
print(hashlib.sha256(raw).hexdigest())
PYTHON
```

Use a fresh directory per build. Pass its printed pin, explicit Java executable,
bundle and artifact paths to `prepare_jena_backend` from
`bijux_ontology.interfaces.adapters.jena`. Runtime operations neither compile nor
retrieve dependencies. The current process profile requires POSIX ownership;
JVM heap, wall time and bounded transport constrain opaque native execution.
