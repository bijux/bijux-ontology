# Changelog

## Unreleased

- Remove the artificial root distribution and retain the four independently
  owned workspace packages. Adopt the accepted shared workflow governance and
  Python tooling projection from `bijux-std` main at
  `73bea158e6cc067a04c351938242491d59861a83`, including ambient Python
  interpreter discovery for shared checks. Declare Ontology's required root,
  standard, configuration and Make checks for the shared verification report.
  [PR #81](https://github.com/bijux/bijux-ontology/pull/81)
- Qualify published OAK 0.6.23 with EUtils >=0.6.1 and Setuptools >=84.1.0,
  resolving the two Setuptools security advisories without a Git dependency or
  metadata override. The optional OAK profile requires Python >=3.12,<3.15;
  core Bijux retains Python 3.11 support. Scoped OAK behavior, installed package
  isolation and separate Frictionless runtimes remain covered by real tests.
  [PR #82](https://github.com/bijux/bijux-ontology/pull/82)
