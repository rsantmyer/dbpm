# Changelog

Notable changes to dbpm are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and dbpm follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.5.3] - 2026-09-10

### Fixed

- `dbpm resume` now re-runs the script matching the in-flight operation's
  mode (e.g. `upgrade`) instead of always re-running `install`; an install
  script can legitimately refuse to run against a schema its own upgrade
  already touched. A package the operation hasn't reached yet still falls
  back to `install`.

## [1.5.2] - 2026-09-02

### Fixed

- `dbpm uninstall --application <name>` (source-free uninstall) no longer
  unconditionally requires `--runtime-prefix`; it's now only required when
  the installed application actually has runtime content to tear down.
- `resume` can now recover a multi-package composite plan when the root
  package never started; packages with no live installed state fall back
  to `install` mode instead of the whole resume being rejected.
- Uninstall no longer crashes when a package's application runtime never
  activated; a missing runtime receipt is now tolerated on the uninstall
  path.
- Fixed a `NameError` from an undefined `_receipt_hook_environment`
  reference during receipt-backed application-runtime staging.
- Operation-tracking SQL now sets `LINESIZE 32767` so pipe-delimited
  `DBMS_OUTPUT` used for operation tracking isn't wrapped by SQL*Plus's
  default 80-column width, which could corrupt line-based parsing of long
  operation ids or reasons.

## [1.5.1] - 2026-08-29

### Fixed

- Stop pulling an already-installed, merely-reachable dependency's runtime
  into `plan["application_runtime"]` when the root package declares no
  runtime of its own; this previously forced an unrelated `--runtime-prefix`
  and unwanted restaging of that dependency's already-active runtime
  commands.

## [1.5.0] - 2026-08-17

### Added

- Coordinate database and application-runtime deployment as a durable composite
  operation stored in Core, with fenced time-bound leases, monotonic recovery
  attempts, per-step evidence, and explicit recovery states.
- Resume runtime work without replaying a database install or upgrade after
  Core confirms the database phase completed.
- Add `dbpm runtime reconcile` for receipt-backed structural runtime repair;
  conflicting replacement requires `DBPM_ALLOW_RUNTIME_REPLACE`.
- Add Core-held lifecycle capability enforcement for mutable sources,
  same-version replacement, runtime replacement, graph reset, and complete
  environment reset.
- Add graph-aware reinstall with dependency sources, consumer-first removal,
  dependency-first installation, and graph-wide runtime replacement.
- Add `dbpm dev reset` as an audited command surface over canonical reinstall,
  and `dbpm dev reset-environment --keep CORE` for explicitly disposable
  schemas.
- Add manifest-declared `state` classification for a package's `etc`/`var`
  content (`config`, `secret`, `business_data`, `work_state`, `cache`, `log`).
  `dev reset-environment` reports classified and unclassified preserved paths
  in its confirmation summary, and `--purge-var CATEGORY` deletes only
  manifest-classified files in the selected purgeable categories; `secret`
  and `config` can never be purged, and unclassified paths are always
  preserved.
- Report concise deployment progress on stderr while preparing plans, checking
  package state, executing database deployments, and staging and activating
  application runtimes. `--verbose` exposes detailed source resolution and
  individual database inspections without changing machine-readable stdout.
- Emit a command-specific success line on stderr after deployment and runtime
  work has fully completed. Dry runs do not emit a success claim.

### Fixed

- Allow runtime receipts for database-only root applications whose runtime is
  supplied entirely by dependencies. Final runtime cleanup no longer rejects
  a successfully activated receipt merely because the root has no payload in
  the receipt's runtime-package map.
- Check an existing runtime receipt's root-application ownership during
  runtime-prefix preflight. Installing an application into a prefix owned by a
  different root now fails before database provenance staging or deployment
  scripts can leave a partial database installation.

## [1.4.3] - 2026-08-10

### Fixed

- Run runtime health validation before database uninstall scripts, then use
  structural-only runtime validation during cleanup. Database-dependent health
  checks no longer fail after their package objects have already been removed
  or prevent the old runtime receipt from being archived.
- Validate that a runtime prefix was supplied and is an existing writable
  directory before running database install scripts. Invalid runtime target
  configuration no longer leaves the database component installed without its
  runtime.

## [1.4.2] - 2026-08-05

### Added

- Added manifest-level `dbpm.minimum_version` compatibility requirements.
  dbpm rejects incompatible root packages and dependencies during source
  loading and carries the requirement into plans, lockfiles, and registry
  metadata.
- Added `DBPM_DB_USER`, `DBPM_DB_PASSWORD`, and `DBPM_DB_DSN` as a structured
  database connection alternative to `DBPM_CONNECT`. The values are composed
  for Oracle CLI execution and inherited by package runtime scripts.

## [1.4.1] - 2026-07-31

### Added

- Added PyPI package metadata, installation guidance, and official links for
  dbpm.io, the source repository, and the dbpm package registry.
- Added a GitHub Release workflow that verifies, builds, and publishes Python
  distributions to PyPI through Trusted Publishing and attaches the same
  artifacts to the GitHub release.

### Changed

- Updated package license metadata to use the SPDX `Apache-2.0` expression.
- Isolated unit tests from local registry configuration so release validation
  does not inherit publisher credentials or metadata overrides.
- Defined `DBPM_RUNTIME_PREFIX` as the required activated-command environment
  contract and clarified that durable configuration, logs, and spool data
  belong in the application-level `etc` and `var` directories.
- Clarified that flat durable directories are the default and that package
  uninstall scripts must preserve shared directories and operator-owned files.

### Fixed

- Verified a locked install against its complete database-independent source
  resolution before omitting dependencies already satisfied in the target
  database. This allows composed runtime installation to retain satisfied
  runtime contributors without reporting a false lockfile execution-order
  mismatch.

## [1.4.0] - 2026-07-25

### Changed

- Replaced the design-only `runtime.into` proposal with a composable
  application runtime specification based on a root-application prefix,
  isolated versioned dependency payloads, declarative command exports, and
  graph-level activation receipts.
- Replaced the short-lived package-owned runtime manifest and execution path;
  `runtime.name`, `runtime.home_env`, `runtime.into`, and `runtime.layout` are
  rejected rather than maintained as compatibility contracts.
- Added validation for the composable `runtime` manifest and an
  application-runtime receipt model.
- Added read-only application runtime graph planning, including isolated
  payload paths, root-controlled aliases and suppression, and export collision
  checks.
- Added locked application runtime staging, package-local script execution
  with the injected application environment contract, per-package log capture,
  and staged command validation that rejects missing, non-executable, or
  payload-escaping targets.
- Added application runtime install activation: validated payloads are
  promoted into versioned package directories, command symlinks are published
  through a dbpm-managed `bin` directory, and the active graph receipt is
  written atomically.
- Added graph-aware runtime validation for receipt/package provenance,
  payload directories, the exact managed command-link set, executable target
  integrity, and package-declared read-only validation scripts.
- Added deterministic runtime resume metadata. Failed, interrupted, and ready
  stages record their exact graph and status; resume selects only a matching
  incomplete generation and reconstructs failed payloads in place.
- Added runtime upgrade and reinstall generation switching. Upgrade reuses
  only identity-matching unchanged payloads and retains prior versions;
  reinstall reconstructs same-version payloads while retaining recovery
  backups.
- Added receipt-reachability retention and garbage collection. The active and
  immediately prior generation are retained; unreachable payload versions,
  expired receipts, command backups, and reinstall backups are removed under
  the application runtime lock.
- Added destructive `dbpm uninstall` planning and execution. Runtime cleanup
  scripts run in reverse dependency order before managed links, payloads,
  retained generations, and the active receipt are removed; `etc`, `var`, and
  unmanaged files are preserved.
- Added `dbpm rollback` for retained application runtime generations.
  Rollback requires exact Core database-version compatibility for every target
  package and records the reactivation as a new generation.
- Added durable activation journaling and automatic crash recovery across
  payload promotion, command-directory switching, and receipt publication.
- Added cross-platform command publication: relative symlinks are preferred,
  with executable hard-link fallback and identity-based validation when
  symlink creation is denied by the platform or local policy.
- Added safe runtime version-path validation and plan-visible payload/command
  effects, and documented database-first partial-failure recovery semantics.

### Fixed

- Relocated text launchers created in staging before payload promotion so
  virtual-environment entry points continue to work from their final paths.
- Retained supplied runtime artifacts for dependencies already satisfied in
  the database, while excluding database-only dependencies from runtime
  staging.
- Deferred root activation references until the complete dependency graph is
  available.
- Excluded `dbpm-lock.json` from local directory artifact checksums so a
  tracked lockfile does not change its own package identity.

## [1.3.0] - 2026-07-23

### Added

- Added manifest-declared runtime components for non-database payloads.
- Added runtime prefix resolution, installed-state receipts, explicit runtime
  plan output, and runtime install, upgrade, resume, reinstall, and validation
  execution.
- Added a design specification for a future first-class `dbpm test` command.

### Fixed

- Limited package tree exclusions for `build`, `dist`, and `target` to
  package-root directories so identically named nested content is retained.

## [1.2.2] - 2026-07-05

### Changed

- Validated manifest script paths before execution.
- Escaped generated SQL metadata safely.
- Sent a fallback successful exit directive to SQL runners.

### Security

- Rejected unsafe ZIP member paths during package extraction.

## [1.2.0] - 2026-07-04

### Added

- Added Core 3.5 deployment metadata prompt support.
- Added deployment-environment selection to Core bootstrap.

### Changed

- Refactored deployment policy handling and its documentation.

## [1.1.2] - 2026-07-03

### Added

- Added actionable suggested commands to deployment errors.
- Added issue templates and project contribution and security guidance.

### Changed

- Distinguished raw Oracle connect strings from SQLcl saved connections.
- Standardized shell examples and clarified that `uv` is a contributor
  convenience rather than a consumer requirement.

## [1.1.0] - 2026-06-24

### Added

- Added SQLcl saved-connection support.

## [1.0.1] - 2026-06-19

### Fixed

- Set the intended Oracle schema explicitly in generated deployment scripts.

[Unreleased]: https://github.com/512itconsulting/dbpm/compare/v1.5.0...HEAD
[1.5.0]: https://github.com/512itconsulting/dbpm/compare/v1.4.3...v1.5.0
[1.4.3]: https://github.com/512itconsulting/dbpm/compare/v1.4.2...v1.4.3
[1.4.2]: https://github.com/512itconsulting/dbpm/compare/v1.4.1...v1.4.2
[1.4.1]: https://github.com/512itconsulting/dbpm/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/512itconsulting/dbpm/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/512itconsulting/dbpm/compare/v1.2.2...v1.3.0
[1.2.2]: https://github.com/512itconsulting/dbpm/compare/v1.2.0...v1.2.2
[1.2.0]: https://github.com/512itconsulting/dbpm/compare/v1.1.2...v1.2.0
[1.1.2]: https://github.com/512itconsulting/dbpm/compare/v1.1.0...v1.1.2
[1.1.0]: https://github.com/512itconsulting/dbpm/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/512itconsulting/dbpm/releases/tag/v1.0.1
