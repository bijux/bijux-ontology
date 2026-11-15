# Changelog

## Unreleased

- Remove the artificial root distribution and retain the four independently
  owned workspace packages. Adopt the accepted shared workflow governance and
  Python tooling projection from `bijux-std` main at
  `73bea158e6cc067a04c351938242491d59861a83`, including ambient Python
  interpreter discovery for shared checks. Declare Ontology's required root,
  standard, configuration and Make checks for the shared verification report.
  [PR #81](https://github.com/bijux/bijux-ontology/pull/81)
- Record the remaining OAK dependency restriction: published OAK 0.7.4 requires
  `setuptools<82`, while the two open Setuptools advisories require patched
  releases at 83.0.0 and 84.1.0. Keep the PyPI-compatible OAK extra until a
  fixed upstream release can be qualified. Both alerts remain blocked by that
  release prerequisite; neither has been dismissed or claimed fixed.
  [PR #81](https://github.com/bijux/bijux-ontology/pull/81)
