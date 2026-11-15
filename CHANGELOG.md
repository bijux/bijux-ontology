# Changelog

## Unreleased

- Remove the artificial root distribution and retain the four independently
  owned workspace packages. Adopt the accepted shared workflow governance and
  Python tooling projection from `bijux-std` main at
  `3b401bb8619534952bd7a54087a0cea4393e4b77`.
  [PR #81](https://github.com/bijux/bijux-ontology/pull/81)
- Record the remaining OAK dependency restriction: published OAK 0.7.4 requires
  `setuptools<82`, while the two open Setuptools advisories require patched
  releases at 83.0.0 and 84.1.0. The canonical upstream fix is unreleased;
  neither alert has been dismissed or claimed fixed.
  [PR #81](https://github.com/bijux/bijux-ontology/pull/81)
