# bijux-ontology-adapters

Optional framework, vocabulary and data-format bridges for `bijux-ontology`.
Install only the extras needed by the selected integration. The core library
remains usable without this distribution or its external runtimes.

See the [repository guide](https://github.com/bijux/bijux-ontology#readme) and
[contracts](https://github.com/bijux/bijux-ontology/blob/main/docs/contracts.md)
for supported semantics and explicit incomplete outcomes.

For local Apache Jena validation, build the shipped `java/` directory from an
unpacked source distribution using Maven and JDK 21 or newer:

```sh
python -m bijux_ontology_adapters.jena.build \
  --source /absolute/path/to/unpacked/java \
  --maven /absolute/path/to/mvn --java /absolute/path/to/jdk/bin/java \
  --build-directory /absolute/path/to/artifacts/build \
  --dependency-directory /absolute/path/to/artifacts/maven \
  --output /absolute/path/to/artifacts/jena-bundle
```

The build downloads dependencies into the supplied Maven location, checks real
positive/negative operations and prints the verified manifest pin. Use new output
and build directories. Pass the pin, Java executable, bundle and artifact paths to
`prepare_jena_backend` from `bijux_ontology_adapters.jena`. Runtime operations do
not compile or retrieve dependencies. The current native profile requires POSIX
process ownership; JVM heap, wall time and bounded transport constrain execution.

The optional `[oak]` extra uses the published OAK 0.6.23 profile with
EUtils >=0.6.1 and Setuptools >=84.1.0 on Python >=3.12,<3.15. Core Bijux
retains Python 3.11 support. The existing OAK basic interface remains available,
including scoped metadata, membership, relationship selection and bounded
failures. No Git dependency or private package index is needed to install the
extra from PyPI.

OAK 0.7.4 declares `setuptools<82`, which prevents the patched Setuptools
releases required by the [manifest exclusion](https://github.com/advisories/GHSA-h35f-9h28-mq5c)
and [legacy wheel installation](https://github.com/advisories/GHSA-xmj3-9gf7-h52w)
advisories. OAK 0.6.23 has no such cap; EUtils 0.6.1 removes its legacy
`pkg_resources` dependency and works with the patched Setuptools releases.
The qualified published profile therefore preserves this integration without
retaining that vulnerable dependency. Future OAK upgrades must preserve the
security floors and pass the installed integration tests.
